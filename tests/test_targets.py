from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_sdlc.policy import PathPolicy
from agent_sdlc.targets import (
    AdoForgeConfig,
    GitHubForgeConfig,
    PolicyConfig,
    RunWindow,
    TargetConfig,
    load_target,
)

ROOT = Path(__file__).resolve().parents[1]

MINIMAL = {
    "name": "t",
    "forge": {"kind": "ado", "org": "o", "project": "p", "repo": "r"},
    "repo": {"install": "true", "commands": {"test": "true"}},
    "policy": {"protected_paths": ["infra/**"]},
}
GITHUB = {**MINIMAL, "forge": {"kind": "github", "owner": "paradigmbrian",
                                "repo": "triathlon-agent", "app_id": 42}}


def test_loads_pilot_target() -> None:
    cfg = load_target(ROOT / "targets" / "rallysource.yaml")
    assert isinstance(cfg.forge, AdoForgeConfig)
    assert cfg.forge.org == "MilesThurman"
    assert cfg.repo.base_branch == "dev"
    assert cfg.repo.branch_prefix == "agent/"
    assert cfg.repo.install == ["npm ci"]
    assert cfg.intake.label == "agent" and cfg.intake.parked_label == "agent:parked"
    assert cfg.clone_url == "https://dev.azure.com/MilesThurman/CodvoMigration/_git/RallySource"
    assert list(cfg.repo.commands) == ["test", "lint", "typecheck", "build"]
    assert "**/prisma/migrations/**" in cfg.policy.protected_paths
    assert cfg.limits.max_concurrent_items == 1


def test_defaults_applied() -> None:
    cfg = TargetConfig.model_validate(MINIMAL)
    assert cfg.limits.max_verify_retries == 3
    assert cfg.limits.max_turns == {"plan": 30, "implement": 80, "review": 30}
    assert cfg.intake.parked_label == "agent:parked"
    assert cfg.repo.base_branch == "main" and cfg.repo.branch_prefix == "agent/"
    assert cfg.repo.install == ["true"]


def test_clone_url_override() -> None:
    repo = {**MINIMAL["repo"], "clone_url": "/tmp/x.git"}
    cfg = TargetConfig.model_validate({**MINIMAL, "repo": repo})
    assert cfg.clone_url == "/tmp/x.git"


def test_github_forge_derives_clone_url() -> None:
    cfg = TargetConfig.model_validate(GITHUB)
    assert isinstance(cfg.forge, GitHubForgeConfig)
    assert cfg.clone_url == "https://github.com/paradigmbrian/triathlon-agent.git"
    assert cfg.forge.installation_id is None
    assert cfg.forge.api_url == "https://api.github.com"


def test_github_app_id_may_be_null_but_not_zero() -> None:
    forge = {k: v for k, v in GITHUB["forge"].items() if k != "app_id"}
    assert TargetConfig.model_validate({**GITHUB, "forge": forge}).forge.app_id is None
    with pytest.raises(ValidationError):
        TargetConfig.model_validate({**GITHUB, "forge": {**forge, "app_id": 0}})


def test_rejects_unknown_forge_kind() -> None:
    with pytest.raises(ValidationError):
        TargetConfig.model_validate({**MINIMAL, "forge": {"kind": "gitlab", "repo": "r"}})


@pytest.mark.parametrize("extra,key", [
    ({"auth": {"mode": "subscription"}}, "auth"),
    ({"laya": {"model": "auto"}}, "laya"),
    ({"limits": {"max_daily_tokens": 5}}, "limits.max_daily_tokens"),
    ({"limits": {"max_daily_agent_turns": 5}}, "limits.max_daily_agent_turns"),
    ({"limits": {"run_window": None}}, "limits.run_window"),
])
def test_rejects_global_keys_in_target_file(extra: dict[str, object], key: str) -> None:
    with pytest.raises(ValidationError, match=f"{key}.*agent-sdlc.yaml"):
        TargetConfig.model_validate({**MINIMAL, **extra})


def test_rejects_old_ado_block() -> None:
    old = {k: v for k, v in MINIMAL.items() if k != "forge"}
    with pytest.raises(ValidationError, match="'ado' was replaced by 'forge'"):
        TargetConfig.model_validate({**old, "ado": {"org": "o", "project": "p", "repo": "r"}})


def test_install_list_and_empty_install() -> None:
    repo = {**MINIMAL["repo"], "install": ["uv sync", "npm ci --prefix web"]}
    cfg = TargetConfig.model_validate({**MINIMAL, "repo": repo})
    assert cfg.repo.install == ["uv sync", "npm ci --prefix web"]
    for bad in ([], [" "]):
        with pytest.raises(ValidationError):
            TargetConfig.model_validate({**MINIMAL, "repo": {**repo, "install": bad}})


def test_rejects_empty_commands() -> None:
    with pytest.raises(ValidationError):
        TargetConfig.model_validate({**MINIMAL, "repo": {"install": "true", "commands": {}}})


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
