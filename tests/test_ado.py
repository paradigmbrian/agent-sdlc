import json
from pathlib import Path

import httpx
import pytest
import respx

from laya_sdlc.adapters.ado import AdoClient, AdoError, html_to_text
from laya_sdlc.secrets import SecretNotFound, basic_auth_header, get_secret
from laya_sdlc.targets import AdoConfig, TargetConfig
from laya_sdlc.workspaces import Workspaces
from tests.conftest import git

BASE = "https://dev.azure.com/MilesThurman"
PROJ = f"{BASE}/CodvoMigration/_apis"
REPO = f"{PROJ}/git/repositories/RallySource"
CFG = AdoConfig(org="MilesThurman", project="CodvoMigration", repo="RallySource")
SELF_ID = "self-guid"


@pytest.fixture
def client() -> AdoClient:
    return AdoClient(CFG, "pat", http=httpx.Client(base_url=BASE, auth=("", "pat")))


def _wi(id_: int, desc: str = "<div>Hello<br>world</div>", tags: str = "laya") -> dict[str, object]:
    return {"id": id_, "fields": {
        "System.Title": f"Item {id_}", "System.Description": desc,
        "Microsoft.VSTS.Common.AcceptanceCriteria": "<ul><li>a</li><li>b</li></ul>",
        "System.Tags": tags, "System.WorkItemType": "Bug"}}


def test_html_to_text() -> None:
    assert html_to_text("<p>One &amp; two</p><p>Three<br/>four</p>") == "One & two\nThree\nfour"
    assert html_to_text("") == ""


def test_basic_auth_header() -> None:
    assert basic_auth_header("pat") == "Authorization: Basic OnBhdA=="


def test_get_secret_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYA_TEST_SECRET", "v")
    assert get_secret("laya-test-secret-that-does-not-exist", "LAYA_TEST_SECRET") == "v"
    monkeypatch.delenv("LAYA_TEST_SECRET")
    with pytest.raises(SecretNotFound):
        get_secret("laya-test-secret-that-does-not-exist", "LAYA_TEST_SECRET")


@respx.mock
def test_list_intake_queries_tag_and_fetches(client: AdoClient) -> None:
    wiql = respx.post(f"{PROJ}/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 5}, {"id": 6}]}))
    respx.get(f"{PROJ}/wit/workitems").mock(
        return_value=httpx.Response(200, json={"value": [_wi(5), _wi(6)]}))
    items = client.list_intake()
    assert [i.id for i in items] == [5, 6]
    query = json.loads(wiql.calls[0].request.content)["query"]
    assert "CONTAINS 'laya'" in query and "NOT IN ('Closed', 'Removed', 'Done')" in query


@respx.mock
def test_get_work_items_strips_html(client: AdoClient) -> None:
    respx.get(f"{PROJ}/wit/workitems").mock(
        return_value=httpx.Response(200, json={"value": [_wi(5, tags="laya; laya:parked")]}))
    [wi] = client.get_work_items([5])
    assert wi.description == "Hello\nworld"
    assert wi.acceptance_criteria == "a\nb"
    assert wi.tags == ("laya", "laya:parked")
    assert wi.url.endswith("/_workitems/edit/5")


@respx.mock
def test_set_tag_add_and_remove(client: AdoClient) -> None:
    respx.get(f"{PROJ}/wit/workitems").mock(
        return_value=httpx.Response(200, json={"value": [_wi(5, tags="laya")]}))
    patch = respx.patch(f"{PROJ}/wit/workitems/5").mock(return_value=httpx.Response(200, json={}))
    client.set_tag(5, "laya:parked", True)
    body = json.loads(patch.calls[0].request.content)
    assert body == [{"op": "add", "path": "/fields/System.Tags", "value": "laya; laya:parked"}]
    assert patch.calls[0].request.headers["content-type"] == "application/json-patch+json"
    client.set_tag(5, "laya", False)
    assert json.loads(patch.calls[1].request.content)[0]["value"] == ""


@respx.mock
def test_create_pr_payload(client: AdoClient) -> None:
    route = respx.post(f"{REPO}/pullrequests").mock(
        return_value=httpx.Response(201, json={"pullRequestId": 42}))
    assert client.create_pr("laya/5-x", "fix: x", "body", 5) == 42
    sent = json.loads(route.calls[0].request.content)
    assert sent["sourceRefName"] == "refs/heads/laya/5-x"
    assert sent["targetRefName"] == "refs/heads/dev"
    assert sent["workItemRefs"] == [{"id": "5"}]


def test_create_pr_refuses_non_laya_branch(client: AdoClient) -> None:
    with pytest.raises(AdoError):
        client.create_pr("dev", "t", "b", 5)


@respx.mock
def test_pr_comments_skip_self_and_system(client: AdoClient) -> None:
    respx.get(f"{BASE}/_apis/connectionData").mock(
        return_value=httpx.Response(200, json={"authenticatedUser": {"id": SELF_ID}}))
    respx.get(f"{REPO}/pullRequests/42/threads").mock(return_value=httpx.Response(200, json={
        "value": [
            {"id": 1, "isDeleted": False, "comments": [
                {"id": 1, "commentType": "text", "content": "please rename",
                 "author": {"id": "human", "displayName": "Brian"}},
                {"id": 2, "commentType": "text", "content": "done",
                 "author": {"id": SELF_ID, "displayName": "laya"}},
                {"id": 3, "commentType": "system", "content": "vote",
                 "author": {"id": "human", "displayName": "Brian"}},
                {"id": 4, "commentType": "text", "content": "gone", "isDeleted": True,
                 "author": {"id": "human", "displayName": "Brian"}},
            ]},
            {"id": 2, "isDeleted": True, "comments": [
                {"id": 1, "commentType": "text", "content": "x",
                 "author": {"id": "human", "displayName": "Brian"}}]},
        ]}))
    comments = client.pr_comments(42)
    assert [(c.thread_id, c.comment_id, c.content) for c in comments] == [(1, 1, "please rename")]


@respx.mock
def test_pr_status_and_delete_branch(client: AdoClient) -> None:
    respx.get(f"{REPO}/pullrequests/42").mock(
        return_value=httpx.Response(200, json={"status": "completed"}))
    assert client.pr_status(42) == "completed"
    respx.get(f"{REPO}/refs").mock(return_value=httpx.Response(
        200, json={"value": [{"name": "refs/heads/laya/5-x", "objectId": "abc"}]}))
    post = respx.post(f"{REPO}/refs").mock(return_value=httpx.Response(200, json={}))
    client.delete_branch("laya/5-x")
    assert json.loads(post.calls[0].request.content) == [
        {"name": "refs/heads/laya/5-x", "oldObjectId": "abc", "newObjectId": "0" * 40}]


def test_push_branch_to_local_origin(tmp_path: Path, target: TargetConfig,
                                     origin_repo: Path) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "laya/5-x")
    (wt / "f.txt").write_text("x")
    ws.commit(wt, "feat: f")
    client = AdoClient(CFG, "pat", http=httpx.Client(), push_url=str(origin_repo))
    client.push_branch(wt, "laya/5-x")
    assert "laya/5-x" in git("branch", "--list", "laya/*", cwd=origin_repo)
    with pytest.raises(AdoError):
        client.push_branch(wt, "dev")


def test_push_branch_dry_run_does_nothing(tmp_path: Path, target: TargetConfig,
                                          origin_repo: Path) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "laya/5-x")
    client = AdoClient(CFG, "pat", http=httpx.Client(), push_url=str(origin_repo),
                       dry_run_push=True)
    client.push_branch(wt, "laya/5-x")
    assert git("branch", "--list", "laya/*", cwd=origin_repo) == ""
    assert client.create_pr("laya/5-x", "t", "b", 5) == 0
