from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdlc.labeling import label_logged
from agent_sdlc.orchestrator.scheduler import Scheduler
from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.policy import PathPolicy
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import (
    AgentResult,
    Denial,
    ParkReason,
    PrComment,
    Stage,
    Usage,
    UsageLimitError,
    WorkItem,
)
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git
from tests.fakes import FakeDecider, FakeForge, FakeRunner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("agent",), "u")


class Env:
    def __init__(self, tmp_path: Path, target: TargetConfig, origin: Path,
                 traces: Path | None = None) -> None:
        self.store = Store("sqlite://")
        self.forge = FakeForge(origin=origin)
        self.forge.add(WI)
        self.ws = Workspaces(tmp_path / "ws", target)
        self.decider = FakeDecider()
        self.runner = FakeRunner()
        self.origin = origin
        executor = StageExecutor(target=target, forge=self.forge, decider=self.decider,
                                 runner=self.runner, workspaces=self.ws,
                                 path_policy=PathPolicy(target.policy.protected_paths),
                                 decisions_for=self.store.decisions_for, traces=traces)
        self.sched = Scheduler(target=target, store=self.store, executor=executor,
                               forge=self.forge, workspaces=self.ws, clock=lambda: NOW)

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
    pr = env.forge.prs[100]
    assert pr["branch"] == "agent/5-add-feature" and "AB#5" in pr["body"]
    assert "agent/5-add-feature" in git("branch", "--list", "agent/*", cwd=env.origin)
    assert [r for r, _ in env.runner.calls] == ["planner", "implementer", "reviewer"]
    env.forge.prs[100]["status"] = "completed"
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
    assert "agent:parked" in env.forge.tags[5]
    assert any("broken.txt present" in c for _, c in env.forge.wi_comments)


async def test_protected_path_is_caught_before_push(env: Env) -> None:
    def write_infra(role, prompt, cwd):  # type: ignore[no-untyped-def]
        (cwd / "infra").mkdir(exist_ok=True)
        (cwd / "infra" / "main.bicep").write_text("x")
        return AgentResult("done", Usage(1, 10, 10))

    env.runner.behaviors["implementer"] = write_infra
    await env.ticks(3)
    assert env.item.park_reason is ParkReason.POLICY
    assert env.forge.prs == {}
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
    env.forge.set_label(5, "agent:parked", False)
    await env.ticks(1)
    assert env.item.stage is Stage.IMPLEMENT  # requeued to plan, plan ran in the same tick
    assert env.store.labels("triage", "clarity")[0][1] == "clear"


async def test_pr_change_request_round(env: Env) -> None:
    await env.ticks(6)
    env.forge.pr_threads[100].append(PrComment(1, 1, "Brian", "/agent also add docs.txt"))
    await env.ticks(1)  # awaiting poll -> implement, then implement runs in the same tick
    assert env.item.stage is Stage.VERIFY and env.item.pr_rounds == 1
    await env.ticks(3)  # verify, review, pr_open
    assert env.item.stage is Stage.AWAITING_HUMAN
    assert env.forge.prs[100]["updates"] == 1
    assert "/agent also add docs.txt" in env.runner.calls[3][1]


async def test_bot_reply_not_reprocessed(env: Env) -> None:
    await env.ticks(6)
    env.decider.answers["comment"] = {"comment_intent": "question"}
    env.forge.pr_threads[100].append(PrComment(1, 1, "Brian", "why?"))
    await env.ticks(3)
    assert len(env.forge.replies) == 1
    assert env.item.stage is Stage.AWAITING_HUMAN


async def test_escalated_agent_parks_with_trace(
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    env = Env(tmp_path, target, origin_repo, traces=tmp_path / "traces")

    def escape(role, prompt, cwd):  # type: ignore[no-untyped-def]
        return AgentResult("", Usage(2, 50, 5), (Denial(
            "Read", "outside_worktree", "path is outside the worktree",
            '{"file_path": "/Users/x/.ssh/config"}'),), escalated="outside_worktree")

    env.runner.behaviors["implementer"] = escape
    await env.ticks(3)  # triage, plan, implement
    assert env.item.stage is Stage.PARKED and env.item.park_reason is ParkReason.POLICY
    assert any("Blocked tool calls" in c and ".ssh/config" in c for _, c in env.forge.wi_comments)
    kinds = [e.kind for e in env.store.events_for(5)]
    assert "tool_denied" in kinds and kinds[-1] == "park_tagged"
    out = render_trace(env.store, 5)
    assert "ESCALATED outside_worktree" in out and "→ parked (policy)" in out
    assert env.runner.traces[1] is not None and env.runner.traces[1].parent == (
        tmp_path / "traces" / "5")


async def test_manifest_change_needs_approval_then_reinstalls(
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    mt = target.model_copy(update={"policy": target.policy.model_copy(
        update={"manifest_paths": ["**/package.json"]})})
    env = Env(tmp_path, mt, origin_repo)
    calls = {"n": 0}

    def work(role, prompt, cwd):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            (cwd / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        else:
            (cwd / "docs.txt").write_text(f"round {calls['n']}\n")
        return AgentResult("done", Usage(1, 10, 10))

    env.runner.behaviors["implementer"] = work
    await env.ticks(3)  # triage, plan, implement
    assert env.item.park_reason is ParkReason.MANIFEST
    assert any("left-pad" in c for _, c in env.forge.wi_comments)
    env.forge.set_label(5, "agent:parked", False)
    await env.ticks(1)  # requeue -> verify runs in the same tick
    assert env.item.stage is Stage.REVIEW
    installs = [e for e in env.store.events_for(5)
                if e.kind == "check" and e.payload["name"] == "install"]
    assert len(installs) == 2  # before implement, and after the approved manifest change
    await env.ticks(2)  # review, pr_open
    assert env.item.stage is Stage.AWAITING_HUMAN
    env.forge.pr_threads[100].append(PrComment(1, 1, "Brian", "/agent add docs"))
    await env.ticks(2)  # awaiting -> implement (same tick), verify
    assert env.item.stage is Stage.REVIEW  # unchanged manifest digest: no second park


async def test_abandoned_pr_records_outcome_for_labeling(env: Env) -> None:
    await env.ticks(6)
    env.forge.prs[100]["status"] = "abandoned"
    await env.ticks(1)
    assert env.item.stage is Stage.CLOSED
    assert [e.payload["result"] for e in env.store.events_for(5) if e.kind == "outcome"] == [
        "abandoned"]
    assert env.store.abandoned_item_ids() == {5}
    assert label_logged(env.store, "review", 10, lambda _: "true", abandoned_only=True) == 1
