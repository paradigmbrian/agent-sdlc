import pytest

from agent_sdlc.adapters.ado import AdoForge
from agent_sdlc.adapters.github import GitHubForge
from agent_sdlc.config import GlobalConfig
from agent_sdlc.orchestrator.runtime import claude_auth_env, make_forge
from agent_sdlc.targets import TargetConfig

BASE = {"repo": {"install": "true", "commands": {"test": "true"}},
        "policy": {"protected_paths": ["infra/**"]}}


def secrets(store: dict[str, str]):  # type: ignore[no-untyped-def]
    def get(service: str, env: str) -> str:
        return store[service]
    return get


def test_make_forge_ado_uses_the_configured_pat_secret() -> None:
    t = TargetConfig.model_validate({**BASE, "name": "a", "forge": {
        "kind": "ado", "org": "o", "project": "p", "repo": "r", "pat_secret": "my-pat"}})
    f = make_forge(t, dry_run_push=True, secret=secrets({"my-pat": "pat"}))
    assert isinstance(f, AdoForge) and f.kind == "ado"


def test_make_forge_github_reads_the_app_key_lazily() -> None:
    t = TargetConfig.model_validate({**BASE, "name": "g", "forge": {
        "kind": "github", "owner": "o", "repo": "r", "app_id": 42}})
    f = make_forge(t, dry_run_push=True,
                   secret=secrets({"agent-sdlc-github-app-42": "-----BEGIN KEY-----"}))
    assert isinstance(f, GitHubForge)     # no network until a token is needed


def test_make_forge_github_without_app_id_explains() -> None:
    t = TargetConfig.model_validate({**BASE, "name": "g", "forge": {
        "kind": "github", "owner": "o", "repo": "r"}})
    with pytest.raises(ValueError, match="set forge.app_id for target g"):
        make_forge(t, dry_run_push=True, secret=secrets({}))


def test_claude_auth_env_by_mode() -> None:
    sub = claude_auth_env(GlobalConfig(), secret=secrets({"agent-sdlc-claude-token": "tok"}))
    assert sub == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    key = claude_auth_env(GlobalConfig.model_validate({"auth": {"mode": "api_key"}}),
                          secret=secrets({"agent-sdlc-anthropic-key": "k"}))
    assert key == {"ANTHROPIC_API_KEY": "k"}
