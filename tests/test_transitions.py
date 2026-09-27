from dataclasses import replace

import pytest

from agent_sdlc.orchestrator.transitions import (
    CommentOutcome,
    Transition,
    after_implement,
    after_plan,
    after_pr_poll,
    after_review,
    after_triage,
    after_verify,
    apply_transition,
    classify_comment,
    park,
    requeue,
)
from agent_sdlc.types import CommandResult, Decision, Item, ParkReason, PrComment, Stage
from tests.fakes import decision


def d(q: str, answer: str, actionable: bool = True, conf: float = 0.9) -> Decision:
    return Decision("g", q, answer, {}, {}, conf, not actionable, actionable)


GOOD_TRIAGE = {"kind": d("kind", "bug"), "clarity": d("clarity", "clear"),
               "touches_protected": d("touches_protected", "no"), "size": d("size", "small")}
ITEM = Item(1, "t", "Fix", "agent/1-fix", Stage.TRIAGE)
OK = CommandResult("test", "t", 0, "ok", 1.0)
BAD = CommandResult("test", "t", 1, "boom", 1.0)


def test_triage_happy_path() -> None:
    assert after_triage(GOOD_TRIAGE) == Transition(Stage.PLAN)


@pytest.mark.parametrize("key,value", [
    ("kind", d("kind", "question")),
    ("kind", d("kind", "bug", actionable=False)),
    ("clarity", d("clarity", "partly clear")),
    ("touches_protected", d("touches_protected", "unknown", actionable=False)),
    ("touches_protected", d("touches_protected", "yes")),
    ("size", d("size", "large")),
])
def test_triage_parks_for_human(key: str, value: Decision) -> None:
    t = after_triage({**GOOD_TRIAGE, key: value})
    assert t.to is Stage.PARKED and t.park_reason is ParkReason.NEEDS_HUMAN
    assert key in t.note


def test_plan_transitions() -> None:
    yes = {"plan_addresses_item": d("a", "yes"), "plan_scope_ok": d("b", "yes")}
    assert after_plan(yes, 0).to is Stage.IMPLEMENT
    no = {**yes, "plan_scope_ok": d("plan_scope_ok", "no")}
    replan = after_plan(no, 0)
    assert (replan.to is Stage.PLAN and replan.count_replan
            and "plan_scope_ok" in (replan.feedback or ""))
    assert after_plan(no, 1).park_reason is ParkReason.PLAN_REJECTED
    shadow = {**yes, "plan_scope_ok": d("plan_scope_ok", "yes", actionable=False)}
    assert after_plan(shadow, 0).park_reason is ParkReason.PLAN_REJECTED


def test_implement_transitions() -> None:
    assert after_implement([], True, 10, 600).to is Stage.VERIFY
    assert after_implement(["infra/x"], True, 10, 600).park_reason is ParkReason.POLICY
    assert after_implement([], False, 0, 600).park_reason is ParkReason.NEEDS_HUMAN
    assert after_implement([], True, 700, 600).park_reason is ParkReason.POLICY


def test_verify_transitions() -> None:
    assert after_verify([OK], 0, 3).to is Stage.REVIEW
    retry = after_verify([OK, BAD], 0, 3)
    assert retry.to is Stage.IMPLEMENT and retry.count_attempt and "boom" in (retry.feedback or "")
    assert after_verify([BAD], 3, 3).park_reason is ParkReason.RED


def test_review_transitions() -> None:
    clean = {"review_blocking": d("review_blocking", "no"), "risk": d("risk", "low")}
    assert after_review(clean, "No blocking issues.", 0, 3) == Transition(Stage.PR_OPEN)
    blocking = {**clean, "review_blocking": d("review_blocking", "yes")}
    t = after_review(blocking, "BLOCKING: null deref", 0, 3)
    assert t.to is Stage.IMPLEMENT and t.feedback == "BLOCKING: null deref" and t.count_attempt
    assert after_review(blocking, "x", 3, 3).park_reason is ParkReason.NEEDS_HUMAN
    unsure = {**clean, "review_blocking": d("review_blocking", "unknown", actionable=False)}
    t = after_review(unsure, "maybe", 0, 3)
    assert t.to is Stage.PR_OPEN and "not confidently resolved" in t.note


def test_classify_comment() -> None:
    c = PrComment(1, 1, "Brian", "/agent rename foo to bar")
    assert classify_comment(c, None) == "change_request"
    plain = PrComment(1, 2, "Brian", "hmm")
    result = classify_comment(plain, {"comment_intent": d("comment_intent", "question")})
    assert result == "question"
    unsure = {"comment_intent": d("comment_intent", "change_request", actionable=False)}
    assert classify_comment(plain, unsure) == "uncertain"


def test_changes_requested_review_is_always_a_change_request() -> None:
    c = PrComment(0, 9, "brian", "(changes requested with no summary)", kind="review",
                  changes_requested=True)
    assert classify_comment(c, None) == "change_request"
    noise = {"comment_intent": decision("comment", "comment_intent", "noise")}
    assert classify_comment(c, noise) == "change_request"


def test_slash_agent_in_a_review_body_is_a_change_request() -> None:
    c = PrComment(0, 9, "brian", "/agent rename the helper", kind="review")
    assert classify_comment(c, None) == "change_request"


def test_pr_poll_transitions() -> None:
    c = PrComment(1, 1, "Brian", "/agent fix")
    assert after_pr_poll("completed", [], 0, 3).to is Stage.DONE
    assert after_pr_poll("abandoned", [], 0, 3).to is Stage.CLOSED
    assert after_pr_poll("active", [], 0, 3).to is Stage.AWAITING_HUMAN
    t = after_pr_poll("active", [CommentOutcome(c, "change_request")], 0, 3)
    assert t.to is Stage.IMPLEMENT and t.count_pr_round and "/agent fix" in (t.feedback or "")
    assert after_pr_poll("active", [CommentOutcome(c, "change_request")], 3, 3).park_reason \
        is ParkReason.PR_ROUNDS


def test_apply_transition_counters_and_park() -> None:
    item = apply_transition(ITEM, Transition(Stage.IMPLEMENT, feedback="fb", count_attempt=True))
    assert item.stage is Stage.IMPLEMENT and item.attempt == 1 and item.data["feedback"] == "fb"
    item = apply_transition(item, Transition(Stage.IMPLEMENT, count_pr_round=True))
    assert item.pr_rounds == 1 and item.attempt == 0
    parked = apply_transition(item, park(ParkReason.RED, "red"))
    assert parked.stage is Stage.PARKED and parked.parked_from is Stage.IMPLEMENT
    assert parked.data["park_note"] == "red"


def test_requeue_gate_park_advances() -> None:
    parked = apply_transition(ITEM, park(ParkReason.NEEDS_HUMAN, "unclear"))
    assert requeue(parked).stage is Stage.PLAN
    plan_parked = apply_transition(Item(1, "t", "x", "b", Stage.PLAN, replans=1),
                                   park(ParkReason.PLAN_REJECTED, "no"))
    assert requeue(plan_parked).stage is Stage.IMPLEMENT
    review_parked = apply_transition(Item(1, "t", "x", "b", Stage.REVIEW, attempt=3),
                                     park(ParkReason.NEEDS_HUMAN, "blocking"))
    assert requeue(review_parked).stage is Stage.PR_OPEN


def test_requeue_non_gate_park_resumes_with_fresh_counters() -> None:
    parked = apply_transition(Item(1, "t", "x", "b", Stage.VERIFY, attempt=3, infra_failures=2),
                              park(ParkReason.RED, "red"))
    item = requeue(parked)
    assert item.stage is Stage.VERIFY and item.attempt == 0 and item.infra_failures == 0
    assert item.park_reason is None and item.parked_from is None


def test_requeue_budget_resets_budget_offset() -> None:
    from agent_sdlc.types import Usage
    parked = apply_transition(Item(1, "t", "x", "b", Stage.IMPLEMENT, usage=Usage(5, 900, 100)),
                              park(ParkReason.BUDGET, "b"))
    assert requeue(parked).data["budget_offset"] == 1000


# --- final review fix wave ---------------------------------------------------------------


def test_i5_pr_rounds_park_keeps_feedback_and_requeues_to_implement() -> None:
    c = PrComment(1, 1, "Brian", "/agent rename foo")
    t = after_pr_poll("active", [CommentOutcome(c, "change_request")], 3, 3)
    assert t.park_reason is ParkReason.PR_ROUNDS and "/agent rename foo" in (t.feedback or "")
    waiting = Item(1, "t", "x", "b", Stage.AWAITING_HUMAN, pr_rounds=3, pr_id=7)
    parked = apply_transition(waiting, t)
    assert "/agent rename foo" in parked.data["feedback"]
    item = requeue(parked)
    assert item.stage is Stage.IMPLEMENT and item.pr_rounds == 0
    assert "/agent rename foo" in item.data["feedback"] and item.pr_id == 7


def test_m1_confident_review_clears_stale_note() -> None:
    stale = Item(1, "t", "x", "b", Stage.REVIEW, data={"note": "old concern", "plan": "p"})
    item = apply_transition(stale, Transition(Stage.PR_OPEN))
    assert "note" not in item.data and item.data["plan"] == "p"
    kept = apply_transition(stale, Transition(Stage.PR_OPEN, note="new concern"))
    assert kept.data["note"] == "new concern"


def test_c1_requeue_drops_parked_tag_set() -> None:
    parked = apply_transition(ITEM, park(ParkReason.NEEDS_HUMAN, "unclear"))
    parked = replace(parked, data={**parked.data, "parked_tag_set": True})
    assert "parked_tag_set" not in requeue(parked).data


def test_f5_requeue_drops_last_denials() -> None:
    parked = apply_transition(ITEM, park(ParkReason.NEEDS_HUMAN, "unclear"))
    parked = replace(parked, data={**parked.data, "last_denials": [{"tool": "Read"}]})
    assert "last_denials" not in requeue(parked).data


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW])
def test_agent_error_park_is_not_a_gate_and_requeue_retries_stage(stage: Stage) -> None:
    from agent_sdlc.orchestrator.transitions import after_agent_error
    from agent_sdlc.types import GATE_PARKS

    t = after_agent_error(stage, "error_max_turns", 3, 3)
    assert t.park_reason is ParkReason.AGENT_ERROR
    assert ParkReason.AGENT_ERROR not in GATE_PARKS
    parked = apply_transition(Item(1, "t", "x", "b", stage, attempt=3, data={"plan": "p"}), t)
    item = requeue(parked)
    assert item.stage is stage and item.attempt == 0


def test_requeue_manifest_goes_to_verify_and_approves_digest() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.MANIFEST,
              parked_from=Stage.IMPLEMENT, attempt=2,
              data={"manifest_pending": "abc", "manifest_diff": "d", "park_note": "n",
                    "parked_tag_set": True})
    new = requeue(it)
    assert new.stage is Stage.VERIFY and new.attempt == 0 and new.park_reason is None
    assert new.data == {"manifest_approved": "abc"}


def test_requeue_manifest_resumes_at_recorded_stage_keeping_feedback() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.MANIFEST,
              parked_from=Stage.IMPLEMENT,
              data={"manifest_pending": "abc", "manifest_diff": "d", "park_note": "n",
                    "manifest_resume": "implement", "feedback": "apply the change request"})
    new = requeue(it)
    assert new.stage is Stage.IMPLEMENT
    assert new.data == {"manifest_approved": "abc", "feedback": "apply the change request"}


def test_requeue_manifest_without_resume_defaults_to_verify() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.MANIFEST,
              parked_from=Stage.PR_OPEN, data={"manifest_pending": "abc"})
    assert requeue(it).stage is Stage.VERIFY


def test_agent_error_does_not_count_toward_verify_retries() -> None:
    from agent_sdlc.orchestrator.transitions import after_agent_error

    t = after_agent_error(Stage.PLAN, "error_max_turns", 0, 3)
    assert t == Transition(Stage.PLAN)
    assert apply_transition(Item(1, "t", "x", "b", Stage.PLAN), t).attempt == 0


def test_requeue_drops_agent_errors() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.AGENT_ERROR,
              parked_from=Stage.PLAN, data={"agent_errors": {"plan": 4}, "plan": "p"})
    assert requeue(it).data == {"plan": "p"}
