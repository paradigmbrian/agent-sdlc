from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    implementer_prompt,
    planner_prompt,
    reviewer_prompt,
)
from agent_sdlc.decisions.gates import triage_state, work_item_text
from agent_sdlc.orchestrator.reporting import (
    QUESTION_REPLY,
    UNCERTAIN_REPLY,
    commit_message,
    plan_comment_html,
    pr_body,
    pr_title,
)
from agent_sdlc.orchestrator.transitions import (
    CommentOutcome,
    Transition,
    after_agent_error,
    after_implement,
    after_plan,
    after_pr_poll,
    after_review,
    after_triage,
    after_verify,
    classify_comment,
    park,
)
from agent_sdlc.policy import PathPolicy
from agent_sdlc.ports import AdoPort, AgentRunner, DeciderPort, WorkspacePort
from agent_sdlc.store import LabelInput
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import AgentResult, CommandResult, Decision, Item, ParkReason, Stage, Usage


@dataclass
class StepResult:
    transition: Transition
    usage: Usage = Usage()
    decisions: list[tuple[Decision, dict[str, Any]]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    pr_id: int | None = None
    labels: list[LabelInput] = field(default_factory=list)


def _logged(
    ds: dict[str, Decision], state: dict[str, Any]
) -> list[tuple[Decision, dict[str, Any]]]:
    return [(d, state) for d in ds.values()]


def _check_dict(r: CommandResult) -> dict[str, Any]:
    d = asdict(r)
    d["output"] = r.output[-2000:]
    return d


class StageExecutor:
    def __init__(self, *, target: TargetConfig, ado: AdoPort, decider: DeciderPort,
                 runner: AgentRunner, workspaces: WorkspacePort, path_policy: PathPolicy,
                 decisions_for: Callable[[int], list[Decision]]) -> None:
        self._t = target
        self._ado = ado
        self._decider = decider
        self._runner = runner
        self._ws = workspaces
        self._pp = path_policy
        self._decisions_for = decisions_for

    async def run(self, item: Item) -> StepResult:
        handlers = {
            Stage.TRIAGE: self._triage, Stage.PLAN: self._plan, Stage.IMPLEMENT: self._implement,
            Stage.VERIFY: self._verify, Stage.REVIEW: self._review, Stage.PR_OPEN: self._pr_open,
            Stage.AWAITING_HUMAN: self._awaiting,
        }
        return await handlers[item.stage](item)

    def _turns(self, stage: str) -> int:
        return self._t.limits.max_turns.get(stage, 30)

    def _agent_failed(self, item: Item, res: AgentResult) -> StepResult | None:
        """An agent error result (max turns, execution error) is a failed attempt (I2)."""
        if not res.is_error:
            return None
        t = after_agent_error(item.stage, res.error, item.attempt,
                              self._t.limits.max_verify_retries)
        return StepResult(t, res.usage)

    async def _triage(self, item: Item) -> StepResult:
        state = triage_state(self._ado.get_work_item(item.id))
        ds = self._decider.decide("triage", state)
        return StepResult(after_triage(ds), decisions=_logged(ds, state))

    async def _plan(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        res = await self._runner.run(PLANNER, planner_prompt(wi, item.data.get("feedback")), wt,
                                     self._turns("plan"))
        if failed := self._agent_failed(item, res):
            return failed
        state = {"work_item": work_item_text(wi), "plan": res.text[:6000]}
        ds = self._decider.decide("plan", state)
        return StepResult(after_plan(ds, item.replans), res.usage, _logged(ds, state),
                          {"plan": res.text, "feedback": None})

    async def _implement(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        self._ws.reset(wt)
        if not item.data.get("installed"):
            inst = self._ws.install(wt)
            if not inst.ok:
                return StepResult(park(ParkReason.INFRA, f"Install failed:\n{inst.output[-2000:]}"))
        res = await self._runner.run(
            IMPLEMENTER, implementer_prompt(wi, str(item.data.get("plan", "")),
                                            item.data.get("feedback")),
            wt, self._turns("implement"))
        if failed := self._agent_failed(item, res):
            return failed
        self._ws.commit(wt, commit_message(wi, item.pr_rounds))
        files = self._ws.changed_files(wt)
        t = after_implement(self._pp.violations(files), bool(files), self._ws.diff_lines(wt),
                            self._t.policy.max_diff_lines)
        return StepResult(t, res.usage, data={"feedback": None, "installed": True,
                                               "denied": list(res.denied)})

    async def _verify(self, item: Item) -> StepResult:
        wt = self._ws.create(item.id, item.branch)
        results = self._ws.run_checks(wt)
        if self._ws.commit(wt, "style: apply lint fixes"):
            violations = self._pp.violations(self._ws.changed_files(wt))
            if violations:
                return StepResult(park(
                    ParkReason.POLICY,
                    "Lint fixes touched protected paths: " + ", ".join(violations)))
            lines, limit = self._ws.diff_lines(wt), self._t.policy.max_diff_lines
            if lines > limit:  # M9
                return StepResult(park(
                    ParkReason.POLICY,
                    f"After lint fixes the diff is {lines} lines, over the {limit}-line limit."))
        t = after_verify(results, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, data={"checks": [_check_dict(r) for r in results]})

    async def _review(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        checks = [CommandResult(**c) for c in item.data.get("checks", [])]
        plan = str(item.data.get("plan", ""))
        res = await self._runner.run(REVIEWER, reviewer_prompt(wi, plan, self._ws.diff(wt), checks),
                                     wt, self._turns("review"))
        if failed := self._agent_failed(item, res):
            return failed
        state = {"work_item": work_item_text(wi, 3000), "plan": plan[:3000],
                 "review_notes": res.text[:6000]}
        ds = self._decider.decide("review", state)
        t = after_review(ds, res.text, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, res.usage, _logged(ds, state), {"review_notes": res.text})

    async def _pr_open(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        violations = self._pp.violations(self._ws.changed_files(wt))
        if violations:
            return StepResult(park(
                ParkReason.POLICY,
                "Pre-push check found protected paths: " + ", ".join(violations)))
        self._ado.push_branch(wt, item.branch)
        body = pr_body(item, wi, self._decisions_for(item.id), item.data.get("checks", []),
                       str(item.data.get("review_notes", "")))
        if item.pr_id:
            self._ado.update_pr(item.pr_id, body)
            pr_id = item.pr_id
        else:
            pr_id = self._ado.create_pr(item.branch, pr_title(wi), body, item.id)
            if pr_id:  # dry-run returns 0: there is no PR to point at (M6)
                self._ado.comment_work_item(
                    item.id, plan_comment_html(str(item.data.get("plan", "")), pr_id))
        return StepResult(Transition(Stage.AWAITING_HUMAN), pr_id=pr_id)

    async def _awaiting(self, item: Item) -> StepResult:
        assert item.pr_id is not None
        status = self._ado.pr_status(item.pr_id)
        seen = list(item.data.get("seen_comments", []))
        outcomes: list[CommentOutcome] = []
        logged: list[tuple[Decision, dict[str, Any]]] = []
        labels: list[LabelInput] = []
        for c in self._ado.pr_comments(item.pr_id):
            if c.key in seen:
                continue
            seen.append(c.key)
            state = {"comment": c.content[:3000]}
            ds = self._decider.decide("comment", state)
            logged += _logged(ds, state)
            intent = classify_comment(c, ds)
            if c.content.strip().lower().startswith("/agent"):
                d = ds["comment_intent"]
                labels.append(LabelInput("comment", "comment_intent", d.raw_probs,
                                         "change_request", "slash_command"))
            outcomes.append(CommentOutcome(c, intent))
            if intent in ("question", "uncertain") and status == "active":
                reply = QUESTION_REPLY if intent == "question" else UNCERTAIN_REPLY
                self._ado.reply_pr(item.pr_id, c.thread_id, c.comment_id, reply)
        t = after_pr_poll(status, outcomes, item.pr_rounds, self._t.limits.max_pr_rounds)
        return StepResult(t, decisions=logged, data={"seen_comments": seen}, labels=labels)
