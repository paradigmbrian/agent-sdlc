from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdlc.orchestrator.scheduler import Scheduler
from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.policy import PathPolicy
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import (
    AgentResult,
    ParkReason,
    PrComment,
    Stage,
    Usage,
    UsageLimitError,
    WorkItem,
)
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git
from tests.fakes import FakeAdo, FakeDecider, FakeRunner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("agent",), "u")


class Env:
    def __init__(self, tmp_path: Path, target: TargetConfig, origin: Path) -> None:
        self.store = Store("sqlite://")
        self.ado = FakeAdo(origin=origin)
        self.ado.add(WI)
        self.ws = Workspaces(tmp_path / "ws", target)
        self.decider = FakeDecider()
        self.runner = FakeRunner()
        self.origin = origin
        executor = StageExecutor(target=target, ado=self.ado, decider=self.decider,
                                 runner=self.runner, workspaces=self.ws,
                                 path_policy=PathPolicy(target.policy.protected_paths),
                                 decisions_for=self.store.decisions_for)
        self.sched = Scheduler(target=target, store=self.store, executor=executor, ado=self.ado,
                               workspaces=self.ws, clock=lambda: NOW)

    async def ticks(self, n: int) -> None:
        for _ in range(n):
            await self.sched.tick()

    @property
    def item(self):  # type: ignore[no-untyped-def]
        return self.store.get(5)


@pytest.fixture
def env(tmp_path: Path, target: TargetConfig, origin_repo: Path) -> Env:
    return Env(tmp_path, target, origin_repo)


async def test_happy_path_to_pr_then_merge(env: Env) -> None:
    await env.ticks(6)  # triage, plan, implement, verify, review, pr_open
    item = env.item
    assert item.stage is Stage.AWAITING_HUMAN and item.pr_id == 100
    pr = env.ado.prs[100]
    assert pr["branch"] == "agent/5-add-feature" and "AB#5" in pr["body"]
    assert "agent/5-add-feature" in git("branch", "--list", "agent/*", cwd=env.origin)
    assert [r for r, _ in env.runner.calls] == ["planner", "implementer", "reviewer"]
    env.ado.prs[100]["status"] = "completed"
    await env.ticks(1)
    assert env.item.stage is Stage.DONE
    assert not env.ws.worktree_path(5).exists()
    assert env.store.labels("review", "review_blocking")[0][1] == "false"
    assert env.store.labels("plan", "plan_scope_ok")[0][1] == "true"


async def test_red_tests_park_after_retries(env: Env) -> None:
    def break_it(role, prompt, cwd):  # type: ignore[no-untyped-def]
        (cwd / "broken.txt").write_text(prompt[-20:])
        return AgentResult("done", Usage(1, 10, 10))

    env.runner.behaviors["implementer"] = break_it
    await env.ticks(2 + 2 * 4)  # triage, plan, then (implement, verify) x 4
    item = env.item
    assert item.stage is Stage.PARKED and item.park_reason is ParkReason.RED
    assert "agent:parked" in env.ado.tags[5]
    assert any("broken.txt present" in c for _, c in env.ado.wi_comments)


async def test_protected_path_is_caught_before_push(env: Env) -> None:
    def write_infra(role, prompt, cwd):  # type: ignore[no-untyped-def]
        (cwd / "infra").mkdir(exist_ok=True)
        (cwd / "infra" / "main.bicep").write_text("x")
        return AgentResult("done", Usage(1, 10, 10))

    env.runner.behaviors["implementer"] = write_infra
    await env.ticks(3)
    assert env.item.park_reason is ParkReason.POLICY
    assert env.ado.prs == {}
    assert git("branch", "--list", "agent/*", cwd=env.origin) == ""


async def test_usage_limit_pauses_loop(env: Env) -> None:
    def limited(role, prompt, cwd):  # type: ignore[no-untyped-def]
        raise UsageLimitError("usage limit reached")

    env.runner.behaviors["planner"] = limited
    await env.ticks(3)
    assert env.item.stage is Stage.PLAN
    assert env.store.get_flag("paused_until") is not None
    assert [r for r, _ in env.runner.calls] == ["planner"]


async def test_shadow_triage_then_human_approval(env: Env) -> None:
    env.decider.shadow = {"triage"}
    await env.ticks(1)
    assert env.item.park_reason is ParkReason.NEEDS_HUMAN
    env.ado.set_tag(5, "agent:parked", False)
    await env.ticks(1)
    assert env.item.stage is Stage.IMPLEMENT  # requeued to plan, plan ran in the same tick
    assert env.store.labels("triage", "clarity")[0][1] == "clear"


async def test_pr_change_request_round(env: Env) -> None:
    await env.ticks(6)
    env.ado.pr_threads[100].append(PrComment(1, 1, "Brian", "/agent also add docs.txt"))
    await env.ticks(1)  # awaiting poll -> implement, then implement runs in the same tick
    assert env.item.stage is Stage.VERIFY and env.item.pr_rounds == 1
    await env.ticks(3)  # verify, review, pr_open
    assert env.item.stage is Stage.AWAITING_HUMAN
    assert env.ado.prs[100]["updates"] == 1
    assert "/agent also add docs.txt" in env.runner.calls[3][1]


async def test_bot_reply_not_reprocessed(env: Env) -> None:
    await env.ticks(6)
    env.decider.answers["comment"] = {"comment_intent": "question"}
    env.ado.pr_threads[100].append(PrComment(1, 1, "Brian", "why?"))
    await env.ticks(3)
    assert len(env.ado.replies) == 1
    assert env.item.stage is Stage.AWAITING_HUMAN
