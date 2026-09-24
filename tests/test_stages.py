from dataclasses import replace
from pathlib import Path

import pytest

from laya_sdlc.orchestrator.stages import StageExecutor
from laya_sdlc.policy import PathPolicy
from laya_sdlc.targets import TargetConfig
from laya_sdlc.types import AgentResult, Item, ParkReason, PrComment, Stage, Usage, WorkItem
from laya_sdlc.workspaces import Workspaces
from tests.fakes import FakeAdo, FakeDecider, FakeRunner

WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("laya",), "u")


@pytest.fixture
def parts(tmp_path: Path, target: TargetConfig, origin_repo: Path):  # type: ignore[no-untyped-def]
    ado = FakeAdo(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    decider, runner = FakeDecider(), FakeRunner()
    ex = StageExecutor(target=target, ado=ado, decider=decider, runner=runner, workspaces=ws,
                       path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [])
    return ex, ado, ws, decider, runner


def item(stage: Stage, **kw) -> Item:  # type: ignore[no-untyped-def]
    return replace(Item(5, "fixture", WI.title, "laya/5-add-feature", stage), **kw)


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
    ws.create(5, "laya/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.to is Stage.VERIFY
    assert ws.changed_files(ws.worktree_path(5)) == ["feature.txt"]
    assert res.data["installed"] is True


async def test_implement_protected_path_parks(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, _, runner = parts
    ws.create(5, "laya/5-add-feature")

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
    wt = ws.create(5, "laya/5-add-feature")
    (wt / "broken.txt").write_text("x")
    ws.commit(wt, "feat: broken")
    res = await ex.run(item(Stage.VERIFY))
    assert res.transition.to is Stage.IMPLEMENT and "broken.txt present" in res.transition.feedback
    assert res.data["checks"][0]["exit_code"] == 1


async def test_review_then_pr_open(parts, origin_repo: Path) -> None:  # type: ignore[no-untyped-def]
    ex, ado, ws, *_ = parts
    wt = ws.create(5, "laya/5-add-feature")
    (wt / "feature.txt").write_text("x")
    ws.commit(wt, "feat: x")
    checks = [{"name": "test", "command": "sh check.sh", "exit_code": 0, "output": "ok",
               "duration_s": 0.1}]
    res = await ex.run(item(Stage.REVIEW, data={"plan": "p", "checks": checks}))
    assert res.transition.to is Stage.PR_OPEN
    res = await ex.run(item(Stage.PR_OPEN, data={"plan": "p", "checks": checks,
                                                 "review_notes": "No blocking issues."}))
    assert res.transition.to is Stage.AWAITING_HUMAN and res.pr_id == 100
    assert ado.prs[100]["branch"] == "laya/5-add-feature"
    assert ado.wi_comments and "PR !100" in ado.wi_comments[0][1]


async def test_pr_open_updates_existing_pr(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, ws, *_ = parts
    wt = ws.create(5, "laya/5-add-feature")
    (wt / "feature.txt").write_text("x")
    ws.commit(wt, "feat: x")
    pr = ado.create_pr("laya/5-add-feature", "t", "b", 5)
    res = await ex.run(item(Stage.PR_OPEN, pr_id=pr, data={"plan": "p", "checks": []}))
    assert res.pr_id == pr and ado.prs[pr]["updates"] == 1


async def test_awaiting_handles_comments(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, _, decider, _ = parts
    pr = ado.create_pr("laya/5-add-feature", "t", "b", 5)
    ado.pr_threads[pr] = [PrComment(1, 1, "Brian", "/laya rename it"),
                          PrComment(2, 1, "Brian", "why this approach?")]
    decider.answers["comment"] = {"comment_intent": "question"}
    res = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))
    assert res.transition.to is Stage.IMPLEMENT and "/laya rename it" in res.transition.feedback
    assert res.data["seen_comments"] == ["1:1", "2:1"]
    assert [t for _, t, _ in ado.replies] == [2]
    assert [lab.gold for lab in res.labels] == ["change_request"]
    again = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr, data=res.data))
    assert again.transition.to is Stage.AWAITING_HUMAN


async def test_awaiting_completed(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, *_ = parts
    pr = ado.create_pr("laya/5-add-feature", "t", "b", 5)
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


class DryRunAdo(FakeAdo):
    def create_pr(self, branch: str, title: str, body: str, work_item_id: int) -> int:
        return 0


async def test_m6_dry_run_does_not_comment_plan(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ado = DryRunAdo(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    ex = StageExecutor(target=target, ado=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [])
    wt = ws.create(5, "laya/5-add-feature")
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
    ado = FakeAdo(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", fixer)
    ex = StageExecutor(target=fixer, ado=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(fixer.policy.protected_paths),
                       decisions_for=lambda _id: [])
    res = await ex.run(item(Stage.VERIFY))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "300" in res.transition.note and "200" in res.transition.note
