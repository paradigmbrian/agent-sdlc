from dataclasses import replace

from agent_sdlc.orchestrator.reporting import (
    MAX_PR_DESCRIPTION,
    commit_message,
    park_comment_html,
    plan_comment_html,
    pr_body,
    pr_title,
)
from agent_sdlc.types import Decision, Item, ParkReason, Stage, Usage, WorkItem

WI = WorkItem(5, "Approve <button> broken", "d", "ac", "Bug", ("agent",), "https://x/5")
ITEM = Item(5, "t", WI.title, "agent/5-x", Stage.PR_OPEN, attempt=1,
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
    assert "agent:parked" in html and "red" in html


def test_park_comment_gate_park_from_triage() -> None:
    """NEEDS_HUMAN park from TRIAGE should mention approving/proceeding to plan."""
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                parked_from=Stage.TRIAGE, data={"park_note": "Unclear scope"})
    html = park_comment_html(item)
    assert "approving" in html or "proceeding" in html
    assert "plan" in html
    assert "retries" not in html


def test_park_comment_red_park_from_verify() -> None:
    """RED park from VERIFY should mention retrying verify, not gate approval."""
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.RED,
                parked_from=Stage.VERIFY, data={"park_note": "Tests failed"})
    html = park_comment_html(item)
    assert "retry" in html
    assert "verify" in html
    assert "approving" not in html and "proceeding" not in html


def test_park_comment_gate_park_from_implement() -> None:
    """NEEDS_HUMAN park from IMPLEMENT should retry, not proceed to next gate stage."""
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                parked_from=Stage.IMPLEMENT, data={"park_note": "No changes"})
    html = park_comment_html(item)
    assert "retry" in html
    assert "implement" in html


def test_plan_comment_and_commit_message() -> None:
    assert "PR !42" in plan_comment_html("<b>x</b>", 42)
    assert "&lt;b&gt;" in plan_comment_html("<b>x</b>", 42)
    msg = commit_message(WI, 0)
    assert msg.startswith("fix: Approve <button> broken") and "AB#5" in msg
    assert "Co-Authored-By" not in msg


# --- final review fix wave ---------------------------------------------------------------


def test_i3_plan_park_comment_includes_escaped_plan() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.PLAN_REJECTED,
                parked_from=Stage.PLAN,
                data={"park_note": "n", "plan": "1. edit <Login>" + "x" * 10000})
    html = park_comment_html(item)
    assert "1. edit &lt;Login&gt;" in html and "<Login>" not in html
    assert len(html) < 7500


def test_i3_review_park_comment_includes_escaped_review_notes() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                parked_from=Stage.REVIEW,
                data={"park_note": "n", "plan": "PLANTEXT", "review_notes": "BLOCKING: <x>"})
    html = park_comment_html(item)
    assert "BLOCKING: &lt;x&gt;" in html and "PLANTEXT" not in html


def test_i4_pr_body_shows_cache_reads_separately() -> None:
    item = replace(ITEM, usage=Usage(12, 30000, 4000, cache_read_tokens=500000))
    body = pr_body(item, WI, DEC, CHECKS, "")
    assert "34,000 tokens" in body and "500,000 cache-read tokens" in body


def test_i5_pr_rounds_park_comment_says_implement() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.PR_ROUNDS,
                parked_from=Stage.AWAITING_HUMAN, data={"park_note": "rounds"})
    assert "<code>implement</code>" in park_comment_html(item)


def test_park_comment_agent_error_from_plan_says_retry() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.AGENT_ERROR,
                parked_from=Stage.PLAN, data={"park_note": "agent did not finish"})
    html = park_comment_html(item)
    assert "retry" in html and "plan" in html
    assert "approving" not in html and "proceeding" not in html
