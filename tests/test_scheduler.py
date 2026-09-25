import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest

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
from tests.fakes import FakeAdo, FakeDecider, FakeRunner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "d", "ac", "Bug", ("agent",), "u")


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
    assert item.branch == "agent/5-add-feature" and item.stage is Stage.PLAN


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
    assert "agent:parked" in ado.tags[5]
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
    ado.set_tag(5, "agent:parked", False)
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
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)))
    await Scheduler(target=night, store=store, executor=ex, ado=ado, workspaces=ws,
                    clock=lambda: NOW).tick()
    assert [i.id for i in ex.seen] == [5]


async def test_concurrency_prefers_in_flight(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    ado.add(replace(WI, id=6, title="Other"))
    store.add_item("fixture", replace(WI, id=6), "agent/6-other")
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.IMPLEMENT))
    ex = ScriptedExecutor(StepResult(Transition(Stage.VERIFY)))
    await sched(env, ex).tick()
    assert [i.id for i in ex.seen] == [5]


async def test_done_cleans_up(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, _ = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    ws.create(5, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    assert store.get(5).stage is Stage.DONE
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
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
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
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.PARKED, park_reason=ParkReason.NEEDS_HUMAN,
                       parked_from=Stage.TRIAGE, data={"parked_tag_set": True}))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
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
    store.add_item("fixture", WI, "agent/5-add-feature")
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
    assert "agent:parked" in ado.tags[5]
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
    assert "agent:parked" not in ado.tags[5] and "parked_tag_set" not in store.get(5).data
    assert ado.wi_comments == []  # tag first, then comment
    await s.tick()  # untagged but flag unset: retry side effects (fails again), no requeue
    assert store.get(5).stage is Stage.PARKED and "agent:parked" not in ado.tags[5]
    await s.tick()  # retry succeeds
    item = store.get(5)
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
    ado.set_tag(5, "agent:parked", False)
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
        store.add_item("fixture", replace(WI, id=i), f"agent/{i}-x")
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
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.AWAITING_HUMAN, pr_id=1))
    store.add_item("fixture", replace(WI, id=6), "agent/6-x")
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
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=Stage.PLAN))
    rejected = decision("plan", "plan_scope_ok", "no")
    ex = ScriptedExecutor(
        StepResult(park(ParkReason.AGENT_ERROR, "agent did not finish"),
                   decisions=[(rejected, {"plan": "old"})]),
        StepResult(Transition(Stage.IMPLEMENT)))
    s = sched(env, ex)
    await s.tick()
    assert store.get(5).park_reason is ParkReason.AGENT_ERROR
    ado.set_tag(5, "agent:parked", False)
    await s.tick()
    assert ex.seen[1].stage is Stage.PLAN  # retried the plan stage, not approved past it
    assert store.labels("plan", "plan_scope_ok") == []
    assert store.labels("plan", "plan_addresses_item") == []


def _add_item(store: Store, stage: Stage, **kw):  # type: ignore[no-untyped-def]
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=stage, **kw))
    return store.get(5)


async def test_step_writes_intake_step_and_transition_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN),
                                     events=[EventInput("agent_session", {"role": "x"})]))
    await sched(env, ex).tick()
    evs = store.events_for(5)
    assert [e.kind for e in evs] == ["intake", "agent_session", "transition"]
    assert evs[-1].stage == "triage" and evs[-1].payload["to"] == "plan"
    assert store.get_flag("last_tick") == NOW.isoformat()


async def test_awaiting_noop_poll_writes_no_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)),
                          StepResult(Transition(Stage.AWAITING_HUMAN)))
    s = sched(env, ex)
    await s.tick()
    await s.tick()
    assert store.events_for(5) == []


async def test_merge_writes_outcome(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    kinds = [(e.kind, e.payload.get("result")) for e in store.events_for(5)]
    assert kinds == [("transition", None), ("outcome", "merged")]


async def test_infra_failure_records_partial_session(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    partial = AgentResult("", Usage(1, 1, 1), role="planner")
    await sched(env, ScriptedExecutor(AgentInfraError("sdk down", partial=partial))).tick()
    assert [e.kind for e in store.events_for(5)] == ["intake", "agent_session", "infra_failure"]


async def test_usage_limit_records_event(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    err = UsageLimitError("usage limit reached", partial=AgentResult("", Usage(), role="planner"))
    await sched(env, ScriptedExecutor(err)).tick()
    assert [e.kind for e in store.events_for(5)][-2:] == ["agent_session", "usage_limit"]


async def test_requeue_writes_event(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    _add_item(store, Stage.PARKED, park_reason=ParkReason.RED, parked_from=Stage.VERIFY,
              data={"parked_tag_set": True})
    s = sched(env, ScriptedExecutor(StepResult(Transition(Stage.REVIEW))))
    await s.tick()  # tag absent -> requeue to verify, then the step runs
    [rq] = [e for e in store.events_for(5) if e.kind == "requeue"]
    assert rq.payload == {"from_reason": "red", "to": "verify", "approved": False}


async def test_park_side_effects_write_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    await sched(env, ScriptedExecutor(StepResult(park(ParkReason.RED, "red")))).tick()
    assert [e.kind for e in store.events_for(5)][-2:] == ["transition", "park_tagged"]


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
    assert [e.kind for e in store.events_for(5)] == ["intake", "agent_session"]
    assert store.get(5).stage is Stage.TRIAGE


async def test_i5_busy_flag_set_during_step_and_cleared_after(  # type: ignore[no-untyped-def]
    env
) -> None:
    store = env[0]
    seen: list[str | None] = []

    class Spy:
        async def run(self, item: Item) -> StepResult:
            seen.append(store.get_flag("busy"))
            return StepResult(Transition(Stage.PLAN))

    await sched(env, Spy()).tick()
    assert seen == [f"5|triage|{NOW.isoformat()}"]
    assert store.get_flag("busy") is None


# --- follow-up fix wave: F4 queued items are not stale, F6 kill switch counts tokens ---


async def test_f4_stale_warns_only_for_in_flight_item(  # type: ignore[no-untyped-def]
    env, caplog: pytest.LogCaptureFixture
) -> None:
    store, ado, ws, target = env
    for i in (5, 6):
        store.add_item("fixture", replace(WI, id=i), f"agent/{i}-x")
        store.save(replace(store.get(i), stage=Stage.PLAN))
        store.add_event("intake", {}, item=store.get(i), ts=NOW - timedelta(hours=3))
    with caplog.at_level(logging.WARNING):
        await sched(env, ScriptedExecutor(StepResult(Transition(Stage.PLAN)))).tick()
    stale_records = [r for r in caplog.records if "stale" in r.getMessage()]
    assert len(stale_records) == 1


async def test_f6_kill_switch_counts_partial_usage(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    partial = AgentResult("", Usage(2, 100, 50), role="planner")
    err = AgentInterrupted("x", partial=partial)
    await sched(env, ScriptedExecutor(err)).tick()
    item = store.get(5)
    assert item.usage.tokens == 150
    assert item.stage is Stage.TRIAGE
    assert store.daily_usage(NOW.date()).turns == 2
    assert store.daily_usage(NOW.date()).tokens == 150
