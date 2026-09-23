from dataclasses import replace
from pathlib import Path

import pytest

from laya_sdlc.cli import main
from laya_sdlc.store import Store
from laya_sdlc.types import ParkReason, Stage, WorkItem

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
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "laya/9-fix-it")
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
    from laya_sdlc.cli import _parser
    monkeypatch.delenv("LAYA_SDLC_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    args = _parser().parse_args(["status"])
    assert Path(args.target) == ROOT / "targets" / "rallysource.yaml"
    assert Path(args.workspaces) == ROOT / "workspaces"
    assert main(["--db", db, "status"]) == 0
    assert "target: rallysource" in capsys.readouterr().out
