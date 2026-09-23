from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest

from laya_sdlc.orchestrator.scheduler import Scheduler
from laya_sdlc.orchestrator.stages import StageExecutor, StepResult
from laya_sdlc.orchestrator.transitions import Transition, park
from laya_sdlc.policy import PathPolicy
from laya_sdlc.store import Store
from laya_sdlc.targets import RunWindow, TargetConfig
from laya_sdlc.types import Item, ParkReason, Stage, Usage, UsageLimitError, WorkItem
from laya_sdlc.workspaces import Workspaces
from tests.fakes import FakeAdo, FakeDecider, FakeRunner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "d", "ac", "Bug", ("laya",), "u")


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
class RaisingAdo(FakeAdo):
    """FakeAdo that raises an infra error from named methods (fix round 1, R15)."""

    fail: set[str] = field(default_factory=set)

    def list_intake(self) -> list[WorkItem]:
        if "list_intake" in self.fail:
            raise httpx.ConnectError("down")
        return super().list_intake()

    def has_tag(self, id: int, tag: str) -> bool:
        if "has_tag" in self.fail:
            raise httpx.ConnectError("down")
        return super().has_tag(id, tag)

    def set_tag(self, id: int, tag: str, present: bool) -> None:
        if "set_tag" in self.fail:
            raise httpx.ConnectError("down")
        super().set_tag(id, tag, present)


@pytest.fixture
def env(tmp_path: Path, target: TargetConfig):  # type: ignore[no-untyped-def]
    store = Store("sqlite://")
    ado = FakeAdo()
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    return store, ado, ws, target


def sched(env, executor, now=NOW):  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    return Scheduler(target=target, store=store, executor=executor, ado=ado, workspaces=ws,
                     clock=lambda: now)


async def test_intake_adds_item_with_branch(env) -> None:  # type: ignore[no-untyped-def]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    await sched(env, ex).tick()
    item = env[0].get(5)
    assert item.branch == "laya/5-add-feature" and item.stage is Stage.PLAN


async def test_step_merges_data_and_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)),
                          StepResult(Transition(Stage.IMPLEMENT), Usage(2, 100, 10),
                                     data={"plan": "p", "feedback": None}))
    s = sched(env, ex)
    await s.tick()
    store.save(replace(store.get(5), data={"feedback": "old"}))
    await s.tick()
    item = store.get(5)
    assert item.data == {"plan": "p"} and item.usage == Usage(2, 100, 10)
    assert store.daily_usage(NOW.date()) == Usage(2, 100, 10)


async def test_parking_comments_and_tags(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    await sched(env, ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))).tick()
    assert store.get(5).stage is Stage.PARKED
    assert "laya:parked" in ado.tags[5]
    assert "unclear" in ado.wi_comments[0][1]


async def test_removing_tag_requeues_with_labels(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    real = StageExecutor(target=target, ado=ado, decider=FakeDecider(shadow={"triage"}),
                         runner=FakeRunner(), workspaces=ws,
                         path_policy=PathPolicy(target.policy.protected_paths),
                         decisions_for=store.decisions_for)
    s = sched(env, real)
    await s.tick()
    assert store.get(5).park_reason is ParkReason.NEEDS_HUMAN
    ado.set_tag(5, "laya:parked", False)
    s._executor = ScriptedExecutor(StepResult(Transition(Stage.IMPLEMENT)))  # stop after requeue
    await s.tick()
    assert store.get(5).stage is Stage.IMPLEMENT  # requeued to PLAN, then stepped once
    assert [g for _, g in store.labels("triage", "clarity")] == ["clear"]
    assert [g for _, g in store.labels("triage", "touches_protected")] == ["false"]


async def test_usage_limit_pauses_without_changing_item(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    reset = NOW + timedelta(hours=2)
    ex = ScriptedExecutor(UsageLimitError("usage limit", reset_at=reset))
    s = sched(env, ex)
    await s.tick()
    assert store.get(5).stage is Stage.TRIAGE
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
    item = store.get(5)
    assert item.stage is Stage.PARKED and item.park_reason is ParkReason.INFRA


async def test_item_budget_parks(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    tight = target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_item_tokens": 100})})
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN), Usage(1, 90, 20)))
    await Scheduler(target=tight, store=store, executor=ex, ado=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    assert store.get(5).park_reason is ParkReason.BUDGET


async def test_run_window_blocks_agent_stages_not_polling(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    night = target.model_copy(update={"limits": target.limits.model_copy(
        update={"run_window": RunWindow(start=time(19), end=time(7))})})
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "laya/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)))
    await Scheduler(target=night, store=store, executor=ex, ado=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    assert [i.id for i in ex.seen] == [5]


async def test_concurrency_prefers_in_flight(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    ado.add(replace(WI, id=6, title="Other"))
    store.add_item("fixture", replace(WI, id=6), "laya/6-other")
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.IMPLEMENT))
    ex = ScriptedExecutor(StepResult(Transition(Stage.VERIFY)))
    await sched(env, ex).tick()
    assert [i.id for i in ex.seen] == [5]


async def test_done_cleans_up(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, _ = env
    store.add_item("fixture", WI, "laya/5-add-feature")
    ws.create(5, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    assert store.get(5).stage is Stage.DONE
    assert not ws.worktree_path(5).exists()
    assert ado.deleted_branches == ["laya/5-add-feature"]


async def test_intake_error_still_polls_and_steps(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"list_intake"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "laya/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)),
                          StepResult(Transition(Stage.PLAN)))
    s = Scheduler(target=target, store=store, executor=ex, ado=ado, workspaces=ws,
                 clock=lambda: NOW)
    await s.tick()  # list_intake() raises; intake skipped, rest of tick proceeds
    assert [i.id for i in ex.seen] == [5, 6]


async def test_has_tag_error_skips_item_others_continue(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"has_tag"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                       parked_from=Stage.TRIAGE))
    store.add_item("fixture", replace(WI, id=6), "laya/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    s = Scheduler(target=target, store=store, executor=ex, ado=ado, workspaces=ws,
                 clock=lambda: NOW)
    await s.tick()  # has_tag() raises for item 5; requeue skipped, item 6 still steps
    assert [i.id for i in ex.seen] == [6]
    assert store.get(5).stage is Stage.PARKED


async def test_requeue_item_set_tag_error_still_requeues(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    ado = RaisingAdo(fail={"set_tag"})
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                       parked_from=Stage.TRIAGE))
    s = Scheduler(target=target, store=store, executor=ScriptedExecutor(), ado=ado, workspaces=ws,
                 clock=lambda: NOW)
    new = s.requeue_item(5)  # set_tag() raises; requeue still commits and no exception escapes
    assert new.stage is Stage.PLAN
    assert store.get(5).stage is Stage.PLAN
