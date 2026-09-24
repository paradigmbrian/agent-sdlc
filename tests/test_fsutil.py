from pathlib import Path

from agent_sdlc.fsutil import ensure_private_dir


def test_creates_every_missing_level_0700(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    ensure_private_dir(target)
    for d in (tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "c"):
        assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_preexisting_directory_keeps_its_mode(tmp_path: Path) -> None:
    existing = tmp_path / "a"
    existing.mkdir(mode=0o755)
    target = existing / "b" / "c"
    ensure_private_dir(target)
    assert oct(existing.stat().st_mode & 0o777) == "0o755"
    assert oct((existing / "b").stat().st_mode & 0o777) == "0o700"
    assert oct(target.stat().st_mode & 0o777) == "0o700"
