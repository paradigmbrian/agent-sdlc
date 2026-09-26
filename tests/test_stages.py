from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.policy import PathPolicy
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import (
    AgentResult,
    Denial,
    Item,
    ParkReason,
    PrComment,
    Stage,
    Usage,
    WorkItem,
)
from agent_sdlc.workspaces import Workspaces
from tests.fakes import FakeDecider, FakeForge, FakeRunner

WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("agent",), "u")


@pytest.fixture
def parts(tmp_path: Path, target: TargetConfig, origin_repo: Path):  # type: ignore[no-untyped-def]
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    decider, runner = FakeDecider(), FakeRunner()
    ex = StageExecutor(target=target, forge=ado, decider=decider, runner=runner, workspaces=ws,
                       path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [])
    return ex, ado, ws, decider, runner


def item(stage: Stage, **kw) -> Item:  # type: ignore[no-untyped-def]
    return replace(Item(5, "fixture", WI.title, "agent/5-add-feature", stage), **kw)


async def test_triage_logs_decisions(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_ = parts
    res = await ex.run(item(Stage.TRIAGE))
    assert res.transition.to is Stage.PLAN
    assert {d.question for d, _ in res.decisions} == {
        "kind", "clarity", "touches_protected", "size"}
    assert res.decisions[0][1]["title"] == "Add feature"


async def test_plan_stores_plan_and_clears_feedback(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, decider, runner = parts
    res = await ex.run(item(Stage.PLAN, data={"feedback": "try again"}))
    assert res.transition.to is Stage.IMPLEMENT
    assert res.data == {"plan": "1. Add feature.txt", "feedback": None}
    assert "try again" in runner.calls[0][1]
    assert ws.worktree_path(5).exists()
    assert res.usage == Usage(3, 1000, 200)


async def test_implement_commits_and_moves_to_verify(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, *_ = parts
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.to is Stage.VERIFY
    assert ws.changed_files(ws.worktree_path(5)) == ["feature.txt"]
    assert isinstance(res.data["installed_digest"], str)


async def test_implement_protected_path_parks(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, _, runner = parts
    ws.create(5, "agent/5-add-feature")

    def write_infra(role, prompt, cwd):  # type: ignore[no-untyped-def]
        (cwd / "infra").mkdir()
        (cwd / "infra" / "x.tf").write_text("x")
        return AgentResult("done", Usage(1, 1, 1))

    runner.behaviors["implementer"] = write_infra
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "infra/x.tf" in res.transition.note


async def test_verify_red_goes_back_to_implement(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, *_ = parts
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "broken.txt").write_text("x")
    ws.commit(wt, "feat: broken")
    res = await ex.run(item(Stage.VERIFY))
    assert res.transition.to is Stage.IMPLEMENT and "broken.txt present" in res.transition.feedback
    assert res.data["checks"][0]["exit_code"] == 1


async def test_review_then_pr_open(parts, origin_repo: Path) -> None:  # type: ignore[no-untyped-def]
    ex, ado, ws, *_ = parts
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "feature.txt").write_text("x")
    ws.commit(wt, "feat: x")
    checks = [{"name": "test", "command": "sh check.sh", "exit_code": 0, "output": "ok",
               "duration_s": 0.1}]
    res = await ex.run(item(Stage.REVIEW, data={"plan": "p", "checks": checks}))
    assert res.transition.to is Stage.PR_OPEN
    res = await ex.run(item(Stage.PR_OPEN, data={"plan": "p", "checks": checks,
                                                 "review_notes": "No blocking issues."}))
    assert res.transition.to is Stage.AWAITING_HUMAN and res.pr_id == 100
    assert ado.prs[100]["branch"] == "agent/5-add-feature"
    assert ado.wi_comments and "PR !100" in ado.wi_comments[0][1]


async def test_pr_open_updates_existing_pr(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, ws, *_ = parts
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "feature.txt").write_text("x")
    ws.commit(wt, "feat: x")
    pr = ado.create_pr("agent/5-add-feature", "t", "b", 5)
    res = await ex.run(item(Stage.PR_OPEN, pr_id=pr, data={"plan": "p", "checks": []}))
    assert res.pr_id == pr and ado.prs[pr]["updates"] == 1


async def test_awaiting_handles_comments(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, _, decider, _ = parts
    pr = ado.create_pr("agent/5-add-feature", "t", "b", 5)
    ado.pr_threads[pr] = [PrComment(1, 1, "Brian", "/agent rename it"),
                          PrComment(2, 1, "Brian", "why this approach?")]
    decider.answers["comment"] = {"comment_intent": "question"}
    res = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))
    assert res.transition.to is Stage.IMPLEMENT and "/agent rename it" in res.transition.feedback
    assert res.data["seen_comments"] == ["thread:1:1", "thread:2:1"]
    assert [t for _, t, _ in ado.replies] == [2]
    assert [lab.gold for lab in res.labels] == ["change_request"]
    again = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr, data=res.data))
    assert again.transition.to is Stage.AWAITING_HUMAN


async def test_awaiting_completed(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, *_ = parts
    pr = ado.create_pr("agent/5-add-feature", "t", "b", 5)
    ado.prs[pr]["status"] = "completed"
    assert (await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))).transition.to is Stage.DONE


# --- final review fix wave ---------------------------------------------------------------


def _failing(role: str):  # type: ignore[no-untyped-def]
    return {role: lambda r, p, c: AgentResult("", Usage(2, 10, 1), is_error=True,
                                              error="error_max_turns")}


@pytest.mark.parametrize("stage,role", [(Stage.PLAN, "planner"),
                                        (Stage.IMPLEMENT, "implementer"),
                                        (Stage.REVIEW, "reviewer")])
async def test_i2_agent_error_retries_then_parks(  # type: ignore[no-untyped-def]
    parts, stage: Stage, role: str
) -> None:
    ex, _, ws, decider, runner = parts
    runner.behaviors = _failing(role)
    data = {"plan": "p", "checks": [], "installed": True}
    res = await ex.run(item(stage, data=data))
    assert res.transition.to is stage and res.transition.count_attempt
    assert res.usage == Usage(2, 10, 1)
    assert decider.calls == []  # no gate decision on an unfinished agent run
    res = await ex.run(item(stage, attempt=3, data=data))
    assert res.transition.park_reason is ParkReason.AGENT_ERROR
    assert "agent did not finish: error_max_turns" in res.transition.note
    assert res.usage == Usage(2, 10, 1)


class DryRunAdo(FakeForge):
    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        return 0


async def test_m6_dry_run_does_not_comment_plan(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ado = DryRunAdo(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    ex = StageExecutor(target=target, forge=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [])
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "feature.txt").write_text("x")
    ws.commit(wt, "feat: x")
    res = await ex.run(item(Stage.PR_OPEN, data={"plan": "p", "checks": []}))
    assert res.transition.to is Stage.AWAITING_HUMAN and res.pr_id == 0
    assert ado.wi_comments == []


async def test_m9_lint_fix_commit_rechecks_diff_limit(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    fixer = target.model_copy(update={"repo": target.repo.model_copy(
        update={"commands": {"lint": "seq 1 300 > generated.txt"}})})
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", fixer)
    ex = StageExecutor(target=fixer, forge=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(fixer.policy.protected_paths),
                       decisions_for=lambda _id: [])
    res = await ex.run(item(Stage.VERIFY))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "300" in res.transition.note and "200" in res.transition.note


T0 = datetime(2026, 10, 2, 19, 4, 12, tzinfo=UTC)
ESCAPE = Denial("Read", "outside_worktree", "path is outside the worktree",
                '{"file_path": "/Users/x/.ssh/config"}')


def _executor(tmp_path: Path, target: TargetConfig, origin_repo: Path,
              traces: Path | None = None):  # type: ignore[no-untyped-def]
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    runner = FakeRunner()
    ex = StageExecutor(target=target, forge=ado, decider=FakeDecider(), runner=runner,
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [], traces=traces, clock=lambda: T0)
    return ex, ado, ws, runner


def _with_manifests(target: TargetConfig) -> TargetConfig:
    return target.model_copy(update={"policy": target.policy.model_copy(
        update={"manifest_paths": ["**/package.json"]})})


async def test_agent_session_events_and_trace_path(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, _, runner = _executor(tmp_path, target, origin_repo, traces=tmp_path / "traces")
    res = await ex.run(item(Stage.PLAN))
    assert [e.kind for e in res.events] == ["agent_session"]
    assert res.events[0].payload["role"] == "planner"
    assert runner.traces == [tmp_path / "traces" / "5" / "20261002T190412-plan-a0-planner.jsonl"]


async def test_token_budget_passed_to_runner(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_, runner = parts
    await ex.run(item(Stage.PLAN, usage=Usage(0, 1_999_000, 0)))
    assert runner.budgets == [1000]


async def test_escalated_session_parks_policy(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, _, runner = parts
    runner.behaviors["implementer"] = lambda r, p, c: AgentResult(
        "", Usage(1, 5, 5), (ESCAPE,), escalated="outside_worktree")
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "outside_worktree" in res.transition.note and ".ssh/config" in res.transition.note
    assert [e.kind for e in res.events] == ["check", "agent_session", "tool_denied"]
    assert res.data["denial_counts"] == {"implementer": 1}
    assert res.data["last_denials"][0]["tool"] == "Read"
    assert res.usage == Usage(1, 5, 5)


async def test_f5_non_escalated_denials_have_no_last_denials(  # type: ignore[no-untyped-def]
    parts
) -> None:
    ex, _, ws, _, runner = parts
    denial = Denial("Bash", "command_not_allowlisted", "command not allowlisted: rm")
    runner.behaviors["implementer"] = lambda r, p, c: AgentResult(
        "done", Usage(1, 1, 1), (denial,))
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.data["denial_counts"] == {"implementer": 1}
    assert "last_denials" not in res.data


async def test_budget_escalation_parks_budget(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_, runner = parts
    runner.behaviors["planner"] = lambda r, p, c: AgentResult(
        "", Usage(1, 5, 5), escalated="budget")
    res = await ex.run(item(Stage.PLAN))
    assert res.transition.park_reason is ParkReason.BUDGET


async def test_verify_writes_check_logs_and_events(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, _ = _executor(tmp_path, target, origin_repo, traces=tmp_path / "traces")
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.VERIFY, attempt=1))
    names = [e.payload["name"] for e in res.events if e.kind == "check"]
    assert names == ["install", "test"]
    log = res.data["checks"][0]["log"]
    assert log.endswith("20261002T190412-verify-a1-test.log") and "ok" in Path(log).read_text()


async def test_manifest_change_parks_then_approval_reinstalls(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)

    def add_dep(r, p, cwd):  # type: ignore[no-untyped-def]
        (cwd / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        return AgentResult("done", Usage(1, 1, 1))

    runner.behaviors["implementer"] = add_dep
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert "package.json" in res.transition.note and "left-pad" in res.data["manifest_diff"]
    pending = res.data["manifest_pending"]
    first_install = res.data["installed_digest"]
    # Approved: verify reinstalls because the manifest content changed, then runs checks.
    data = {"manifest_approved": pending, "installed_digest": first_install}
    res = await ex.run(item(Stage.VERIFY, data=data))
    assert res.transition.to is Stage.REVIEW
    assert [e.payload["name"] for e in res.events if e.kind == "check"] == ["install", "test"]
    assert res.data["installed_digest"] != first_install
    # Same digest again: no reinstall, no park.
    res2 = await ex.run(item(Stage.VERIFY, data={**data, **res.data}))
    assert [e.payload["name"] for e in res2.events if e.kind == "check"] == ["test"]


async def test_unapproved_manifest_blocks_install_at_implement_start(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package.json").write_text("{}\n")
    ws.commit(wt, "sneak")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert runner.calls == [] and res.events == []  # no install, no agent


async def test_pr_open_rechecks_manifests(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, ado, ws, _ = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package.json").write_text("{}\n")
    ws.commit(wt, "x")
    res = await ex.run(item(Stage.PR_OPEN, data={"plan": "p", "checks": []}))
    assert res.transition.park_reason is ParkReason.MANIFEST and ado.prs == {}


async def test_legacy_installed_flag_reinstalls_once(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, *_ = parts
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", "installed": True}))
    assert [e.payload["name"] for e in res.events if e.kind == "check"] == ["install"]
    again = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", **res.data}))
    assert [e for e in again.events if e.kind == "check"] == []


async def test_awaiting_records_pr_comment_events(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, _, decider, _ = parts
    pr = ado.create_pr("agent/5-add-feature", "t", "b", 5)
    ado.pr_threads[pr] = [PrComment(1, 1, "Brian", "/agent rename it")]
    res = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))
    assert [(e.kind, e.payload["intent"]) for e in res.events] == [
        ("pr_comment", "change_request")]


# --- fix round 1 ---------------------------------------------------------------------------


async def test_verify_rechecks_policy_before_manifest_and_install(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    """A protected-path violation committed alongside an approved manifest change must still
    park POLICY at verify, before the manifest gate lets install and checks run (finding 1)."""
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "infra").mkdir()
    (wt / "infra" / "x.bicep").write_text("x")
    (wt / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
    ws.commit(wt, "sneak")
    digest = ws.blob_digest(wt, ["package.json"])
    res = await ex.run(item(Stage.VERIFY, data={"manifest_approved": digest}))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "infra/x.bicep" in res.transition.note
    assert [e for e in res.events if e.kind == "check"] == []  # no install ran
    assert runner.calls == []


async def test_stopped_note_quotes_the_escalating_denial(parts) -> None:  # type: ignore[no-untyped-def]
    """The escalating denial is usually last; the quoted note and last_denials must keep it
    even when more than _PARK_DENIALS denials preceded it (finding 2)."""
    ex, _, ws, _, runner = parts
    ws.create(5, "agent/5-add-feature")
    denials = tuple(
        Denial("Bash", "command_not_allowlisted", f"command not allowlisted: cmd{i}")
        for i in range(6)
    ) + (Denial("Read", "outside_worktree", "path is outside the worktree",
               '{"file_path": "/Users/x/.ssh/config"}'),)
    runner.behaviors["implementer"] = lambda r, p, c: AgentResult(
        "", Usage(1, 5, 5), denials, escalated="outside_worktree")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "outside_worktree" in res.transition.note and ".ssh/config" in res.transition.note
    assert res.data["last_denials"][-1] == {
        "role": "implementer", "tool": "Read", "category": "outside_worktree",
        "reason": "path is outside the worktree", "input": '{"file_path": "/Users/x/.ssh/config"}'}


# --- final-review fix wave: I1 escalated+is_error, I2 manifest resume stage, I3 approval diff ---


async def test_i1_escalated_error_result_parks_policy(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, _, runner = parts
    ws.create(5, "agent/5-add-feature")
    denial = Denial("Read", "outside_worktree", "path is outside the worktree", "{}")
    runner.behaviors["implementer"] = lambda r, p, c: AgentResult(
        "", Usage(1, 1, 1), is_error=True, error="error_during_execution",
        escalated="outside_worktree", denials=(denial,))
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.POLICY


def _with_lock_manifests(target: TargetConfig) -> TargetConfig:
    return target.model_copy(update={"policy": target.policy.model_copy(
        update={"manifest_paths": ["**/package.json", "**/package-lock.json"]})})


async def test_i2_manifest_gate_records_resume_stage(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package.json").write_text("{}\n")
    ws.commit(wt, "sneak")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", "feedback": "fix it"}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert res.data["manifest_resume"] == "implement"

    def add_dep(r, p, cwd):  # type: ignore[no-untyped-def]
        (cwd / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        return AgentResult("done", Usage(1, 1, 1))

    runner.behaviors["implementer"] = add_dep
    approved = {"manifest_approved": res.data["manifest_pending"]}
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", **approved}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert res.data["manifest_resume"] == "verify"


async def test_i3_manifest_diff_shows_package_json_then_lock_stat(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, _ = _executor(tmp_path, _with_lock_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package-lock.json").write_text(
        "".join(f'    "node_modules/pkg-{i}": {{"version": "1.0.{i}"}},\n' for i in range(2000)))
    (wt / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
    ws.commit(wt, "deps")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    diff = res.data["manifest_diff"]
    assert diff.startswith("diff --git a/package.json b/package.json") and "left-pad" in diff
    assert "package-lock.json" in diff and "1 file changed" in diff
    assert "pkg-1999" not in diff


async def test_i3_manifest_diff_is_truncated_with_marker(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, _ = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    deps = ",\n".join(f'  "pkg-{i}": "1.0.{i}"' for i in range(1000))
    (wt / "package.json").write_text(f'{{"dependencies": {{\n{deps}\n}}}}\n')
    ws.commit(wt, "deps")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    diff = res.data["manifest_diff"]
    assert diff.endswith("\n…(truncated)") and len(diff) == 6000 + len("\n…(truncated)")


async def test_f1_lockfile_only_change_shows_full_diff(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, _ = _executor(tmp_path, _with_lock_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package-lock.json").write_text('{"lockfileVersion": 3, "left-pad": "1.0.0"}\n')
    ws.commit(wt, "deps")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    diff = res.data["manifest_diff"]
    assert diff.startswith("diff --git a/package-lock.json b/package-lock.json")
    assert any(line.startswith("+") and "left-pad" in line for line in diff.splitlines())
