from datetime import UTC, datetime
from pathlib import Path

from agent_sdlc.orchestrator.scheduler import Scheduler
from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.orchestrator.supervisor import Supervisor
from agent_sdlc.policy import PathPolicy
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import PrComment, Stage, WorkItem
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git
from tests.fakes import FakeDecider, FakeForge, FakeRunner

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("agent",), "u")


def _clone_origin(tmp_path: Path, origin: Path, name: str) -> Path:
    dst = tmp_path / f"{name}.git"
    git("clone", "-q", "--bare", str(origin), str(dst), cwd=tmp_path)
    return dst


def _build(target: TargetConfig, store: Store, forge: FakeForge, root: Path,
           slots: SessionSlots | None = None,
           decider: FakeDecider | None = None) -> Scheduler:
    ws = Workspaces(root / "ws", target)
    ex = StageExecutor(target=target, forge=forge, decider=decider or FakeDecider(),
                       runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=store.decisions_for, slots=slots)
    return Scheduler(target=target, store=store, executor=ex, forge=forge, workspaces=ws,
                     clock=lambda: NOW)


async def test_github_flavour_to_pr_and_review_round(tmp_path: Path, target: TargetConfig,
                                                     origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'e2e.db'}")
    gh = target.model_copy(update={"name": "gh"})
    forge = FakeForge(origin=origin_repo, kind="github")
    forge.add(WI)
    sched = _build(gh, store, forge, tmp_path)
    for _ in range(6):
        await sched.tick()
    item = store.get_by_ref("gh", 5)
    assert item.stage is Stage.AWAITING_HUMAN
    pr = forge.prs[100]
    assert pr["title"].endswith("(#5)") and "AB#" not in pr["body"]
    assert pr["body"].startswith("Automated change for #5 ")
    assert pr["body"].endswith("Closes #5")
    assert any("for PR #100" in c for _, c in forge.wi_comments)
    forge.pr_threads[100].append(PrComment(0, 1, "brian", "(changes requested with no summary)",
                                           kind="review", changes_requested=True))
    await sched.tick()  # awaiting poll -> implement, then implement runs in the same tick
    assert store.get_by_ref("gh", 5).stage is Stage.VERIFY


async def test_github_park_comment_says_label(tmp_path: Path, target: TargetConfig,
                                              origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'e2e.db'}")
    gh = target.model_copy(update={"name": "gh"})
    forge = FakeForge(origin=origin_repo, kind="github")
    forge.add(WI)
    sched = _build(gh, store, forge, tmp_path, decider=FakeDecider(shadow={"triage"}))
    await sched.tick()
    assert store.get_by_ref("gh", 5).stage is Stage.PARKED
    assert "agent:parked" in forge.tags[5]
    assert any("<code>agent:parked</code> label" in c for _, c in forge.wi_comments)


def test_two_targets_in_one_supervisor(tmp_path: Path, target: TargetConfig,
                                       origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'two.db'}")
    slots = SessionSlots(1, poll_s=0.01)
    targets, forges = {}, {}
    for name, kind in (("rally", "ado"), ("tri", "github")):
        t = target.model_copy(update={"name": name, "repo": target.repo.model_copy(
            update={"clone_url": str(_clone_origin(tmp_path, origin_repo, name))})})
        f = FakeForge(origin=Path(str(t.repo.clone_url)), kind=kind)
        f.add(WI)
        targets[name], forges[name] = t, f

    def build(t: TargetConfig) -> Scheduler:
        return _build(t, store, forges[t.name], tmp_path / t.name, slots)

    sup = Supervisor(list(targets.values()), build)
    for _ in range(6):
        sup.run_once()
    for name in ("rally", "tri"):
        assert store.get_by_ref(name, 5).stage is Stage.AWAITING_HUMAN, name
    assert forges["rally"].prs[100]["title"].endswith("(AB#5)")
    assert forges["tri"].prs[100]["body"].endswith("Closes #5")
