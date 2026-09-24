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
from laya_sdlc.types import (
    AgentInfraError,
    Item,
    ParkReason,
    Stage,
    Usage,
    UsageLimitError,
    WorkItem,
)
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
                       parked_from=Stage.TRIAGE, data={"parked_tag_set": True}))
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


# --- final review fix wave ---------------------------------------------------------------


@dataclass
class FlakyAdo(FakeAdo):
    """FakeAdo whose named methods raise an infra error for their next N calls."""

    fail_counts: dict[str, int] = field(default_factory=dict)

    def _maybe_fail(self, name: str) -> None:
        if self.fail_counts.get(name, 0) > 0:
            self.fail_counts[name] -= 1
            raise httpx.ConnectError("blip")

    def comment_work_item(self, id: int, html_text: str) -> None:
        self._maybe_fail("comment_work_item")
        super().comment_work_item(id, html_text)

    def set_tag(self, id: int, tag: str, present: bool) -> None:
        self._maybe_fail("set_tag")
        super().set_tag(id, tag, present)


def _flaky(tmp_path: Path, target: TargetConfig, ex: ScriptedExecutor,
           **fails: int) -> tuple[Store, FlakyAdo, Scheduler]:
    store = Store("sqlite://")
    ado = FlakyAdo(fail_counts=dict(fails))
    ado.add(WI)
    s = Scheduler(target=target, store=store, executor=ex, ado=ado,
                  workspaces=Workspaces(tmp_path / "ws", target), clock=lambda: NOW)
    return store, ado, s


async def test_c1_park_comment_failure_still_tags_and_is_not_requeued(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))
    store, ado, s = _flaky(tmp_path, target, ex, comment_work_item=1)
    await s.tick()
    assert "laya:parked" in ado.tags[5]
    assert store.get(5).data.get("parked_tag_set") is True
    await s.tick()  # the tag is present: nothing is read as human approval
    item = store.get(5)
    assert item.stage is Stage.PARKED and len(ex.seen) == 1


async def test_c1_park_set_tag_failure_is_retried_and_never_requeued(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")))
    store, ado, s = _flaky(tmp_path, target, ex, set_tag=2)
    await s.tick()  # park; set_tag fails
    assert "laya:parked" not in ado.tags[5] and "parked_tag_set" not in store.get(5).data
    assert ado.wi_comments == []  # tag first, then comment
    await s.tick()  # untagged but flag unset: retry side effects (fails again), no requeue
    assert store.get(5).stage is Stage.PARKED and "laya:parked" not in ado.tags[5]
    await s.tick()  # retry succeeds
    item = store.get(5)
    assert item.stage is Stage.PARKED and item.data.get("parked_tag_set") is True
    assert "laya:parked" in ado.tags[5] and len(ado.wi_comments) == 1
    assert len(ex.seen) == 1


async def test_c1_removing_tag_after_successful_park_requeues(
    tmp_path: Path, target: TargetConfig
) -> None:
    ex = ScriptedExecutor(StepResult(park(ParkReason.NEEDS_HUMAN, "unclear")),
                          StepResult(Transition(Stage.IMPLEMENT)))
    store, ado, s = _flaky(tmp_path, target, ex)
    await s.tick()
    ado.set_tag(5, "laya:parked", False)
    await s.tick()
    item = store.get(5)
    assert item.stage is Stage.IMPLEMENT and "parked_tag_set" not in item.data


@pytest.mark.parametrize("err", [RuntimeError("boom"), AgentInfraError("sdk died")])
async def test_i1_any_executor_error_backs_off_then_parks_infra(
    env, err: Exception  # type: ignore[no-untyped-def]
) -> None:
    store = env[0]
    ex = ScriptedExecutor(err, err, err)
    await sched(env, ex).tick()
    item = store.get(5)
    assert item.stage is Stage.TRIAGE and item.infra_failures == 1
    assert item.data["retry_after"] == (NOW + timedelta(minutes=2)).isoformat()
    for i in (1, 2):
        await sched(env, ex, now=NOW + timedelta(hours=i)).tick()
    item = store.get(5)
    assert item.stage is Stage.PARKED and item.park_reason is ParkReason.INFRA
    assert type(err).__name__ in item.data["park_note"]


async def test_i1_error_on_one_item_does_not_stop_others(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    two = target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_concurrent_items": 2})})
    for i in (5, 6):
        store.add_item("fixture", replace(WI, id=i), f"laya/{i}-x")
        store.save(replace(store.get(i), stage=Stage.IMPLEMENT))
    ex = ScriptedExecutor(RuntimeError("boom"), StepResult(Transition(Stage.VERIFY)))
    await Scheduler(target=two, store=store, executor=ex, ado=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    assert [i.id for i in ex.seen] == [5, 6]
    assert store.get(5).infra_failures == 1 and store.get(6).stage is Stage.VERIFY


async def test_i1_error_polling_awaiting_item_still_steps_active(  # type: ignore[no-untyped-def]
    env
) -> None:
    store = env[0]
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "laya/6-x")
    ex = ScriptedExecutor(KeyError("pr vanished"), StepResult(Transition(Stage.PLAN)))
    await sched(env, ex).tick()
    assert [i.id for i in ex.seen] == [5, 6]
    assert store.get(5).infra_failures == 1 and store.get(6).stage is Stage.PLAN


async def test_i2_usage_limit_records_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    used = Usage(4, 500, 50, cache_read_tokens=9000)
    ex = ScriptedExecutor(UsageLimitError("usage limit reached", usage=used))
    await sched(env, ex).tick()
    assert store.get(5).usage == used and store.get(5).stage is Stage.TRIAGE
    assert store.daily_usage(NOW.date()).tokens == 550
    assert store.get_flag("paused_until") == (NOW + timedelta(minutes=30)).isoformat()


async def test_i4_cache_reads_do_not_trip_item_budget(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    tight = target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_item_tokens": 100})})
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN),
                                     Usage(1, 50, 20, cache_read_tokens=1_000_000)))
    await Scheduler(target=tight, store=store, executor=ex, ado=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    item = store.get(5)
    assert item.stage is Stage.PLAN and item.usage.cache_read_tokens == 1_000_000


async def test_agent_error_park_requeue_retries_stage_without_labels(  # type: ignore[no-untyped-def]
    env,
) -> None:
    from tests.fakes import decision

    store, ado, _, _ = env
    store.add_item("fixture", WI, "laya/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.PLAN))
    rejected = decision("plan", "plan_scope_ok", "no")
    ex = ScriptedExecutor(
        StepResult(park(ParkReason.AGENT_ERROR, "agent did not finish"),
                   decisions=[(rejected, {"plan": "old"})]),
        StepResult(Transition(Stage.IMPLEMENT)))
    s = sched(env, ex)
    await s.tick()
    assert store.get(5).park_reason is ParkReason.AGENT_ERROR
    ado.set_tag(5, "laya:parked", False)
    await s.tick()
    assert ex.seen[1].stage is Stage.PLAN  # retried the plan stage, not approved past it
    assert store.labels("plan", "plan_scope_ok") == []
    assert store.labels("plan", "plan_addresses_item") == []
