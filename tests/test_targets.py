from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from laya_sdlc.targets import RunWindow, TargetConfig, load_target

ROOT = Path(__file__).resolve().parents[1]

MINIMAL = {
    "name": "t",
    "ado": {"org": "o", "project": "p", "repo": "r"},
    "repo": {"install": "true", "commands": {"test": "true"}},
    "policy": {"protected_paths": ["infra/**"]},
}


def test_loads_pilot_target() -> None:
    cfg = load_target(ROOT / "targets" / "rallysource.yaml")
    assert cfg.ado.org == "MilesThurman"
    assert cfg.ado.base_branch == "dev"
    assert cfg.ado.branch_prefix == "laya/"
    assert cfg.clone_url == "https://dev.azure.com/MilesThurman/CodvoMigration/_git/RallySource"
    assert list(cfg.repo.commands) == ["test", "lint", "typecheck", "build"]
    assert "**/prisma/migrations/**" in cfg.policy.protected_paths
    assert cfg.limits.max_concurrent_items == 1
    assert cfg.auth.mode == "subscription"


def test_defaults_applied() -> None:
    cfg = TargetConfig.model_validate(MINIMAL)
    assert cfg.limits.max_verify_retries == 3
    assert cfg.limits.max_turns == {"plan": 30, "implement": 80, "review": 30}
    assert cfg.laya.default_threshold == 0.8
    assert cfg.ado.parked_tag == "laya:parked"


def test_clone_url_override() -> None:
    repo = {**MINIMAL["repo"], "clone_url": "/tmp/x.git"}
    cfg = TargetConfig.model_validate({**MINIMAL, "repo": repo})
    assert cfg.clone_url == "/tmp/x.git"


def test_rejects_empty_commands() -> None:
    with pytest.raises(ValidationError):
        TargetConfig.model_validate({**MINIMAL, "repo": {"install": "true", "commands": {}}})


def test_rejects_unknown_auth_mode() -> None:
    with pytest.raises(ValidationError):
        TargetConfig.model_validate({**MINIMAL, "auth": {"mode": "password"}})


def test_run_window_same_day_and_wrapping() -> None:
    day = RunWindow(start=time(9), end=time(17))
    assert day.contains(time(12)) and not day.contains(time(18))
    night = RunWindow(start=time(19), end=time(7))
    assert night.contains(time(23)) and night.contains(time(3)) and not night.contains(time(12))
