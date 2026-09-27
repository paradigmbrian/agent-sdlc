import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from agent_sdlc.adapters.dry_run import DryRunForge
from agent_sdlc.cli import main
from agent_sdlc.store import Store
from agent_sdlc.types import PrComment, Stage, WorkItem
from tests.fakes import FakeDecider, FakeForge, FakeRunner

WI = WorkItem(5, "Add feature", "d", "ac", "Bug", ("agent",), "u")


def test_dry_run_forge_reads_through_and_skips_writes(tmp_path: Path) -> None:
    inner = FakeForge()
    inner.add(WI)
    f = DryRunForge(inner)
    assert f.kind == "ado" and f.label_word == "tag"
    assert f.list_intake() == [WI] and f.get_item(5) == WI and f.has_label(5, "agent")
    f.comment_item(5, "x")
    f.set_label(5, "agent:parked", True)
    f.push_branch(tmp_path, "agent/5-x")
    assert f.create_pr("agent/5-x", "t", "b", 5) == 0
    f.update_pr(0, "b", 5)
    f.reply_pr(0, PrComment(1, 1, "a", "c"), "x")
    f.comment_pr(0, "x")
    f.delete_branch("agent/5-x")
    assert f.pr_status(0) == "active" and f.pr_comments(0) == []
    assert inner.wi_comments == [] and inner.prs == {} and inner.replies == []
    assert inner.tags[5] == {"agent"} and inner.deleted_branches == []


def test_dry_run_forge_reads_real_prs(tmp_path: Path) -> None:
    inner = FakeForge()
    pr = inner.create_pr("agent/5-x", "t", "b", 5)
    inner.prs[pr]["status"] = "completed"
    assert DryRunForge(inner).pr_status(pr) == "completed"


TARGET = """name: fixture
forge: {{kind: ado, org: o, project: p, repo: r}}
repo:
  base_branch: dev
  clone_url: {origin}
  install: "true"
  commands: {{test: sh check.sh}}
policy: {{protected_paths: ["infra/**"]}}
"""


def _db_files(db: Path) -> dict[str, bytes]:
    # -shm is SQLite's shared-memory reader index: any reader touches it (including the
    # snapshot's read-only connection), and it holds no data, so it is excluded here.
    return {p.name: p.read_bytes() for p in db.parent.glob(db.name + "*")
            if not p.name.endswith("-shm")}


def _leftovers() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("agent-sdlc-dry-run-*"))


def test_cli_dry_run_is_isolated(
    tmp_path: Path, origin_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agent_sdlc.cli as cli_mod
    import agent_sdlc.orchestrator.runtime as rt

    spy = FakeForge()                      # no origin: a real push would fail loudly
    spy.add(WI)
    monkeypatch.setenv("AGENT_SDLC_ADO_PAT", "pat")
    monkeypatch.setattr(rt, "AdoForge", lambda *a, **k: spy)
    monkeypatch.setattr(rt, "ClaudeAgentRunner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr(cli_mod, "claude_auth_env", lambda cfg: {})
    monkeypatch.setattr(cli_mod, "_decider", lambda cfg, store: FakeDecider())
    target = tmp_path / "t.yaml"
    target.write_text(TARGET.format(origin=origin_repo))
    db = tmp_path / "state.db"
    store = Store(f"sqlite:///{db}")
    it = store.add_item("fixture", WI, "agent/5-add-feature")
    assert it is not None
    store.save(replace(it, stage=Stage.PR_OPEN, data={"plan": "p", "checks": []}))
    before, temps = _db_files(db), _leftovers()

    rc = main(["--target", str(target), "--db", f"sqlite:///{db}", "run", "--once",
               "--dry-run"])

    assert rc == 0
    assert _db_files(db) == before
    assert Store(f"sqlite:///{db}").get_by_ref("fixture", 5).stage is Stage.PR_OPEN
    assert spy.prs == {} and spy.wi_comments == [] and spy.tags[5] == {"agent"}
    assert _leftovers() == temps
    out = capsys.readouterr().out
    kept = Path(out.split("dry run: traces kept in ", 1)[1].strip())
    assert kept.is_dir() and kept.parts[-3] == "dry-run"


def test_cli_dry_run_needs_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    rc = main(["--target", str(root / "targets" / "rallysource.yaml"),
               "--db", f"sqlite:///{tmp_path / 's.db'}", "run", "--dry-run"])
    assert rc == 1 and "--dry-run needs --once" in capsys.readouterr().out


def test_cli_dry_run_needs_sqlite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    rc = main(["--target", str(root / "targets" / "rallysource.yaml"),
               "--db", "postgresql://h/db", "run", "--once", "--dry-run"])
    assert rc == 1 and "--dry-run needs a SQLite --db" in capsys.readouterr().out
