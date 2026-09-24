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
    store.save(replace(store.get(9), stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run(db, "requeue", "9", "--local") == 0
    assert Store(db).get(9).stage is Stage.VERIFY


# --- final review fix wave ---------------------------------------------------------------


def test_m12_defaults_resolve_from_project_root(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_sdlc.cli import _parser
    monkeypatch.delenv("AGENT_SDLC_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    args = _parser().parse_args(["status"])
    assert Path(args.target) == ROOT / "targets" / "rallysource.yaml"
    assert Path(args.workspaces) == ROOT / "workspaces"
    assert main(["--db", db, "status"]) == 0
    assert "target: rallysource" in capsys.readouterr().out


def test_trace_command(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "agent/9-a")
    store.add_event("intake", {"branch": "agent/9-a"}, item=store.get(9))
    assert run(db, "trace", "9") == 0
    assert "branch agent/9-a" in capsys.readouterr().out
    assert run(db, "trace", "404") == 1
    assert "no item #404" in capsys.readouterr().out


def test_status_shows_last_tick_and_stale(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    now = datetime.now(UTC)
    store.set_flag("last_tick", (now - timedelta(minutes=10)).isoformat())
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    store.save(replace(store.get(9), stage=Stage.PLAN,
                       data={"denial_counts": {"planner": 2}}))
    store.add_event("intake", {}, item=store.get(9), ts=now - timedelta(hours=3))
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "last tick: 10m ago  LOOP NOT RUNNING?" in out
    assert "denied 2" in out and "STALE" in out


def test_pause_and_local_requeue_write_events(db: str) -> None:
    assert run(db, "pause") == 0
    store = Store(db)
    assert [e.kind for e in store.events_since(datetime(2000, 1, 1, tzinfo=UTC))] == ["pause"]
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    store.save(replace(store.get(9), stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run(db, "requeue", "9", "--local") == 0
    [rq] = Store(db).events_for(9)
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


def test_status_busy_suppresses_loop_warning(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(minutes=10)
    store.set_flag("last_tick", since.isoformat())
    store.set_flag("busy", f"9|implement|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: #9 implement for 10m" in out
    assert "LOOP NOT RUNNING?" not in out and "STUCK?" not in out


def test_status_busy_past_stale_limit_is_stuck(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(hours=3)
    store.set_flag("last_tick", since.isoformat())
    store.set_flag("busy", f"9|implement|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: #9 implement for 3h  STUCK?" in out and "LOOP NOT RUNNING?" in out
