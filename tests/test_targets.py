from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_sdlc.policy import PathPolicy
from agent_sdlc.targets import PolicyConfig, RunWindow, TargetConfig, load_target

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
    assert cfg.ado.branch_prefix == "agent/"
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
    assert cfg.ado.parked_tag == "agent:parked"


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


def test_rallysource_protects_tooling_config_but_not_app_config() -> None:
    t = load_target(ROOT / "targets" / "rallysource.yaml")
    pp = PathPolicy(t.policy.protected_paths)
    for p in ["eslint.config.js", "commitlint.config.js", "apps/rallysource-web/vite.config.ts",
              "apps/rallysource-teams/tailwind.config.ts", "apps/rallysource-web/postcss.config.js",
              "apps/rallysource-api/eslint.config.mjs", "packages/eslint-config/base.js",
              "turbo.json", ".npmrc", "apps/rallysource-api/.npmrc",
              "apps/rallysource-api/nest-cli.json",
              "apps/rallysource-web/tsconfig.json", "apps/rallysource-teams/tsconfig.app.json",
              "apps/rallysource-api/tsconfig.build.json", "packages/tsconfig/tsconfig.base.json",
              ".prettierrc", ".prettierrc.cjs", "apps/rallysource-web/.postcssrc.js", ".babelrc",
              "apps/rallysource-api/.eslintrc.json", "prettier.config.js", ".config/x.json"]:
        assert pp.is_protected(p), p
    for p in ["apps/rallysource-api/src/config/app.config.ts", "package.json",
              "apps/rallysource-api/package.json", "apps/rallysource-web/src/App.tsx",
              "apps/rallysource-api/src/tsconfig-helpers.ts",
              "apps/rallysource-web/src/rc.ts", "apps/rallysource-web/src/source.ts"]:
        assert not pp.is_protected(p), p
    mp = PathPolicy(t.policy.manifest_paths)
    assert mp.violations(["package.json", "apps/rallysource-api/package.json",
                          "package-lock.json", "src/a.ts"]) == [
        "apps/rallysource-api/package.json", "package-lock.json", "package.json"]
    assert t.limits.max_denials_per_session == 5 and t.limits.stale_after_minutes == 120


def test_policy_rejects_path_both_protected_and_manifest() -> None:
    with pytest.raises(ValidationError):
        PolicyConfig(protected_paths=["**/package.json"], manifest_paths=["**/package.json"])
