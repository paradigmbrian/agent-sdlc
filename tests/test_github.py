import base64
from urllib.parse import unquote

import httpx
import pytest
import respx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github import GitHubForge, split_acceptance
from agent_sdlc.targets import GitHubForgeConfig, IntakeConfig

API = "https://api.github.com"
REPO = f"{API}/repos/paradigmbrian/triathlon-agent"
CFG = GitHubForgeConfig(kind="github", owner="paradigmbrian", repo="triathlon-agent", app_id=42)


class FakeAuth:
    def __init__(self) -> None:
        self.tokens = ["ghs_one", "ghs_two"]
        self.invalidated = 0

    def token(self) -> str:
        return self.tokens[0]

    def invalidate(self) -> None:
        self.invalidated += 1
        self.tokens.pop(0)

    def bot_login(self) -> str:
        return "agent-sdlc-bot[bot]"


def forge(dry_run: bool = False, auth: FakeAuth | None = None,
          push_url: str | None = None) -> GitHubForge:
    return GitHubForge(CFG, auth or FakeAuth(), intake=IntakeConfig(), base_branch="main",
                       branch_prefix="agent/", http=httpx.Client(base_url=API),
                       push_url=push_url, dry_run_push=dry_run)


def issue(n: int, body: str | None = "Do it", labels: tuple[str, ...] = ("agent",),
          pr: bool = False) -> dict[str, object]:
    d: dict[str, object] = {"number": n, "title": f"Issue {n}", "body": body,
                            "labels": [{"name": x} for x in labels],
                            "html_url": f"https://github.com/x/{n}"}
    if pr:
        d["pull_request"] = {"url": "u"}
    return d


def test_split_acceptance() -> None:
    body = "Intro\n\n## Acceptance criteria\n- a\n- b\n\n## Notes\nlater"
    assert split_acceptance(body) == ("Intro\n\n## Notes\nlater", "- a\n- b")
    assert split_acceptance("### acceptance criteria:\nx\n#### sub\ny") == ("", "x\n#### sub\ny")
    assert split_acceptance("no section") == ("no section", "")
    assert split_acceptance("") == ("", "")


def test_port_attributes_and_git_header() -> None:
    f = forge()
    assert f.kind == "github" and f.label_word == "label"
    assert f.pr_ref(3) == "#3" and f.item_ref(5) == "#5"
    scheme, value = f.git_auth_header().split(": ", 1)[1].split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == "x-access-token:ghs_one"


@respx.mock
def test_list_intake_drops_prs_and_maps_items() -> None:
    route = respx.get(f"{REPO}/issues").mock(return_value=httpx.Response(200, json=[
        issue(5, "Body\n## Acceptance criteria\nworks", ("agent", "bug")),
        issue(6, pr=True),
        issue(7, None),
    ]))
    items = forge().list_intake()
    assert [i.id for i in items] == [5, 7]
    first, second = items
    assert (first.description, first.acceptance_criteria) == ("Body", "works")
    assert first.work_item_type == "Bug" and first.tags == ("agent", "bug")
    assert (second.description, second.acceptance_criteria, second.work_item_type) == \
        ("", "", "Issue")
    params = route.calls[0].request.url.params
    assert params["labels"] == "agent" and params["state"] == "open"
    assert route.calls[0].request.headers["Authorization"] == "Bearer ghs_one"


@respx.mock
def test_list_intake_reads_every_page() -> None:
    page1 = [issue(n) for n in range(1, 101)]
    page2 = [issue(101)]
    respx.get(f"{REPO}/issues", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=page1))
    respx.get(f"{REPO}/issues", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=page2))
    assert len(forge().list_intake()) == 101


@respx.mock
def test_get_item_refuses_a_pull_request() -> None:
    respx.get(f"{REPO}/issues/6").mock(return_value=httpx.Response(200, json=issue(6, pr=True)))
    with pytest.raises(ForgeError, match="pull request"):
        forge().get_item(6)


@respx.mock
def test_labels_add_remove_encoded_and_has() -> None:
    add = respx.post(f"{REPO}/issues/5/labels").mock(return_value=httpx.Response(200, json=[]))
    rm = respx.delete(url__regex=rf"{REPO}/issues/5/labels/.+").mock(
        return_value=httpx.Response(404))
    respx.get(f"{REPO}/issues/5").mock(
        return_value=httpx.Response(200, json=issue(5, labels=("agent", "agent:parked"))))
    f = forge()
    f.set_label(5, "agent:parked", True)
    f.set_label(5, "agent:parked", False)   # 404: already gone is success
    assert add.calls[0].request.content == b'{"labels":["agent:parked"]}'
    raw_path = rm.calls[0].request.url.raw_path.decode()
    assert raw_path.endswith("/labels/agent%3Aparked")
    assert unquote(raw_path).endswith("/labels/agent:parked")
    assert f.has_label(5, "agent:parked") and not f.has_label(5, "nope")


@respx.mock
def test_comment_item_posts_html() -> None:
    route = respx.post(f"{REPO}/issues/5/comments").mock(return_value=httpx.Response(201))
    forge(dry_run=True).comment_item(5, "<p>hi</p>")   # issue comments stay real in dry run
    assert route.calls[0].request.content == b'{"body":"<p>hi</p>"}'


@respx.mock
def test_401_refreshes_once_then_retries() -> None:
    route = respx.get(f"{REPO}/issues/5").mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json=issue(5))])
    auth = FakeAuth()
    assert forge(auth=auth).get_item(5).id == 5
    assert auth.invalidated == 1
    assert route.calls[1].request.headers["Authorization"] == "Bearer ghs_two"


@respx.mock
def test_errors_and_rate_limits_never_leak_tokens() -> None:
    respx.get(f"{REPO}/issues/5").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"}))
    respx.get(f"{REPO}/issues/6").mock(return_value=httpx.Response(429))
    respx.get(f"{REPO}/issues/7").mock(return_value=httpx.Response(404))
    f = forge()
    with pytest.raises(ForgeError, match="rate limit.*1900000000") as e:
        f.get_item(5)
    assert "ghs_" not in str(e.value)
    with pytest.raises(ForgeError, match="rate limit"):
        f.get_item(6)
    with pytest.raises(ForgeError, match="HTTP 404"):
        f.get_item(7)


@respx.mock
def test_list_closed_limits_and_drops_prs() -> None:
    respx.get(f"{REPO}/issues").mock(return_value=httpx.Response(200, json=[
        issue(1), issue(2, pr=True), issue(3), issue(4)]))
    assert [i.id for i in forge().list_closed(2)] == [1, 3]
