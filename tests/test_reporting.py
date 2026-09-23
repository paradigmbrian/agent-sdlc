from laya_sdlc.orchestrator.reporting import (
    MAX_PR_DESCRIPTION,
    commit_message,
    park_comment_html,
    plan_comment_html,
    pr_body,
    pr_title,
)
from laya_sdlc.types import Decision, Item, ParkReason, Stage, Usage, WorkItem

WI = WorkItem(5, "Approve <button> broken", "d", "ac", "Bug", ("laya",), "https://x/5")
ITEM = Item(5, "t", WI.title, "laya/5-x", Stage.PR_OPEN, attempt=1,
            data={"plan": "1. fix it", "note": ""}, usage=Usage(12, 30000, 4000))
DEC = [Decision("review", "risk", "low", {"low": 0.9}, {"low": 0.9}, 0.9, True, False)]
CHECKS = [{"name": "test", "command": "npm test", "exit_code": 0, "output": "ok",
           "duration_s": 3.2}]


def test_pr_title_prefix_by_type() -> None:
    assert pr_title(WI) == "fix: Approve <button> broken (AB#5)"
    assert pr_title(WorkItem(6, "Add x", "", "", "User Story", (), "")).startswith("feat: ")


def test_pr_body_contents() -> None:
    body = pr_body(ITEM, WI, DEC, CHECKS, "No blocking issues.")
    assert "AB#5" in body and "1. fix it" in body and "npm test" in body
    assert "risk" in body and "shadow" in body
    assert "12 turns" in body and "34,000 tokens" in body


def test_pr_body_truncates_to_ado_limit() -> None:
    long_item = Item(5, "t", "x", "b", Stage.PR_OPEN, data={"plan": "p" * 10000})
    body = pr_body(long_item, WI, DEC, CHECKS, "r" * 10000)
    assert len(body) <= MAX_PR_DESCRIPTION
    assert "truncated" in body


def test_park_comment_escapes_html() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.RED,
                parked_from=Stage.VERIFY, data={"park_note": "<script>boom</script>"})
    html = park_comment_html(item)
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "laya:parked" in html and "red" in html


def test_plan_comment_and_commit_message() -> None:
    assert "PR !42" in plan_comment_html("<b>x</b>", 42)
    assert "&lt;b&gt;" in plan_comment_html("<b>x</b>", 42)
    msg = commit_message(WI, 0)
    assert msg.startswith("fix: Approve <button> broken") and "AB#5" in msg
    assert "Co-Authored-By" not in msg
