from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    Role,
    implementer_prompt,
    planner_prompt,
    reviewer_prompt,
)
from agent_sdlc.decisions.gates import triage_state, work_item_text
from agent_sdlc.orchestrator.events import agent_events, check_event
from agent_sdlc.orchestrator.reporting import (
    QUESTION_REPLY,
    UNCERTAIN_REPLY,
    commit_message,
    plan_comment_html,
    pr_body,
    pr_title,
)
from agent_sdlc.orchestrator.slots import SessionSlots
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
from agent_sdlc.ports import AgentRunner, DeciderPort, ForgePort, WorkspacePort
from agent_sdlc.store import LabelInput
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import (
    AgentResult,
    CommandResult,
    Decision,
    EventInput,
    Item,
    ParkReason,
    PrComment,
    Stage,
    Usage,
    WorkItem,
)
from agent_sdlc.workspaces import MergeConflict

log = logging.getLogger(__name__)
_KEPT_DENIALS = 10     # denials kept on the item for the PR body
_PARK_DENIALS = 5      # denials quoted in a park note/comment
_MANIFEST_DIFF_CHARS = 6000
_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json")


@dataclass
class StepResult:
    transition: Transition
    usage: Usage = Usage()
    decisions: list[tuple[Decision, dict[str, Any]]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    pr_id: int | None = None
    labels: list[LabelInput] = field(default_factory=list)
    events: list[EventInput] = field(default_factory=list)
    # PR replies the scheduler sends after committing the step (spec §3.3)
    replies: list[tuple[PrComment, str]] = field(default_factory=list)


def _logged(
    ds: dict[str, Decision], state: dict[str, Any]
) -> list[tuple[Decision, dict[str, Any]]]:
    return [(d, state) for d in ds.values()]


def _check_dict(r: CommandResult) -> dict[str, Any]:
    d = asdict(r)
    d["output"] = r.output[-2000:]
    return d


def _denial_dicts(res: AgentResult) -> list[dict[str, str]]:
    return [{"role": res.role, "tool": d.tool, "category": d.category, "reason": d.reason,
             "input": d.input} for d in res.denials]


class StageExecutor:
    def __init__(self, *, target: TargetConfig, forge: ForgePort, decider: DeciderPort,
                 runner: AgentRunner, workspaces: WorkspacePort, path_policy: PathPolicy,
                 decisions_for: Callable[[int], list[Decision]],
                 traces: Path | None = None,
                 clock: Callable[[], datetime] | None = None,
                 slots: SessionSlots | None = None,
                 should_stop: Callable[[], bool] = lambda: False) -> None:
        self._t = target
        self._forge = forge
        self._decider = decider
        self._runner = runner
        self._ws = workspaces
        self._pp = path_policy
        self._mp = PathPolicy(target.policy.manifest_paths)
        self._decisions_for = decisions_for
        self._traces = traces
        self._clock = clock or (lambda: datetime.now(UTC))
        self._slots = slots
        self._should_stop = should_stop

    async def run(self, item: Item) -> StepResult:
        handlers = {
            Stage.TRIAGE: self._triage, Stage.PLAN: self._plan, Stage.IMPLEMENT: self._implement,
            Stage.VERIFY: self._verify, Stage.REVIEW: self._review, Stage.PR_OPEN: self._pr_open,
            Stage.AWAITING_HUMAN: self._awaiting,
        }
        return await handlers[item.stage](item)

    def _turns(self, stage: str) -> int:
        return self._t.limits.max_turns.get(stage, 30)

    # tracing & agents ------------------------------------------------------
    def _trace_path(self, item: Item, name: str, ext: str) -> Path | None:
        if self._traces is None:
            return None
        ts = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S")
        return (self._traces / item.target / str(item.external_id)
                / f"{ts}-{item.stage.value}-a{item.attempt}-{name}.{ext}")

    async def _run_agent(self, item: Item, role: Role, prompt: str, wt: Path,
                         stage: str) -> tuple[AgentResult, list[EventInput], dict[str, Any]]:
        spent = item.usage.tokens - int(item.data.get("budget_offset", 0))
        budget = max(self._t.limits.max_item_tokens - spent, 0)
        hold = (self._slots.hold(self._should_stop) if self._slots is not None
               else contextlib.nullcontext())
        with hold:
            res = await self._runner.run(role, prompt, wt, self._turns(stage),
                                         trace=self._trace_path(item, role.name, "jsonl"),
                                         token_budget=budget)
        res = replace(res, role=role.name)
        log.info("agent %s: %s turns, %s tokens, %s denied%s", role.name, res.usage.turns,
                 f"{res.usage.tokens:,}", len(res.denials),
                 f", escalated {res.escalated}" if res.escalated else "")
        return res, agent_events(res), self._denial_data(item, res)

    @staticmethod
    def _denial_data(item: Item, res: AgentResult) -> dict[str, Any]:
        """Item-wide denial counts per role, the first 10 denials (PR body) and this session's
        denials (park comments). Empty when the session had none."""
        if not res.denials:
            return {}
        new = _denial_dicts(res)
        counts = dict(item.data.get("denial_counts") or {})
        counts[res.role] = counts.get(res.role, 0) + len(new)
        kept = list(item.data.get("denials") or [])
        kept += new[: max(0, _KEPT_DENIALS - len(kept))]
        return {"denial_counts": counts, "denials": kept}

    def _stopped(self, res: AgentResult, events: list[EventInput],
                 data: dict[str, Any]) -> StepResult | None:
        """The runner interrupted the session (spec §5.3, §5.4)."""
        if res.escalated is None:
            return None
        if res.escalated == "budget":
            t = park(ParkReason.BUDGET,
                     f"The {res.role} session was stopped at the item token budget of "
                     f"{self._t.limits.max_item_tokens:,} tokens.")
        else:
            quoted = "\n".join(f"- {d.tool} [{d.category}]: {d.reason}"
                               + (f" {d.input}" if d.input else "")
                               for d in res.denials[-_PARK_DENIALS:])
            t = park(ParkReason.POLICY,
                     f"The {res.role} agent was stopped after blocked tool calls "
                     f"({res.escalated}):\n{quoted}")
        data = {**data, "last_denials": _denial_dicts(res)[-_PARK_DENIALS:]}
        return StepResult(t, res.usage, events=events, data=data)

    def _agent_failed(self, item: Item, res: AgentResult, events: list[EventInput],
                      data: dict[str, Any]) -> StepResult | None:
        """An agent error result (max turns, execution error) is a failed attempt (I2)."""
        if not res.is_error:
            return None
        t = after_agent_error(item.stage, res.error, item.attempt,
                              self._t.limits.max_verify_retries)
        return StepResult(t, res.usage, events=events, data=data)

    # policy, manifests & install --------------------------------------------
    def _agent_owned(self, item: Item, wt: Path, paths: list[str]) -> list[str]:
        """`paths` minus those still exactly as a human pushed them (spec §3.1): policy, the
        manifest gate and the diff limit judge only the agent's changes."""
        human: dict[str, str] = item.data.get("human_blobs") or {}
        theirs = [p for p in paths if p in human]
        if not theirs:
            return paths
        now = self._ws.blobs(wt, theirs)
        return [p for p in paths if p not in human or now[p] != human[p]]

    def _agent_diff_lines(self, item: Item, wt: Path) -> int:
        if not item.data.get("human_blobs"):
            return self._ws.diff_lines(wt)
        return self._ws.diff_lines(wt, self._agent_owned(item, wt, self._ws.changed_files(wt)))

    def _incorporate(self, item: Item, wt: Path) -> Item | StepResult:
        """Merge commits a human pushed to the PR branch, then recompute item.data["human_blobs"]
        from the branch's full history (spec §3.1): derived fresh each round rather than kept as
        stored state, so an interrupted step never leaves it stale and a human edit that git
        auto-merged into a file the agent also touched is still judged by its own blob. A
        conflict parks for a human."""
        try:
            self._ws.incorporate_remote(wt, item.branch)
        except MergeConflict as e:
            return StepResult(park(
                ParkReason.NEEDS_HUMAN,
                "PR branch has diverged and could not be merged: " + ", ".join(e.files)))
        human = self._ws.human_blobs(wt)
        if human == (item.data.get("human_blobs") or {}):
            return item
        return replace(item, data={**item.data, "human_blobs": human})

    def _policy_park(self, item: Item, wt: Path, when: str) -> StepResult | None:
        """Protected-path and diff-limit checks, re-run wherever a park (e.g. an unapproved
        manifest) may have let a human requeue past them without a fresh recheck."""
        changed = self._agent_owned(item, wt, self._ws.changed_files(wt))
        violations = self._pp.violations(changed)
        if violations:
            return StepResult(park(
                ParkReason.POLICY, f"{when} found protected paths: " + ", ".join(violations)))
        lines, limit = self._agent_diff_lines(item, wt), self._t.policy.max_diff_lines
        if lines > limit:
            return StepResult(park(
                ParkReason.POLICY,
                f"{when}: the diff is {lines} lines, over the {limit}-line limit."))
        return None

    def _manifest_gate(self, item: Item, wt: Path, resume: Stage) -> StepResult | None:
        """Park when the branch changes a manifest in a way no human has approved (§5.2).
        `resume` is where an approving requeue goes next."""
        files = self._mp.violations(self._agent_owned(item, wt, self._ws.changed_files(wt)))
        if not files:
            return None
        digest = self._ws.blob_digest(wt, files)
        if digest == item.data.get("manifest_approved"):
            return None
        log.warning("unapproved manifest change: %s", ", ".join(files))
        # Full hunks for manifests, only a stat for lockfiles when both changed, so a large
        # lockfile change cannot push the package.json change out of the approval diff (I3).
        # A lockfile-only change gets its own full diff (F1): a stat alone hides what changed.
        locks = [f for f in files if f.endswith(_LOCKFILES)]
        manifests = [f for f in files if f not in locks]
        if manifests:
            diff = self._ws.diff(wt, paths=manifests) + (
                self._ws.diff(wt, paths=locks, stat=True) if locks else "")
        else:
            diff = self._ws.diff(wt, paths=locks) if locks else ""
        if len(diff) > _MANIFEST_DIFF_CHARS:
            diff = diff[:_MANIFEST_DIFF_CHARS] + "\n…(truncated)"
        return StepResult(
            park(ParkReason.MANIFEST,
                 f"Dependency manifests changed: {', '.join(files)}. A human must approve "
                 f"them before install and {resume.value} run."),
            data={"manifest_pending": digest, "manifest_diff": diff,
                  "manifest_resume": resume.value})

    def _ensure_installed(self, item: Item, wt: Path
                          ) -> tuple[StepResult | None, dict[str, Any], list[EventInput]]:
        """Install when the manifests at HEAD differ from the last install. Callers run
        _manifest_gate first, so an unapproved manifest is never installed."""
        digest = self._ws.blob_digest(wt, self._mp.violations(self._ws.tracked_files(wt)))
        if digest == item.data.get("installed_digest"):
            return None, {}, []
        inst = self._ws.install(wt, log=self._trace_path(item, "install", "log"))
        events = [check_event(inst)]
        if not inst.ok:
            return (StepResult(park(ParkReason.INFRA, f"Install failed:\n{inst.output[-2000:]}"),
                               events=events), {}, events)
        return None, {"installed_digest": digest}, events

    # stages ----------------------------------------------------------------
    async def _triage(self, item: Item) -> StepResult:
        state = triage_state(self._forge.get_item(item.external_id))
        ds = self._decider.decide("triage", state)
        return StepResult(after_triage(ds), decisions=_logged(ds, state))

    async def _plan(self, item: Item) -> StepResult:
        wi = self._forge.get_item(item.external_id)
        wt = self._ws.create(item.external_id, item.branch)
        res, events, data = await self._run_agent(
            item, PLANNER, planner_prompt(wi, item.data.get("feedback")), wt, "plan")
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        state = {"work_item": work_item_text(wi), "plan": res.text[:6000]}
        ds = self._decider.decide("plan", state)
        return StepResult(after_plan(ds, item.replans), res.usage, _logged(ds, state),
                          {"plan": res.text, "feedback": None, **data}, events=events)

    async def _implement(self, item: Item) -> StepResult:
        wi = self._forge.get_item(item.external_id)
        wt = self._ws.create(item.external_id, item.branch)
        self._ws.reset(wt)
        human: dict[str, Any] = {}
        if item.pr_id:
            merged = self._incorporate(item, wt)
            if isinstance(merged, StepResult):
                return merged
            if merged is not item:
                item, human = merged, {"human_blobs": merged.data["human_blobs"]}
        res = await self._implement_changes(item, wi, wt)
        return replace(res, data={**human, **res.data}) if human else res

    async def _implement_changes(self, item: Item, wi: WorkItem, wt: Path) -> StepResult:
        # Resume at implement: pending feedback (e.g. a PR change request) must still apply (I2).
        if gate := self._manifest_gate(item, wt, Stage.IMPLEMENT):
            return gate
        failed_install, inst_data, events = self._ensure_installed(item, wt)
        if failed_install:
            return failed_install
        before = self._ws.head(wt)
        res, agent_evs, data = await self._run_agent(
            item, IMPLEMENTER, implementer_prompt(wi, str(item.data.get("plan", "")),
                                                  item.data.get("feedback")),
            wt, "implement")
        events += agent_evs
        data.update(inst_data)
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        self._ws.commit(wt, commit_message(wi, item.pr_rounds, self._forge.item_ref(wi.id)))
        if item.data.get("feedback") and self._ws.head(wt) == before:
            # Nothing new for the feedback: don't report the request as handled (spec §3.2).
            return StepResult(park(ParkReason.NEEDS_HUMAN,
                                   "The implementer made no changes for the feedback."),
                              res.usage, data=data, events=events)
        files = self._ws.changed_files(wt)
        t = after_implement(self._pp.violations(self._agent_owned(item, wt, files)),
                            bool(files), self._agent_diff_lines(item, wt),
                            self._t.policy.max_diff_lines)
        if t.to is Stage.VERIFY and (gate := self._manifest_gate(item, wt, Stage.VERIFY)):
            return replace(gate, usage=res.usage, events=events,
                           data={"feedback": None, **data, **gate.data})
        return StepResult(t, res.usage, data={"feedback": None, **data}, events=events)

    async def _verify(self, item: Item) -> StepResult:
        wt = self._ws.create(item.external_id, item.branch)
        if policy := self._policy_park(item, wt, "Verify"):
            return policy
        if gate := self._manifest_gate(item, wt, Stage.VERIFY):
            return gate
        failed_install, inst_data, events = self._ensure_installed(item, wt)
        if failed_install:
            return failed_install
        results = self._ws.run_checks(
            wt, log_for=lambda name: self._trace_path(item, name, "log"))
        events += [check_event(r) for r in results]
        for r in results:
            log.info("check %s: exit %s (%ss)", r.name, r.exit_code, r.duration_s)
        if self._ws.commit(wt, "style: apply lint fixes"):
            changed = self._agent_owned(item, wt, self._ws.changed_files(wt))
            violations = self._pp.violations(changed)
            if violations:
                return StepResult(park(
                    ParkReason.POLICY,
                    "Lint fixes touched protected paths: " + ", ".join(violations)),
                    events=events, data=inst_data)
            if gate := self._manifest_gate(item, wt, Stage.VERIFY):
                return replace(gate, events=events, data={**inst_data, **gate.data})
            lines, limit = self._agent_diff_lines(item, wt), self._t.policy.max_diff_lines
            if lines > limit:  # M9
                return StepResult(park(
                    ParkReason.POLICY,
                    f"After lint fixes the diff is {lines} lines, over the {limit}-line limit."),
                    events=events, data=inst_data)
        t = after_verify(results, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, data={"checks": [_check_dict(r) for r in results], **inst_data},
                          events=events)

    async def _review(self, item: Item) -> StepResult:
        wi = self._forge.get_item(item.external_id)
        wt = self._ws.create(item.external_id, item.branch)
        checks = [CommandResult(**c) for c in item.data.get("checks", [])]
        plan = str(item.data.get("plan", ""))
        res, events, data = await self._run_agent(
            item, REVIEWER, reviewer_prompt(wi, plan, self._ws.diff(wt), checks), wt, "review")
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        state = {"work_item": work_item_text(wi, 3000), "plan": plan[:3000],
                 "review_notes": res.text[:6000]}
        ds = self._decider.decide("review", state)
        t = after_review(ds, res.text, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, res.usage, _logged(ds, state), {"review_notes": res.text, **data},
                          events=events)

    async def _pr_open(self, item: Item) -> StepResult:
        wi = self._forge.get_item(item.external_id)
        wt = self._ws.create(item.external_id, item.branch)
        if policy := self._policy_park(item, wt, "Pre-push check"):
            return policy
        if gate := self._manifest_gate(item, wt, Stage.VERIFY):
            return gate
        self._forge.push_branch(wt, item.branch)
        ref = self._forge.item_ref(wi.id)
        body = pr_body(item, wi, ref, self._decisions_for(item.id), item.data.get("checks", []),
                       str(item.data.get("review_notes", "")))
        events: list[EventInput] = []
        if item.pr_id:
            self._forge.update_pr(item.pr_id, body, wi.id)
            pr_id = item.pr_id
        else:
            pr_id = self._forge.create_pr(item.branch, pr_title(wi, ref), body, wi.id)
            if pr_id:  # dry-run returns 0: there is no PR to point at (M6)
                events += self._comment_plan(wi.id, str(item.data.get("plan", "")), pr_id)
        return StepResult(Transition(Stage.AWAITING_HUMAN), pr_id=pr_id, events=events)

    def _comment_plan(self, wi_id: int, plan: str, pr_id: int) -> list[EventInput]:
        """Best effort: once the PR exists, its id must be stored, so a failed comment must not
        fail the step and make a retry open a second PR (R2). The PR body carries the plan."""
        try:
            self._forge.comment_item(wi_id, plan_comment_html(plan, self._forge.pr_ref(pr_id)))
        except (httpx.HTTPError, ForgeError, OSError) as e:
            log.warning("plan comment failed for PR %s: %s", pr_id, e)
            return [EventInput("plan_comment_failed", {"pr_id": pr_id, "error": str(e)[:500]})]
        return []

    async def _awaiting(self, item: Item) -> StepResult:
        assert item.pr_id is not None
        status = self._forge.pr_status(item.pr_id)
        seen = list(item.data.get("seen_comments", []))
        outcomes: list[CommentOutcome] = []
        logged: list[tuple[Decision, dict[str, Any]]] = []
        labels: list[LabelInput] = []
        events: list[EventInput] = []
        replies: list[tuple[PrComment, str]] = []
        for c in self._forge.pr_comments(item.pr_id):
            if c.key in seen:
                continue
            seen.append(c.key)
            state = {"comment": c.content[:3000]}
            ds = self._decider.decide("comment", state)
            logged += _logged(ds, state)
            intent = classify_comment(c, ds)
            events.append(EventInput("pr_comment", {"thread_id": c.thread_id,
                                                    "comment_id": c.comment_id,
                                                    "author": c.author, "intent": intent}))
            slash = c.content.strip().lower().startswith("/agent")
            if slash or c.changes_requested:
                d = ds["comment_intent"]
                labels.append(LabelInput("comment", "comment_intent", d.raw_probs,
                                         "change_request",
                                         "slash_command" if slash else "changes_requested",
                                         target=item.target))
            outcomes.append(CommentOutcome(c, intent))
            if intent in ("question", "uncertain") and status == "active":
                reply = QUESTION_REPLY if intent == "question" else UNCERTAIN_REPLY
                replies.append((c, reply))
        t = after_pr_poll(status, outcomes, item.pr_rounds, self._t.limits.max_pr_rounds)
        return StepResult(t, decisions=logged, data={"seen_comments": seen}, labels=labels,
                          events=events, replies=replies)
