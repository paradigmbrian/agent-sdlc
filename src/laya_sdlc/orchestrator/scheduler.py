from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from laya_sdlc.adapters.ado import AdoError
from laya_sdlc.orchestrator.reporting import park_comment_html
from laya_sdlc.orchestrator.stages import StepResult
from laya_sdlc.orchestrator.transitions import APPROVAL_LABELS, apply_transition, park, requeue
from laya_sdlc.ports import AdoPort, WorkspacePort
from laya_sdlc.store import LabelInput, Store
from laya_sdlc.targets import TargetConfig
from laya_sdlc.types import (
    ACTIVE_STAGES,
    GATE_PARKS,
    AgentInfraError,
    AgentInterrupted,
    Item,
    ParkReason,
    Stage,
    Usage,
    UsageLimitError,
)
from laya_sdlc.workspaces import GitError, slugify

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
        if self._paused(now):
            return
        self._intake()
        self._requeue_untagged()
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
        self._store.commit_step(new, [], Usage(), self._clock().date(), labels)
        try:
            self._ado.set_tag(item.id, self._t.ado.parked_tag, False)
        except _INFRA_ERRORS as e:
            log.warning("clearing parked tag failed for #%s (idempotent cleanup): %s", item.id, e)
        log.info("requeued #%s -> %s", item.id, new.stage)
        return new

    # one step --------------------------------------------------------------
    async def _step(self, item: Item, now: datetime) -> None:
        retry_after = item.data.get("retry_after")
        if retry_after and datetime.fromisoformat(str(retry_after)) > now:
            return
        try:
            res = await self._executor.run(item)
        except UsageLimitError as e:
            # Tokens spent before the limit hit still count toward item and daily budgets.
            self._store.commit_step(replace(item, usage=item.usage + e.usage), [], e.usage,
                                    now.date(), [])
            until = e.reset_at or (now + _DEFAULT_PAUSE)
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
        self._store.commit_step(new, res.decisions, res.usage, now.date(), labels)
        self._side_effects(new)

    def _infra_failure(self, item: Item, now: datetime, err: Exception) -> None:
        n = item.infra_failures + 1
        log.warning("infra failure %s on #%s: %s", n, item.id, err)
        if n >= _MAX_INFRA_FAILURES:
            new = apply_transition(replace(item, infra_failures=n),
                                   park(ParkReason.INFRA, f"{type(err).__name__}: {err}"))
            self._store.save(new)
            self._side_effects(new)
            return
        retry = now + timedelta(minutes=2 ** n)
        self._store.save(replace(item, infra_failures=n,
                                 data={**item.data, "retry_after": retry.isoformat()}))

    def _park_side_effects(self, item: Item) -> None:
        """Tag first, and record that the tag is set, before commenting: only a confirmed tag
        makes its later removal mean "a human approved" (C1)."""
        try:
            self._ado.set_tag(item.id, self._t.ado.parked_tag, True)
        except _INFRA_ERRORS:
            log.exception("setting the parked tag failed for #%s; will retry", item.id)
            return
        self._store.save(replace(item, data={**item.data, "parked_tag_set": True}))
        try:
            self._ado.comment_work_item(item.id, park_comment_html(item))
            if item.pr_id:
                self._ado.comment_pr(item.pr_id, f"laya-sdlc parked this item "
                                     f"({item.park_reason}): {item.data.get('park_note', '')}")
        except _INFRA_ERRORS:
            log.exception("park comments failed for #%s", item.id)

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
