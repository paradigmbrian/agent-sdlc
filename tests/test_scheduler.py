import logging
import threading
import time as time_module
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest

from agent_sdlc.config import GlobalLimits
from agent_sdlc.orchestrator.scheduler import Scheduler
from agent_sdlc.orchestrator.stages import StageExecutor, StepResult
from agent_sdlc.orchestrator.transitions import Transition, park
from agent_sdlc.policy import PathPolicy
from agent_sdlc.store import Store
from agent_sdlc.targets import RunWindow, TargetConfig
from agent_sdlc.types import (
    AgentInfraError,
    AgentInterrupted,
    AgentResult,
    EventInput,
    Item,
    ParkReason,
    Stage,
    Usage,
    UsageLimitError,
    WorkItem,
)
from agent_sdlc.workspaces import Workspaces
from tests.fakes import FakeDecider, FakeForge, FakeRunner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "d", "ac", "Bug", ("agent",), "u")


def _it(store: Store, n: int = 5) -> Item:
    return store.get_by_ref("fixture", n)


class ScriptedExecutor:
    def __init__(self, *results: StepResult | Exception) -> None:
        self.results = list(results)
        self.seen: list[Item] = []

    async def run(self, item: Item) -> StepResult:
        self.seen.append(item)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@dataclass
class RaisingAdo(FakeForge):
    """FakeForge that raises an infra error from named methods (fix round 1, R15)."""

    fail: set[str] = field(default_factory=set)

    def list_intake(self) -> list[WorkItem]:
        if "list_intake" in self.fail:
            raise httpx.ConnectError("down")
        return super().list_intake()

    def has_label(self, id: int, label: str) -> bool:
        if "has_label" in self.fail:
            raise httpx.ConnectError("down")
        return super().has_label(id, label)

    def set_label(self, id: int, label: str, present: bool) -> None:
        if "set_label" in self.fail:
            raise httpx.ConnectError("down")
        super().set_label(id, label, present)


@pytest.fixture
def env(tmp_path: Path, target: TargetConfig):  # type: ignore[no-untyped-def]
    store = Store("sqlite://")
    ado = FakeForge()
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    return store, ado, ws, target


def sched(env, executor, now=NOW):  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    return Scheduler(target=target, store=store, executor=executor, forge=ado, workspaces=ws,
                     clock=lambda: now)


async def test_intake_adds_item_with_branch(env) -> None:  # type: ignore[no-untyped-def]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    await sched(env, ex).tick()
    item = env[0].get_by_ref("fixture", 5)
    assert item.branch == "agent/5-add-feature" and item.stage is Stage.PLAN


async def test_step_merges_data_and_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)),
                          StepResult(Transition(Stage.IMPLEMENT), Usage(2, 100, 10),
                                     data={"plan": "p", "feedback": None}))
    s = sched(env, ex)
    await s.tick()
    store.save(replace(_it(store), data={"feedback": "old"}))
    await s.tick()
    item = _it(store)
    assert item.data == {"plan": "p"} and item.usage == Usage(2, 100, 10)
    assert store.daily_usage(NOW.date()) == Usage(2, 100, 10)


async def test_parking_comments_and_tags(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    await sched(env, ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))).tick()
    assert _it(store).stage is Stage.PARKED
    assert "agent:parked" in ado.tags[5]
    assert "unclear" in ado.wi_comments[0][1]


async def test_removing_tag_requeues_with_labels(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    real = StageExecutor(target=target, forge=ado, decider=FakeDecider(shadow={"triage"}),
                         runner=FakeRunner(), workspaces=ws,
                         path_policy=PathPolicy(target.policy.protected_paths),
                         decisions_for=store.decisions_for)
    s = sched(env, real)
    await s.tick()
    assert _it(store).park_reason is ParkReason.NEEDS_HUMAN
    ado.set_label(5, "agent:parked", False)
    s._executor = ScriptedExecutor(StepResult(Transition(Stage.IMPLEMENT)))  # stop after requeue
    await s.tick()
    assert _it(store).stage is Stage.IMPLEMENT  # requeued to PLAN, then stepped once
    assert [g for _, g in store.labels("triage", "clarity")] == ["clear"]
    assert [g for _, g in store.labels("triage", "touches_protected")] == ["false"]


async def test_usage_limit_pauses_without_changing_item(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    reset = NOW + timedelta(hours=2)
    ex = ScriptedExecutor(UsageLimitError("usage limit", reset_at=reset))
    s = sched(env, ex)
    await s.tick()
    assert _it(store).stage is Stage.TRIAGE
    assert store.get_flag("paused_until") == reset.isoformat()
    await s.tick()  # still paused: executor not called again
    assert len(ex.seen) == 1


async def test_kill_switch(env) -> None:  # type: ignore[no-untyped-def]
    env[0].set_flag("paused", "1")
    ex = ScriptedExecutor()
    await sched(env, ex).tick()
    assert ex.seen == []


async def test_infra_errors_retry_then_park(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    err = httpx.ConnectError("down")
    ex = ScriptedExecutor(err, err, err)
    for i in range(3):
        await sched(env, ex, now=NOW + timedelta(hours=i)).tick()
    item = _it(store)
    assert item.stage is Stage.PARKED and item.park_reason is ParkReason.INFRA


async def test_item_budget_parks(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    tight = target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_item_tokens": 100})})
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN), Usage(1, 90, 20)))
    await Scheduler(target=tight, store=store, executor=ex, forge=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    assert _it(store).park_reason is ParkReason.BUDGET


async def test_run_window_blocks_agent_stages_not_polling(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)))
    await Scheduler(target=target, store=store, executor=ex, forge=ado, workspaces=ws,
                    clock=lambda: NOW,
                    limits=GlobalLimits(run_window=RunWindow(start=time(19), end=time(7)))).tick()
    assert [i.external_id for i in ex.seen] == [5]


async def test_concurrency_prefers_in_flight(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    ado.add(replace(WI, id=6, title="Other"))
    store.add_item("fixture", replace(WI, id=6), "agent/6-other")
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.IMPLEMENT))
    ex = ScriptedExecutor(StepResult(Transition(Stage.VERIFY)))
    await sched(env, ex).tick()
    assert [i.external_id for i in ex.seen] == [5]


async def test_done_cleans_up(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, _ = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    ws.create(5, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.AWAITING_HUMAN, pr_id=1))
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    assert _it(store).stage is Stage.DONE
    assert not ws.worktree_path(5).exists()
    assert ado.deleted_branches == ["agent/5-add-feature"]


async def test_intake_error_still_polls_and_steps(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"list_intake"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)),
                          StepResult(Transition(Stage.PLAN)))
    s = Scheduler(target=target, store=store, executor=ex, forge=ado, workspaces=ws,
                 clock=lambda: NOW)
    await s.tick()  # list_intake() raises; intake skipped, rest of tick proceeds
    assert [i.external_id for i in ex.seen] == [5, 6]


async def test_has_tag_error_skips_item_others_continue(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"has_label"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                       parked_from=Stage.TRIAGE, data={"parked_tag_set": True}))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    s = Scheduler(target=target, store=store, executor=ex, forge=ado, workspaces=ws,
                 clock=lambda: NOW)
    await s.tick()  # has_label() raises for item 5; requeue skipped, item 6 still steps
    assert [i.external_id for i in ex.seen] == [6]
    assert _it(store).stage is Stage.PARKED


async def test_requeue_item_set_tag_error_still_requeues(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"set_label"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "agent/5-add-feature")
    item = _it(store)
    store.save(replace(item, stage=Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                       parked_from=Stage.TRIAGE))
    s = Scheduler(target=target, store=store, executor=ScriptedExecutor(), forge=ado, workspaces=ws,
                 clock=lambda: NOW)
    new = s.requeue_item(item.id)  # set_label() raises; requeue still commits, no exception escapes
    assert new.stage is Stage.PLAN
    assert _it(store).stage is Stage.PLAN


# --- final review fix wave ---------------------------------------------------------------


@dataclass
class FlakyAdo(FakeForge):
    """FakeForge whose named methods raise an infra error for their next N calls."""

    fail_counts: dict[str, int] = field(default_factory=dict)

    def _maybe_fail(self, name: str) -> None:
        if self.fail_counts.get(name, 0) > 0:
            self.fail_counts[name] -= 1
            raise httpx.ConnectError("blip")

    def comment_item(self, id: int, html: str) -> None:
        self._maybe_fail("comment_item")
        super().comment_item(id, html)

    def set_label(self, id: int, label: str, present: bool) -> None:
        self._maybe_fail("set_label")
        super().set_label(id, label, present)


def _flaky(tmp_path: Path, target: TargetConfig, ex: ScriptedExecutor,
           **fails: int) -> tuple[Store, FlakyAdo, Scheduler]:
    store = Store("sqlite://")
    ado = FlakyAdo(fail_counts=dict(fails))
    ado.add(WI)
    s = Scheduler(target=target, store=store, executor=ex, forge=ado,
                  workspaces=Workspaces(tmp_path / "ws", target), clock=lambda: NOW)
    return store, ado, s


async def test_c1_park_comment_failure_still_tags_and_is_not_requeued(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))
    store, ado, s = _flaky(tmp_path, target, ex, comment_item=1)
    await s.tick()
    assert "agent:parked" in ado.tags[5]
    assert _it(store).data.get("parked_tag_set") is True
    await s.tick()  # the tag is present: nothing is read as human approval
    item = _it(store)
    assert item.stage is Stage.PARKED and len(ex.seen) == 1


async def test_c1_park_set_tag_failure_is_retried_and_never_requeued(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))
    store, ado, s = _flaky(tmp_path, target, ex, set_label=2)
    await s.tick()  # park; set_tag fails
    assert "agent:parked" not in ado.tags[5] and "parked_tag_set" not in _it(store).data
    assert ado.wi_comments == []  # tag first, then comment
    await s.tick()  # untagged but flag unset: retry side effects (fails again), no requeue
    assert _it(store).stage is Stage.PARKED and "agent:parked" not in ado.tags[5]
    await s.tick()  # retry succeeds
    item = _it(store)
    assert item.stage is Stage.PARKED and item.data.get("parked_tag_set") is True
    assert "agent:parked" in ado.tags[5] and len(ado.wi_comments) == 1
    assert len(ex.seen) == 1


async def test_c1_removing_tag_after_successful_park_requeues(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")),
                          StepResult(Transition(Stage.IMPLEMENT)))
    store, ado, s = _flaky(tmp_path, target, ex)
    await s.tick()
    ado.set_label(5, "agent:parked", False)
    await s.tick()
    item = _it(store)
    assert item.stage is Stage.IMPLEMENT and "parked_tag_set" not in item.data


@pytest.mark.parametrize("err", [RuntimeError("boom"), AgentInfraError("sdk died")])
async def test_i1_any_executor_error_backs_off_then_parks_infra(
    env, err: Exception  # type: ignore[no-untyped-def]
) -> None:
    store = env[0]
    ex = ScriptedExecutor(err, err, err)
    await sched(env, ex).tick()
    item = _it(store)
    assert item.stage is Stage.TRIAGE and item.infra_failures == 1
    assert item.data["retry_after"] == (NOW + timedelta(minutes=2)).isoformat()
    for i in (1, 2):
        await sched(env, ex, now=NOW + timedelta(hours=i)).tick()
    item = _it(store)
    assert item.stage is Stage.PARKED and item.park_reason is ParkReason.INFRA
    assert type(err).__name__ in item.data["park_note"]


class GatedExecutor:
    """Per-item results; an item with a gate blocks until the gate is set (spec §1 tests)."""

    def __init__(self, results: dict[int, StepResult | Exception],
                gates: dict[int, threading.Event] | None = None,
                barrier: threading.Barrier | None = None) -> None:
        self.results, self.gates, self.barrier = results, gates or {}, barrier
        self.seen: set[int] = set()

    async def run(self, item: Item) -> StepResult:
        self.seen.add(item.external_id)
        if self.barrier is not None:
            self.barrier.wait(5)             # every item must be running at the same time
        gate = self.gates.get(item.external_id)
        if gate is not None:
            assert gate.wait(5)
        r = self.results[item.external_id]
        if isinstance(r, Exception):
            raise r
        return r


def _limit(target: TargetConfig, n: int) -> TargetConfig:
    return target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_concurrent_items": n})})


def _two_implementing(store: Store) -> None:
    for i in (5, 6):
        store.add_item("fixture", replace(WI, id=i), f"agent/{i}-x")
        store.save(replace(_it(store, i), stage=Stage.IMPLEMENT))


def _drain(s: Scheduler) -> None:
    for th in list(s._running.values()):  # noqa: SLF001 - joining the item threads
        th.join(5)


def _until(pred, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """Poll `pred` every 10ms until true, up to `timeout`s (controller ruling: `tick(wait=False)`
    returns before the launched item thread has necessarily entered the executor)."""
    deadline = time_module.monotonic() + timeout
    while not pred():
        assert time_module.monotonic() < deadline, "timed out waiting for condition"
        time_module.sleep(0.01)


async def test_items_run_concurrently(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY)),
                        6: StepResult(Transition(Stage.VERIFY))}, barrier=threading.Barrier(2))
    await Scheduler(target=_limit(target, 2), store=store, executor=ex, forge=ado,
                    workspaces=ws, clock=lambda: NOW).tick()
    assert _it(store, 5).stage is Stage.VERIFY and _it(store, 6).stage is Stage.VERIFY


async def test_limit_holds_and_freed_slot_is_refilled(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    gate = threading.Event()
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY)),
                        6: StepResult(Transition(Stage.VERIFY))}, gates={5: gate})
    s = Scheduler(target=_limit(target, 1), store=store, executor=ex, forge=ado,
                  workspaces=ws, clock=lambda: NOW)
    await s.tick(wait=False)
    _until(lambda: 5 in ex.seen)  # controller ruling: wait for the executor to actually start
    await s.tick(wait=False)                  # 5 still running: 6 must not start
    assert ex.seen == {5}
    assert set(store.flags("busy:fixture:")) == {"busy:fixture:5"}
    assert (store.get_flag("busy:fixture:5") or "").startswith("implement|")
    gate.set()
    _drain(s)
    assert store.flags("busy:fixture:") == {}
    await s.tick()
    assert ex.seen == {5, 6} and _it(store, 6).stage is Stage.VERIFY


async def test_run_forever_clears_stale_busy_flags(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    store.set_flag("busy:fixture:9", "implement|2026-09-01T00:00:00+00:00")
    store.set_flag("busy:other:9", "implement|2026-09-01T00:00:00+00:00")
    stop = threading.Event()
    stop.set()
    await sched(env, GatedExecutor({})).run_forever(1, stop)
    assert store.get_flag("busy:fixture:9") is None
    assert store.get_flag("busy:other:9") is not None


async def test_requeue_scan_skips_a_running_item(env) -> None:  # type: ignore[no-untyped-def]
    """Review focus 1: a running item that parked must not get park side effects twice."""
    store, ado, ws, target = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.IMPLEMENT))
    gate = threading.Event()
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY))}, gates={5: gate})
    s = sched(env, ex)
    await s.tick(wait=False)
    _until(lambda: 5 in ex.seen)  # controller ruling: wait for the executor to actually start
    store.save(replace(_it(store), stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))   # as if the item thread just parked
    await s.tick(wait=False)
    assert ado.wi_comments == [] and "agent:parked" not in ado.tags[5]
    gate.set()
    _drain(s)


async def test_i1_error_on_one_item_does_not_stop_others(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    ex = GatedExecutor({5: RuntimeError("boom"), 6: StepResult(Transition(Stage.VERIFY))})
    await Scheduler(target=_limit(target, 2), store=store, executor=ex, forge=ado,
                    workspaces=ws, clock=lambda: NOW).tick()
    assert ex.seen == {5, 6}
    assert _it(store).infra_failures == 1 and _it(store, 6).stage is Stage.VERIFY


async def test_i1_error_polling_awaiting_item_still_steps_active(  # type: ignore[no-untyped-def]
    env
) -> None:
    store = env[0]
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
    ex = ScriptedExecutor(KeyError("pr vanished"), StepResult(Transition(Stage.PLAN)))
    await sched(env, ex).tick()
    assert [i.external_id for i in ex.seen] == [5, 6]
    assert _it(store).infra_failures == 1 and _it(store, 6).stage is Stage.PLAN


async def test_i2_usage_limit_records_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    used = Usage(4, 500, 50, cache_read_tokens=9000)
    ex = ScriptedExecutor(UsageLimitError("usage limit reached", usage=used))
    await sched(env, ex).tick()
    assert _it(store).usage == used and _it(store).stage is Stage.TRIAGE
    assert store.daily_usage(NOW.date()).tokens == 550
    assert store.get_flag("paused_until") == (NOW + timedelta(minutes=30)).isoformat()


async def test_i4_cache_reads_do_not_trip_item_budget(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    tight = target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_item_tokens": 100})})
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN),
                                     Usage(1, 50, 20, cache_read_tokens=1_000_000)))
    await Scheduler(target=tight, store=store, executor=ex, forge=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    item = _it(store)
    assert item.stage is Stage.PLAN and item.usage.cache_read_tokens == 1_000_000


async def test_agent_error_park_requeue_retries_stage_without_labels(  # type: ignore[no-untyped-def]
    env,
) -> None:
    from tests.fakes import decision

    store, ado, _, _ = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.PLAN))
    rejected = decision("plan", "plan_scope_ok", "no")
    ex = ScriptedExecutor(
        StepResult(park(ParkReason.AGENT_ERROR, "agent did not finish"),
                   decisions=[(rejected, {"plan": "old"})]),
        StepResult(Transition(Stage.IMPLEMENT)))
    s = sched(env, ex)
    await s.tick()
    assert _it(store).park_reason is ParkReason.AGENT_ERROR
    ado.set_label(5, "agent:parked", False)
    await s.tick()
    assert ex.seen[1].stage is Stage.PLAN  # retried the plan stage, not approved past it
    assert store.labels("plan", "plan_scope_ok") == []
    assert store.labels("plan", "plan_addresses_item") == []


def _add_item(store: Store, stage: Stage, **kw):  # type: ignore[no-untyped-def]
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=stage, **kw))
    return _it(store)


async def test_step_writes_intake_step_and_transition_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN),
                                     events=[EventInput("agent_session", {"role": "x"})]))
    await sched(env, ex).tick()
    evs = store.events_for(_it(store).id)
    assert [e.kind for e in evs] == ["intake", "agent_session", "transition"]
    assert evs[-1].stage == "triage" and evs[-1].payload["to"] == "plan"
    assert store.get_flag("last_tick:fixture") == NOW.isoformat()


async def test_awaiting_noop_poll_writes_no_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)),
                          StepResult(Transition(Stage.AWAITING_HUMAN)))
    s = sched(env, ex)
    await s.tick()
    await s.tick()
    assert store.events_for(_it(store).id) == []


async def test_merge_writes_outcome(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    kinds = [(e.kind, e.payload.get("result")) for e in store.events_for(_it(store).id)]
    assert kinds == [("transition", None), ("outcome", "merged")]


async def test_infra_failure_records_partial_session(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    partial = AgentResult("", Usage(1, 1, 1), role="planner")
    await sched(env, ScriptedExecutor(AgentInfraError("sdk down", partial=partial))).tick()
    assert [e.kind for e in store.events_for(_it(store).id)] == [
        "intake", "agent_session", "infra_failure"]


async def test_usage_limit_records_event(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    err = UsageLimitError("usage limit reached", partial=AgentResult("", Usage(), role="planner"))
    await sched(env, ScriptedExecutor(err)).tick()
    assert [e.kind for e in store.events_for(_it(store).id)][-2:] == [
        "agent_session", "usage_limit"]


async def test_requeue_writes_event(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    _add_item(store, Stage.PARKED, park_reason=ParkReason.RED, parked_from=Stage.VERIFY,
              data={"parked_tag_set": True})
    s = sched(env, ScriptedExecutor(StepResult(Transition(Stage.REVIEW))))
    await s.tick()  # tag absent -> requeue to verify, then the step runs
    [rq] = [e for e in store.events_for(_it(store).id) if e.kind == "requeue"]
    assert rq.payload == {"from_reason": "red", "to": "verify", "approved": False}


async def test_park_side_effects_write_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    await sched(env, ScriptedExecutor(StepResult(park(ParkReason.RED, "red")))).tick()
    assert [e.kind for e in store.events_for(_it(store).id)][-2:] == ["transition", "park_tagged"]


async def test_stale_item_logs_warning(env, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    it = _add_item(store, Stage.PLAN)
    store.add_event("intake", {}, item=it, ts=NOW - timedelta(hours=3))
    with caplog.at_level(logging.WARNING):
        await sched(env, ScriptedExecutor(StepResult(Transition(Stage.PLAN)))).tick()
    assert "stale" in caplog.text


# --- final-review fix wave: I4 kill switch keeps policy events, I5 busy flag ---


async def test_i4_interrupted_step_records_partial_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    err = AgentInterrupted("x", partial=AgentResult("", Usage(), role="planner"))
    await sched(env, ScriptedExecutor(err)).tick()
    assert [e.kind for e in store.events_for(_it(store).id)] == ["intake", "agent_session"]
    assert _it(store).stage is Stage.TRIAGE


async def test_i5_busy_flag_set_during_step_and_cleared_after(  # type: ignore[no-untyped-def]
    env
) -> None:
    store = env[0]
    seen: list[str | None] = []

    class Spy:
        async def run(self, item: Item) -> StepResult:
            seen.append(store.get_flag("busy:fixture:5"))
            return StepResult(Transition(Stage.PLAN))

    await sched(env, Spy()).tick()
    assert seen == [f"triage|{NOW.isoformat()}"]
    assert store.get_flag("busy:fixture:5") is None


# --- follow-up fix wave: F4 queued items are not stale, F6 kill switch counts tokens ---


async def test_f4_stale_warns_only_for_in_flight_item(  # type: ignore[no-untyped-def]
    env, caplog: pytest.LogCaptureFixture
) -> None:
    store, ado, ws, target = env
    for i in (5, 6):
        store.add_item("fixture", replace(WI, id=i), f"agent/{i}-x")
        store.save(replace(_it(store, i), stage=Stage.PLAN))
        store.add_event("intake", {}, item=_it(store, i), ts=NOW - timedelta(hours=3))
    with caplog.at_level(logging.WARNING):
        await sched(env, ScriptedExecutor(StepResult(Transition(Stage.PLAN)))).tick()
    stale_records = [r for r in caplog.records if "stale" in r.getMessage()]
    assert len(stale_records) == 1


async def test_f6_kill_switch_counts_partial_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    partial = AgentResult("", Usage(2, 100, 50), role="planner")
    err = AgentInterrupted("x", partial=partial)
    await sched(env, ScriptedExecutor(err)).tick()
    item = _it(store)
    assert item.usage.tokens == 150
    assert item.stage is Stage.TRIAGE
    assert store.daily_usage(NOW.date()).turns == 2
    assert store.daily_usage(NOW.date()).tokens == 150


# --- Task 8: per-target pause/flags, stoppable loop -----------------------------------------


async def test_per_target_pause_and_flags(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    store.set_flag("paused:fixture", "1")
    await sched(env, ex).tick()
    assert ex.seen == [] and store.get_flag("last_tick:fixture") == NOW.isoformat()
    store.set_flag("paused:fixture", None)
    store.set_flag("paused:other", "1")          # another target's pause does not apply
    await sched(env, ex).tick()
    assert [i.external_id for i in ex.seen] == [5]
    assert store.get_flag("busy:fixture") is None


def test_stop_requested(env) -> None:  # type: ignore[no-untyped-def]
    from agent_sdlc.orchestrator.scheduler import stop_requested
    store = env[0]
    assert not stop_requested(store, "fixture")
    store.set_flag("paused:fixture", "1")
    assert stop_requested(store, "fixture") and not stop_requested(store, "other")
    store.set_flag("paused:fixture", None)
    store.set_flag("paused", "1")
    assert stop_requested(store, "other")


async def test_run_forever_stops_on_event(env) -> None:  # type: ignore[no-untyped-def]
    import threading
    stop = threading.Event()

    class Once(ScriptedExecutor):
        async def run(self, item: Item) -> StepResult:
            stop.set()
            return StepResult(Transition(Stage.PLAN))

    await sched(env, Once()).run_forever(poll_s=60, stop=stop)
    assert env[0].get_by_ref("fixture", 5).stage is Stage.PLAN
