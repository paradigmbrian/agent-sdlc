import base64
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github import GitHubForge, split_acceptance
from agent_sdlc.targets import GitHubForgeConfig, IntakeConfig, TargetConfig
from agent_sdlc.types import PrComment
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git

API = "https://api.github.com"
REPO = f"{API}/repos/paradigmbrian/triathlon-agent"
CFG = GitHubForgeConfig(kind="github", owner="paradigmbrian", repo="triathlon-agent", app_id=42)


class FakeAuth:
    def __init__(self) -> None:
        self.tokens = ["ghs_one", "ghs_two"]
        self.invalidated = 0
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
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


@respx.mock
def test_list_closed_limit_applies_after_pr_filtering() -> None:
    """When page 1 has many PRs, limit should apply to issues after filtering."""
    # Page 1: issues 1-10, with PRs at positions where i % 2 == 0 (5 PRs, 5 issues)
    page1 = [issue(i, pr=(i % 2 == 0)) for i in range(1, 101)]  # 50 PRs, 50 issues
    # Page 2: more issues
    page2 = [issue(i) for i in range(101, 111)]  # 10 more issues
    respx.get(f"{REPO}/issues", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=page1))
    respx.get(f"{REPO}/issues", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=page2))
    result = forge().list_closed(10)
    assert len(result) == 10
    # Expected: first 10 issues after filtering PRs from page 1 and page 2
    expected = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    assert [i.id for i in result] == expected


@respx.mock
def test_429_uses_retry_after_when_ratelimit_reset_absent() -> None:
    """429 with Retry-After header should report retry time (not 'unknown')."""
    respx.get(f"{REPO}/issues/5").mock(return_value=httpx.Response(
        429, headers={"Retry-After": "60"}))
    f = forge()
    with pytest.raises(ForgeError, match="rate limit.*retry after 60 s") as e:
        f.get_item(5)
    assert "ghs_" not in str(e.value)


def user(login: str) -> dict[str, str]:
    return {"login": login}


@respx.mock
def test_create_and_update_pr_carry_closes_line() -> None:
    create = respx.post(f"{REPO}/pulls").mock(
        return_value=httpx.Response(201, json={"number": 12}))
    update = respx.patch(f"{REPO}/pulls/12").mock(return_value=httpx.Response(200, json={}))
    f = forge()
    assert f.create_pr("agent/5-x", "fix: x (#5)", "body", 5) == 12
    sent = httpx.Response(200, content=create.calls[0].request.content).json()
    assert sent == {"title": "fix: x (#5)", "head": "agent/5-x", "base": "main",
                    "body": "body\n\nCloses #5"}
    f.update_pr(12, "new", 5)
    assert httpx.Response(200, content=update.calls[0].request.content).json() == {
        "body": "new\n\nCloses #5"}
    with pytest.raises(ForgeError):
        f.create_pr("main", "t", "b", 5)


@respx.mock
def test_pr_status_mapping() -> None:
    respx.get(f"{REPO}/pulls/1").mock(return_value=httpx.Response(
        200, json={"state": "closed", "merged": True}))
    respx.get(f"{REPO}/pulls/2").mock(return_value=httpx.Response(
        200, json={"state": "closed", "merged": False}))
    respx.get(f"{REPO}/pulls/3").mock(return_value=httpx.Response(
        200, json={"state": "open", "merged": False}))
    f = forge()
    assert [f.pr_status(n) for n in (1, 2, 3)] == ["completed", "abandoned", "active"]


@respx.mock
def test_pr_comments_merge_three_sources_and_skip_the_bot() -> None:
    respx.get(f"{REPO}/issues/12/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 1, "user": user("brian"), "body": "please add docs"},
        {"id": 2, "user": user("agent-sdlc-bot[bot]"), "body": "my own reply"},
        {"id": 3, "user": user("brian"), "body": "   "},
    ]))
    respx.get(f"{REPO}/pulls/12/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 10, "user": user("brian"), "body": "rename this", "in_reply_to_id": None},
        {"id": 11, "user": user("brian"), "body": "and this", "in_reply_to_id": 10},
    ]))
    respx.get(f"{REPO}/pulls/12/reviews").mock(return_value=httpx.Response(200, json=[
        {"id": 20, "user": user("brian"), "body": "", "state": "CHANGES_REQUESTED"},
        {"id": 21, "user": user("brian"), "body": "", "state": "APPROVED"},
        {"id": 22, "user": user("brian"), "body": "/agent tidy up", "state": "COMMENTED"},
        {"id": 23, "user": user("brian"), "body": "draft", "state": "PENDING"},
    ]))
    got = forge().pr_comments(12)
    assert [(c.kind, c.thread_id, c.comment_id, c.content, c.changes_requested) for c in got] == [
        ("conversation", 0, 1, "please add docs", False),
        ("review_comment", 10, 10, "rename this", False),
        ("review_comment", 10, 11, "and this", False),
        ("review", 0, 20, "(changes requested with no summary)", True),
        ("review", 0, 22, "/agent tidy up", False),
    ]
    assert len({c.key for c in got}) == 5


@respx.mock
def test_pr_comments_read_every_page() -> None:
    first = [{"id": n, "user": user("brian"), "body": f"c{n}"} for n in range(1, 101)]
    respx.get(f"{REPO}/issues/12/comments", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=first))
    respx.get(f"{REPO}/issues/12/comments", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[{"id": 101, "user": user("b"), "body": "last"}]))
    respx.get(f"{REPO}/pulls/12/comments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REPO}/pulls/12/reviews").mock(return_value=httpx.Response(200, json=[]))
    assert len(forge().pr_comments(12)) == 101


@respx.mock
def test_reply_routing() -> None:
    thread = respx.post(f"{REPO}/pulls/12/comments/10/replies").mock(
        return_value=httpx.Response(201))
    convo = respx.post(f"{REPO}/issues/12/comments").mock(return_value=httpx.Response(201))
    f = forge()
    f.reply_pr(12, PrComment(10, 11, "brian", "why?", kind="review_comment"), "Because.")
    f.reply_pr(12, PrComment(0, 1, "brian", "line one\nline two", kind="conversation"), "Sure.")
    assert httpx.Response(200, content=thread.calls[0].request.content).json() == {
        "body": "Because."}
    body = httpx.Response(200, content=convo.calls[0].request.content).json()["body"]
    assert body == "> line one\n> line two\n\n@brian Sure."


@respx.mock
def test_delete_branch_tolerates_missing_and_refuses_other_refs() -> None:
    route = respx.delete(f"{REPO}/git/refs/heads/agent/5-x").mock(
        return_value=httpx.Response(422))
    f = forge()
    f.delete_branch("agent/5-x")
    assert route.call_count == 1
    with pytest.raises(ForgeError):
        f.delete_branch("main")


@respx.mock
def test_dry_run_makes_pr_side_effects_no_ops() -> None:
    f = forge(dry_run=True)
    assert f.create_pr("agent/5-x", "t", "b", 5) == 0
    f.update_pr(0, "b", 5)
    f.comment_pr(0, "x")
    f.reply_pr(0, PrComment(0, 1, "a", "c", kind="conversation"), "x")
    f.delete_branch("agent/5-x")
    assert f.pr_status(0) == "active" and f.pr_comments(0) == []
    assert respx.calls.call_count == 0


def test_push_branch_to_local_origin(tmp_path: Path, target: TargetConfig,
                                     origin_repo: Path) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "agent/5-x")
    (wt / "f.txt").write_text("x")
    ws.commit(wt, "feat: f")
    f = forge(push_url=str(origin_repo))
    f.push_branch(wt, "agent/5-x")
    assert "agent/5-x" in git("branch", "--list", "agent/*", cwd=origin_repo)
    with pytest.raises(ForgeError):
        f.push_branch(wt, "main")


def test_m8_push_branch_fetches_the_token_only_once(
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    """push_branch must build its auth header from the same token it redacts with, not fetch
    the token a second time (which could differ) via git_auth_header() (M-8)."""
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "agent/5-x")
    (wt / "f.txt").write_text("x")
    ws.commit(wt, "feat: f")
    auth = FakeAuth()
    f = forge(auth=auth, push_url=str(origin_repo))
    f.push_branch(wt, "agent/5-x")
    assert auth.calls == 1


def test_push_failure_is_redacted(tmp_path: Path, target: TargetConfig) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "agent/5-x")
    f = forge(push_url=str(tmp_path / "missing-ghs_one.git"))
    with pytest.raises(ForgeError) as e:
        f.push_branch(wt, "agent/5-x")
    assert "ghs_one" not in str(e.value)


def test_github_forge_satisfies_the_port() -> None:
    from agent_sdlc.ports import ForgePort
    port: ForgePort = forge()
    assert port.kind == "github"
