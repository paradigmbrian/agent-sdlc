from __future__ import annotations

from dataclasses import dataclass, replace

from agent_sdlc.types import (
    GATE_PARKS,
    CommandResult,
    Decision,
    Item,
    ParkReason,
    PrComment,
    Stage,
)


@dataclass(frozen=True)
class Transition:
    to: Stage
    park_reason: ParkReason | None = None
    note: str = ""
    feedback: str | None = None
    count_attempt: bool = False
    count_replan: bool = False
    count_pr_round: bool = False


def park(reason: ParkReason, note: str) -> Transition:
    return Transition(Stage.PARKED, park_reason=reason, note=note)


def _why(d: Decision) -> str:
    flag = ", shadow" if d.shadow else ""
    return f"{d.question}={d.answer} (confidence {d.confidence:.2f}{flag})"


def _is(d: Decision, *answers: str) -> bool:
    return d.actionable and d.answer in answers


def after_triage(ds: dict[str, Decision]) -> Transition:
    problems = []
    if not _is(ds["kind"], "bug", "feature", "chore"):
        problems.append(_why(ds["kind"]))
    if not _is(ds["clarity"], "clear"):
        problems.append(_why(ds["clarity"]))
    if not _is(ds["touches_protected"], "no"):
        problems.append(_why(ds["touches_protected"]))
    if not _is(ds["size"], "small", "medium"):
        problems.append(_why(ds["size"]))
    if problems:
        return park(ParkReason.NEEDS_HUMAN, "Triage needs a human: " + "; ".join(problems))
    return Transition(Stage.PLAN)


def after_plan(ds: dict[str, Decision], replans: int) -> Transition:
    a, b = ds["plan_addresses_item"], ds["plan_scope_ok"]
    if _is(a, "yes") and _is(b, "yes"):
        return Transition(Stage.IMPLEMENT)
    reasons = "; ".join(_why(x) for x in (a, b) if not _is(x, "yes"))
    if (_is(a, "no") or _is(b, "no")) and replans < 1:
        return Transition(Stage.PLAN, feedback=f"The previous plan was rejected: {reasons}. "
                          "Revise it to fully address the work item within scope.",
                          count_replan=True)
    return park(ParkReason.PLAN_REJECTED, f"Plan needs a human: {reasons}")


def after_implement(violations: list[str], changed: bool, diff_lines: int,
                    max_diff_lines: int) -> Transition:
    if violations:
        return park(ParkReason.POLICY, "Changes touch protected paths: " + ", ".join(violations))
    if not changed:
        return park(ParkReason.NEEDS_HUMAN, "The implementer made no changes.")
    if diff_lines > max_diff_lines:
        return park(ParkReason.POLICY,
                    f"Diff is {diff_lines} lines, over the {max_diff_lines}-line limit.")
    return Transition(Stage.VERIFY)


def _failures(results: list[CommandResult]) -> str:
    return "\n\n".join(f"`{r.command}` failed (exit {r.exit_code}):\n```\n{r.output[-3000:]}\n```"
                       for r in results if not r.ok)


def after_verify(results: list[CommandResult], attempt: int, max_retries: int) -> Transition:
    if all(r.ok for r in results):
        return Transition(Stage.REVIEW)
    if attempt >= max_retries:
        return park(ParkReason.RED, f"Checks still failing after {attempt} retries:\n"
                    + _failures(results))
    return Transition(Stage.IMPLEMENT, feedback=_failures(results), count_attempt=True)


def after_review(ds: dict[str, Decision], notes: str, attempt: int,
                 max_retries: int) -> Transition:
    blocking = ds["review_blocking"]
    if _is(blocking, "yes"):
        if attempt >= max_retries:
            return park(ParkReason.NEEDS_HUMAN,
                        f"Reviewer still reports blocking issues after {attempt} retries.")
        return Transition(Stage.IMPLEMENT, feedback=notes, count_attempt=True)
    if _is(blocking, "no"):
        return Transition(Stage.PR_OPEN)
    return Transition(Stage.PR_OPEN, note="Reviewer concerns were not confidently resolved "
                      f"({_why(blocking)}); read the review notes below.")


@dataclass(frozen=True)
class CommentOutcome:
    comment: PrComment
    intent: str  # change_request | question | approval | noise | uncertain


def classify_comment(c: PrComment, ds: dict[str, Decision] | None) -> str:
    if c.changes_requested or c.content.strip().lower().startswith("/agent"):
        return "change_request"
    if ds is None:
        return "uncertain"
    d = ds["comment_intent"]
    return d.answer if d.actionable else "uncertain"


def after_pr_poll(status: str, outcomes: list[CommentOutcome], pr_rounds: int,
                  max_pr_rounds: int) -> Transition:
    if status == "completed":
        return Transition(Stage.DONE)
    if status == "abandoned":
        return Transition(Stage.CLOSED)
    changes = [o.comment for o in outcomes if o.intent == "change_request"]
    if not changes:
        return Transition(Stage.AWAITING_HUMAN)
    feedback = "Reviewer requested changes on the pull request:\n\n" + "\n\n".join(
        f"- {c.author}: {c.content}" for c in changes)
    if pr_rounds >= max_pr_rounds:
        # Keep the triggering request so a human re-queue applies it (I5).
        return Transition(Stage.PARKED, park_reason=ParkReason.PR_ROUNDS,
                          note=f"Reached {max_pr_rounds} PR revision rounds.", feedback=feedback)
    return Transition(Stage.IMPLEMENT, feedback=feedback, count_pr_round=True)


def after_agent_error(stage: Stage, error: str, attempt: int, max_retries: int) -> Transition:
    """The agent session ended with an error result (max turns, execution error): retry the
    stage, and park for a human once the retry budget is spent (I2)."""
    if attempt >= max_retries:
        return park(ParkReason.AGENT_ERROR,
                    f"The {stage.value} agent did not finish after {attempt} retries "
                    f"(agent did not finish: {error or 'error'}).")
    return Transition(stage, count_attempt=True)


def apply_transition(item: Item, t: Transition) -> Item:
    data = dict(item.data)
    if t.feedback is not None:
        data["feedback"] = t.feedback
    if t.note:
        data["park_note" if t.to is Stage.PARKED else "note"] = t.note
    elif t.to is Stage.PR_OPEN:
        data.pop("note", None)  # a confident review clears a stale reviewer-concern note (M1)
    attempt = item.attempt + (1 if t.count_attempt else 0)
    pr_rounds = item.pr_rounds
    if t.count_pr_round:
        pr_rounds, attempt = pr_rounds + 1, 0
    return replace(
        item, stage=t.to,
        park_reason=t.park_reason if t.to is Stage.PARKED else None,
        parked_from=item.stage if t.to is Stage.PARKED else None,
        attempt=attempt, replans=item.replans + (1 if t.count_replan else 0),
        pr_rounds=pr_rounds, data=data)


# Human approval past a gate: where to go next, and which labels the approval implies.
_APPROVE_NEXT = {Stage.TRIAGE: Stage.PLAN, Stage.PLAN: Stage.IMPLEMENT, Stage.REVIEW: Stage.PR_OPEN}
APPROVAL_LABELS: dict[Stage, tuple[str, dict[str, str]]] = {
    Stage.TRIAGE: ("triage", {"clarity": "clear", "touches_protected": "false"}),
    Stage.PLAN: ("plan", {"plan_addresses_item": "true", "plan_scope_ok": "true"}),
    Stage.REVIEW: ("review", {"review_blocking": "false"}),
}


def requeue(item: Item) -> Item:
    if item.stage is not Stage.PARKED or item.parked_from is None:
        raise ValueError(f"item {item.id} is not parked")
    reason, source = item.park_reason, item.parked_from
    to = _APPROVE_NEXT.get(source, source) if reason in GATE_PARKS else source
    if reason is ParkReason.PR_ROUNDS:
        to = Stage.IMPLEMENT  # apply the change request kept in data["feedback"] (I5)
    data = {k: v for k, v in item.data.items()
            if k not in ("park_note", "parked_tag_set", "last_denials")}
    if reason is ParkReason.BUDGET:
        data["budget_offset"] = item.usage.tokens
    if reason is ParkReason.MANIFEST:
        # Approval of exactly the digest the human saw; install and the gate's resume stage run
        # next (spec §5.2). Older parks without manifest_resume go to verify (I2).
        to = Stage(data.pop("manifest_resume", Stage.VERIFY.value))
        pending = data.pop("manifest_pending", None)
        data.pop("manifest_diff", None)
        if pending is not None:
            data["manifest_approved"] = pending
    return replace(item, stage=to, park_reason=None, parked_from=None, attempt=0, replans=0,
                   infra_failures=0,
                   pr_rounds=0 if reason is ParkReason.PR_ROUNDS else item.pr_rounds, data=data)
