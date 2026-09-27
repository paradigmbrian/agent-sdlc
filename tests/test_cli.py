import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_sdlc.cli import main
from agent_sdlc.store import Store
from agent_sdlc.types import ParkReason, Stage, WorkItem

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'state.db'}"


def run(db: str, *args: str) -> int:
    return main(["--target", str(ROOT / "targets" / "rallysource.yaml"), "--db", db, *args])


def test_pause_resume(db: str) -> None:
    assert run(db, "pause") == 0
    assert Store(db).get_flag("paused") == "1"
    assert run(db, "resume") == 0
    assert Store(db).get_flag("paused") is None


def test_status_lists_items(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "agent/9-fix-it")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "#9" in out and "triage" in out and "paused: no" in out


def test_requeue_without_ado_resets_locally(db: str) -> None:
    store = Store(db)
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    store.save(replace(store.get_by_ref("rallysource", 9), stage=Stage.PARKED,
                       park_reason=ParkReason.RED, parked_from=Stage.VERIFY))
    assert run(db, "requeue", "9", "--local") == 0
    assert Store(db).get_by_ref("rallysource", 9).stage is Stage.VERIFY


# --- final review fix wave ---------------------------------------------------------------


def test_m12_defaults_resolve_from_project_root(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_sdlc.cli import _parser
    monkeypatch.delenv("AGENT_SDLC_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_SDLC_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    args = _parser().parse_args(["status"])
    assert Path(args.config) == ROOT / "agent-sdlc.yaml" and args.target is None
    assert Path(args.workspaces) == ROOT / "workspaces"
    assert main(["--db", db, "status"]) == 0
    assert "target: rallysource" in capsys.readouterr().out


def test_trace_command(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "agent/9-a")
    store.add_event("intake", {"branch": "agent/9-a"}, item=store.get_by_ref("rallysource", 9))
    assert run(db, "trace", "9") == 0
    assert "branch agent/9-a" in capsys.readouterr().out
    assert run(db, "trace", "404") == 1
    assert "no item 404" in capsys.readouterr().out


def test_status_shows_last_tick_and_stale(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    now = datetime.now(UTC)
    store.set_flag("last_tick:rallysource", (now - timedelta(minutes=10)).isoformat())
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    item = store.get_by_ref("rallysource", 9)
    store.save(replace(item, stage=Stage.PLAN, data={"denial_counts": {"planner": 2}}))
    store.add_event("intake", {}, item=item, ts=now - timedelta(hours=3))
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "last tick: 10m ago  LOOP NOT RUNNING?" in out
    assert "denied 2" in out and "STALE" in out


def test_f4_status_marks_queued_item_instead_of_stale(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(db)
    now = datetime.now(UTC)
    for i in (9, 10):
        store.add_item("rallysource", WorkItem(i, "Fix it", "", "", "Bug", (), "u"), f"b{i}")
        item = store.get_by_ref("rallysource", i)
        store.save(replace(item, stage=Stage.PLAN))
        store.add_event("intake", {}, item=item, ts=now - timedelta(hours=3))
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("#")}
    assert "STALE" in lines["#9"] and "STALE" not in lines["#10"]
    assert "queued" in lines["#10"] and "queued" not in lines["#9"]


def test_pause_and_local_requeue_write_events(db: str) -> None:
    assert run(db, "pause") == 0
    store = Store(db)
    assert [e.kind for e in store.events_since(datetime(2000, 1, 1, tzinfo=UTC))] == ["pause"]
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    item = store.get_by_ref("rallysource", 9)
    store.save(replace(item, stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run(db, "requeue", "9", "--local") == 0
    [rq] = Store(db).events_for(item.id)
    assert rq.kind == "requeue" and rq.payload["to"] == "verify"


def test_cli_writes_log_file(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SDLC_LOGS", str(tmp_path / "logs"))
    assert run(db, "status") == 0
    logging.getLogger("agent_sdlc.cli_test").info("hello from test")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello from test" in (tmp_path / "logs" / "agent-sdlc.log").read_text()


def test_metrics_command(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "metrics", "--days", "7") == 0
    assert "Outcomes" in capsys.readouterr().out


def test_status_lists_each_busy_item(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(minutes=10)
    store.set_flag("last_tick:rallysource", datetime.now(UTC).isoformat())
    store.set_flag("busy:rallysource:9", f"implement|{since.isoformat()}")
    store.set_flag("busy:rallysource:12", f"verify|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: rallysource#9 implement for 10m" in out
    assert "busy: rallysource#12 verify for 10m" in out
    assert "LOOP NOT RUNNING?" not in out and "STUCK?" not in out


def test_status_busy_past_stale_limit_is_stuck(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(hours=3)
    store.set_flag("last_tick:rallysource", since.isoformat())
    store.set_flag("busy:rallysource:9", f"implement|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: rallysource#9 implement for 3h  STUCK?" in out and "LOOP NOT RUNNING?" in out


def test_pause_one_target(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "pause", "--target-name", "rallysource") == 0
    store = Store(db)
    assert store.get_flag("paused:rallysource") == "1" and store.get_flag("paused") is None
    assert run(db, "resume") == 0                       # global resume leaves it paused
    assert Store(db).get_flag("paused:rallysource") == "1"
    assert run(db, "resume", "--target-name", "rallysource") == 0
    assert Store(db).get_flag("paused:rallysource") is None


TARGET_YAML = """\
name: {name}
forge: {forge}
repo: {{install: "true", commands: {{test: "true"}}}}
policy: {{protected_paths: ["infra/**"]}}
"""


@pytest.fixture
def two(tmp_path: Path) -> Path:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a.yaml").write_text(TARGET_YAML.format(
        name="rally", forge="{kind: ado, org: o, project: p, repo: r}"))
    (tmp_path / "t" / "b.yaml").write_text(TARGET_YAML.format(
        name="tri", forge="{kind: github, owner: o, repo: r, app_id: 1}"))
    cfg = tmp_path / "agent-sdlc.yaml"
    cfg.write_text("targets: [t/a.yaml, t/b.yaml]\n")
    return cfg


def run_cfg(cfg: Path, db: str, *args: str) -> int:
    return main(["--config", str(cfg), "--db", db, *args])


def test_resolve_ref(db: str) -> None:
    from agent_sdlc.cli import resolve_ref
    store = Store(db)
    store.add_item("rally", WorkItem(5, "A", "", "", "Bug", (), "u"), "b1")
    store.add_item("tri", WorkItem(5, "B", "", "", "Bug", (), "u"), "b2")
    store.add_item("tri", WorkItem(6, "C", "", "", "Bug", (), "u"), "b3")
    names = ["rally", "tri"]
    assert resolve_ref(store, names, "tri#5").title == "B"
    assert resolve_ref(store, names, "6").title == "C"
    with pytest.raises(LookupError, match="rally#5, tri#5"):
        resolve_ref(store, names, "5")
    with pytest.raises(LookupError, match="no item nope#5"):
        resolve_ref(store, names, "nope#5")
    with pytest.raises(LookupError, match="no item 404"):
        resolve_ref(store, names, "404")
    with pytest.raises(LookupError, match="use <target>#<id>"):
        resolve_ref(store, names, "abc")


def test_status_groups_targets(two: Path, db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rally", WorkItem(5, "Ado thing", "", "", "Bug", (), "u"), "b1")
    store.add_item("tri", WorkItem(5, "Gh thing", "", "", "Bug", (), "u"), "b2")
    store.set_flag("paused:tri", "1")
    assert run_cfg(two, db, "status") == 0
    out = capsys.readouterr().out
    assert "target: rally  forge: ado  paused: no" in out
    assert "target: tri  forge: github  paused: yes" in out
    assert out.index("Ado thing") < out.index("target: tri") < out.index("Gh thing")
    assert run_cfg(two, db, "status", "--target-name", "tri") == 0
    assert "target: rally" not in capsys.readouterr().out


def test_trace_by_ref_and_ambiguous(two: Path, db: str,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rally", WorkItem(5, "A", "", "", "Bug", (), "u"), "agent/5-a")
    store.add_item("tri", WorkItem(5, "B", "", "", "Bug", (), "u"), "agent/5-b")
    assert run_cfg(two, db, "trace", "tri#5") == 0
    assert 'tri#5 "B"' in capsys.readouterr().out
    assert run_cfg(two, db, "trace", "5") == 1
    assert "rally#5, tri#5" in capsys.readouterr().out


@pytest.fixture
def one_broken_target(tmp_path: Path) -> Path:
    (tmp_path / "t").mkdir()
    # No app_id: make_forge raises ValueError for this target (spec §7), before any secret
    # or network access, so run --once fails fast and deterministically.
    (tmp_path / "t" / "b.yaml").write_text(TARGET_YAML.format(
        name="tri", forge="{kind: github, owner: o, repo: r}"))
    cfg = tmp_path / "agent-sdlc.yaml"
    cfg.write_text("targets: [t/b.yaml]\n")
    return cfg


def test_m4_run_once_exits_nonzero_when_a_target_fails_to_build(
    one_broken_target: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_sdlc.cli as cli_mod
    from tests.fakes import FakeDecider
    # Avoid constructing the real (heavy) Laya-backed decider: irrelevant to this failure,
    # which happens while building the target's forge, before any decision is made.
    monkeypatch.setattr(cli_mod, "_decider", lambda cfg, store: FakeDecider())
    assert run_cfg(one_broken_target, db, "run", "--once") == 1


def test_requeue_local_by_ref(two: Path, db: str) -> None:
    store = Store(db)
    it = store.add_item("tri", WorkItem(7, "B", "", "", "Bug", (), "u"), "b")
    assert it is not None
    store.save(replace(it, stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run_cfg(two, db, "requeue", "tri#7", "--local") == 0
    assert Store(db).get_by_ref("tri", 7).stage is Stage.VERIFY


# --- M-1: unknown --target-name errors instead of silently misbehaving ----------------------


def test_m1_pause_unknown_target_name_errors(
    two: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(two, db, "pause", "--target-name", "bogus") == 1
    assert "unknown target bogus (known: rally, tri)" in capsys.readouterr().out
    assert Store(db).get_flag("paused:bogus") is None


def test_m1_resume_unknown_target_name_errors(
    two: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(two, db, "resume", "--target-name", "bogus") == 1
    assert "unknown target bogus (known: rally, tri)" in capsys.readouterr().out


def test_m1_status_unknown_target_name_errors(
    two: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(two, db, "status", "--target-name", "bogus") == 1
    assert "unknown target bogus (known: rally, tri)" in capsys.readouterr().out


def test_m1_metrics_unknown_target_name_errors(
    two: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(two, db, "metrics", "--target-name", "bogus") == 1
    assert "unknown target bogus (known: rally, tri)" in capsys.readouterr().out


def test_m1_label_unknown_target_name_errors(
    two: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(two, db, "label", "triage", "--target-name", "bogus") == 1
    assert "unknown target bogus (known: rally, tri)" in capsys.readouterr().out


# --- M-2: global pause/resume must work even when a target file fails to load ---------------


@pytest.fixture
def one_bad_target(tmp_path: Path) -> Path:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a.yaml").write_text(TARGET_YAML.format(
        name="rally", forge="{kind: ado, org: o, project: p, repo: r}"))
    (tmp_path / "t" / "bad.yaml").write_text("name: broken\n")  # missing forge/repo/policy
    cfg = tmp_path / "agent-sdlc.yaml"
    cfg.write_text("targets: [t/a.yaml, t/bad.yaml]\n")
    return cfg


def test_m2_global_pause_works_when_a_target_file_is_invalid(one_bad_target: Path,
                                                              db: str) -> None:
    assert run_cfg(one_bad_target, db, "pause") == 0
    assert Store(db).get_flag("paused") == "1"


def test_m2_global_resume_works_when_a_target_file_is_invalid(one_bad_target: Path,
                                                               db: str) -> None:
    assert run_cfg(one_bad_target, db, "resume") == 0
    assert Store(db).get_flag("paused") is None


def test_m2_pause_with_target_name_and_invalid_config_warns_but_still_sets_flag(
    one_bad_target: Path, db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cfg(one_bad_target, db, "pause", "--target-name", "rally") == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower()
    assert Store(db).get_flag("paused:rally") == "1"
