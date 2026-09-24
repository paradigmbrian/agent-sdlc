from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from agent_sdlc.adapters.ado import AdoError
from agent_sdlc.logctx import log_context
from agent_sdlc.orchestrator.events import agent_events, transition_event
from agent_sdlc.orchestrator.reporting import park_comment_html
from agent_sdlc.orchestrator.stages import StepResult
from agent_sdlc.orchestrator.transitions import APPROVAL_LABELS, apply_transition, park, requeue
from agent_sdlc.ports import AdoPort, WorkspacePort
from agent_sdlc.store import LabelInput, Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import (
    ACTIVE_STAGES,
    GATE_PARKS,
    AgentInfraError,
    AgentInterrupted,
    AgentResult,
    EventInput,
    Item,
    ParkReason,
    Stage,
    Usage,
    UsageLimitError,
)
from agent_sdlc.workspaces import GitError, slugify

log = logging.getLogger(__name__)
_INFRA_ERRORS = (httpx.HTTPError, GitError, AdoError, OSError)
_MAX_INFRA_FAILURES = 3
_DEFAULT_PAUSE = timedelta(minutes=30)


class Executor(Protocol):
    async def run(self, item: Item) -> StepResult: ...


def _merge(data: dict[str, object], updates: dict[str, object]) -> dict[str, object]:
    out = dict(data)
    for k, v in updates.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def _partial_events(partial: object) -> list[EventInput]:
    """Events for the part of an agent session that ran before an exception (spec §3)."""
    return agent_events(partial) if isinstance(partial, AgentResult) else []


class Scheduler:
    def __init__(self, *, target: TargetConfig, store: Store, executor: Executor, ado: AdoPort,
                 workspaces: WorkspacePort, clock: Callable[[], datetime] | None = None) -> None:
        self._t = target
        self._store = store
        self._executor = executor
        self._ado = ado
        self._ws = workspaces
        self._clock = clock or (lambda: datetime.now().astimezone())

    # loop ------------------------------------------------------------------
    async def run_forever(self, poll_s: int = 60) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(poll_s)

    def _paused(self, now: datetime) -> bool:
        if self._store.get_flag("paused") == "1":
            return True
        until = self._store.get_flag("paused_until")
        return bool(until and datetime.fromisoformat(until) > now)

    def _agent_work_allowed(self, now: datetime) -> bool:
        lim = self._t.limits
        if lim.run_window and not lim.run_window.contains(now.time()):
            return False
        used = self._store.daily_usage(now.date())
        return used.turns < lim.max_daily_agent_turns and used.tokens < lim.max_daily_tokens

    async def tick(self) -> None:
        now = self._clock()
        self._store.set_flag("last_tick", now.isoformat())
        if self._paused(now):
            return
        self._intake()
        self._requeue_untagged()
        self._warn_stale(now)
        for item in self._store.items(self._t.name, [Stage.AWAITING_HUMAN]):
            await self._step(item, now)
        if not self._agent_work_allowed(now):
            return
        active = self._store.items(self._t.name, ACTIVE_STAGES)
        active.sort(key=lambda i: i.stage is Stage.TRIAGE)  # in-flight work first (stable)
        for item in active[: self._t.limits.max_concurrent_items]:
            if self._paused(self._clock()):
                return
            await self._step(item, now)

    def _warn_stale(self, now: datetime) -> None:
        limit = timedelta(minutes=self._t.limits.stale_after_minutes)
        for item in self._store.items(self._t.name, ACTIVE_STAGES):
            last = self._store.last_event_ts(item.id)
            if last is not None and now - last > limit:
                with log_context(item.id, item.stage.value):
                    log.warning("stale: no event for %s", now - last)

    # intake & requeue ------------------------------------------------------
    def _intake(self) -> None:
        try:
            intake = self._ado.list_intake()
        except _INFRA_ERRORS as e:
            log.warning("intake failed; skipping this tick: %s", e)
            return
        for wi in intake:
            if self._t.ado.parked_tag in wi.tags:
                continue
            branch = f"{self._t.ado.branch_prefix}{wi.id}-{slugify(wi.title)}"
            if self._store.add_item(self._t.name, wi, branch):
                self._store.add_event("intake", {"title": wi.title, "branch": branch},
                                      item=self._store.get(wi.id))
                log.info("intake: #%s %s", wi.id, wi.title)

    def _requeue_untagged(self) -> None:
        for item in self._store.items(self._t.name, [Stage.PARKED]):
            if not item.data.get("parked_tag_set"):
                # The park tag was never confirmed set, so a missing tag is not a human
                # approval: retry the park side effects instead (C1).
                self._park_side_effects(item)
                continue
            try:
                tagged = self._ado.has_tag(item.id, self._t.ado.parked_tag)
            except _INFRA_ERRORS as e:
                log.warning("has_tag failed for #%s; leaving parked this tick: %s", item.id, e)
                continue
            if not tagged:
                self.requeue_item(item.id)

    def _approval_labels(self, item_id: int, stages: tuple[Stage, ...],
                         source: str) -> list[LabelInput]:
        """Labels implied by a human approving the gates at `stages` (requeue or merge)."""
        out: list[LabelInput] = []
        for stage in stages:
            gate, golds = APPROVAL_LABELS[stage]
            latest = {d.question: d for d in self._store.decisions_for(item_id, gate)}
            out += [LabelInput(gate, q, latest[q].raw_probs, gold, source)
                    for q, gold in golds.items() if q in latest]
        return out

    def requeue_item(self, item_id: int) -> Item:
        item = self._store.get(item_id)
        new = requeue(item)
        labels: list[LabelInput] = []
        if item.park_reason in GATE_PARKS and item.parked_from in APPROVAL_LABELS:
            labels = self._approval_labels(item.id, (item.parked_from,), "human_requeue")
        event = EventInput("requeue", {
            "from_reason": item.park_reason.value if item.park_reason else None,
            "to": new.stage.value, "approved": bool(labels)})
        self._store.commit_step(new, [], Usage(), self._clock().date(), labels,
                                events=[event], at=item)
        try:
            self._ado.set_tag(item.id, self._t.ado.parked_tag, False)
        except _INFRA_ERRORS as e:
            log.warning("clearing parked tag failed for #%s (idempotent cleanup): %s", item.id, e)
        log.info("requeued #%s -> %s", item.id, new.stage)
        return new

    # one step --------------------------------------------------------------
    async def _step(self, item: Item, now: datetime) -> None:
        with log_context(item.id, item.stage.value):
            await self._run_step(item, now)

    async def _run_step(self, item: Item, now: datetime) -> None:
        retry_after = item.data.get("retry_after")
        if retry_after and datetime.fromisoformat(str(retry_after)) > now:
            return
        try:
            res = await self._executor.run(item)
        except UsageLimitError as e:
            # Tokens spent before the limit hit still count toward item and daily budgets.
            until = e.reset_at or (now + _DEFAULT_PAUSE)
            events = _partial_events(e.partial) + [
                EventInput("usage_limit", {"until": until.isoformat()})]
            self._store.commit_step(replace(item, usage=item.usage + e.usage), [], e.usage,
                                    now.date(), [], events=events, at=item)
            self._store.set_flag("paused_until", until.isoformat())
            log.warning("usage limit hit; pausing until %s", until)
            return
        except AgentInterrupted:
            return
        except (*_INFRA_ERRORS, AgentInfraError) as e:
            self._infra_failure(item, now, e)
            return
        except Exception as e:  # any per-item error backs off and eventually parks (I1)
            log.exception("step failed for #%s", item.id)
            self._infra_failure(item, now, e)
            return
        new = apply_transition(item, res.transition)
        new = replace(new, usage=item.usage + res.usage, infra_failures=0,
                      pr_id=res.pr_id if res.pr_id is not None else item.pr_id,
                      data=_merge(new.data, {**res.data, "retry_after": None}))
        offset = int(new.data.get("budget_offset", 0))
        if new.stage is not Stage.PARKED and \
                new.usage.tokens - offset > self._t.limits.max_item_tokens:
            new = apply_transition(replace(new, stage=item.stage), park(
                ParkReason.BUDGET, f"Item used {new.usage.tokens - offset:,} tokens, over "
                f"the {self._t.limits.max_item_tokens:,} limit."))
        labels = list(res.labels)
        if new.stage is Stage.DONE:  # a merge approves the plan and review gates
            labels += self._approval_labels(item.id, (Stage.PLAN, Stage.REVIEW), "merged")
        events = list(res.events)
        if not (item.stage is Stage.AWAITING_HUMAN and new.stage is Stage.AWAITING_HUMAN):
            events.append(transition_event(item, new))
        if new.stage in (Stage.DONE, Stage.CLOSED):
            events.append(EventInput("outcome", {
                "result": "merged" if new.stage is Stage.DONE else "abandoned",
                "pr_id": new.pr_id}))
        self._store.commit_step(new, res.decisions, res.usage, now.date(), labels,
                                events=events, at=item)
        if new.stage is not item.stage:
            log.info("%s -> %s%s", item.stage.value, new.stage.value,
                     f" ({new.park_reason.value})" if new.park_reason else "")
        self._side_effects(new)

    def _infra_failure(self, item: Item, now: datetime, err: Exception) -> None:
        n = item.infra_failures + 1
        log.warning("infra failure %s on #%s: %s", n, item.id, err)
        events = _partial_events(getattr(err, "partial", None)) + [
            EventInput("infra_failure", {"n": n, "error": f"{type(err).__name__}: {err}"[:2000]})]
        if n >= _MAX_INFRA_FAILURES:
            new = apply_transition(replace(item, infra_failures=n),
                                   park(ParkReason.INFRA, f"{type(err).__name__}: {err}"))
            events.append(transition_event(item, new))
            self._store.save(new, events=events, at=item)
            self._side_effects(new)
            return
        retry = now + timedelta(minutes=2 ** n)
        self._store.save(replace(item, infra_failures=n,
                                 data={**item.data, "retry_after": retry.isoformat()}),
                         events=events, at=item)

    def _park_side_effects(self, item: Item) -> None:
        """Tag first, and record that the tag is set, before commenting: only a confirmed tag
        makes its later removal mean "a human approved" (C1)."""
        try:
            self._ado.set_tag(item.id, self._t.ado.parked_tag, True)
        except _INFRA_ERRORS as e:
            log.exception("setting the parked tag failed for #%s; will retry", item.id)
            self._store.add_event("park_side_effect_failed",
                                  {"step": "tag", "error": str(e)[:500]}, item=item)
            return
        self._store.save(replace(item, data={**item.data, "parked_tag_set": True}),
                         events=[EventInput("park_tagged")], at=item)
        try:
            self._ado.comment_work_item(item.id, park_comment_html(item))
            if item.pr_id:
                self._ado.comment_pr(item.pr_id, f"agent-sdlc parked this item "
                                     f"({item.park_reason}): {item.data.get('park_note', '')}")
        except _INFRA_ERRORS as e:
            log.exception("park comments failed for #%s", item.id)
            self._store.add_event("park_side_effect_failed",
                                  {"step": "comment", "error": str(e)[:500]}, item=item)

    def _side_effects(self, item: Item) -> None:
        if item.stage is Stage.PARKED:
            self._park_side_effects(item)
            return
        try:
            if item.stage in (Stage.DONE, Stage.CLOSED):
                self._ws.remove(item.id, item.branch)
                if item.pr_id:
                    self._ado.delete_branch(item.branch)
        except _INFRA_ERRORS:
            log.exception("side effects failed for #%s", item.id)
