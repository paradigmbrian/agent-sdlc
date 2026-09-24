import subprocess
from pathlib import Path

import pytest

from agent_sdlc.targets import TargetConfig


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path_factory: pytest.TempPathFactory,
                         monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests must never write logs or traces under the real ~/.agent-sdlc."""
    base = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("AGENT_SDLC_LOGS", str(base / "logs"))
    monkeypatch.setenv("AGENT_SDLC_TRACES", str(base / "traces"))


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "dev", cwd=src)
    (src / "check.sh").write_text(
        '#!/bin/sh\nif [ -f broken.txt ]; then echo "broken.txt present"; exit 1; fi\necho ok\n'
    )
    (src / "README.md").write_text("fixture\n")
    git("add", "-A", cwd=src)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def target(origin_repo: Path) -> TargetConfig:
    return TargetConfig.model_validate(
        {
            "name": "fixture",
            "ado": {"org": "o", "project": "p", "repo": "r"},
            "repo": {
                "clone_url": str(origin_repo),
                "install": "true",
                "commands": {"test": "sh check.sh"},
                "command_timeout_s": 30,
            },
            "policy": {"protected_paths": ["infra/**", "**/.env*"], "max_diff_lines": 200},
        }
    )
