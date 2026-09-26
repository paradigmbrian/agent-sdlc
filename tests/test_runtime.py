from pathlib import Path

import pytest

from agent_sdlc.adapters.ado import AdoForge
from agent_sdlc.adapters.github import GitHubForge
from agent_sdlc.config import GlobalConfig
from agent_sdlc.orchestrator.runtime import build_scheduler, claude_auth_env, make_forge
from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from tests.fakes import FakeDecider, FakeForge

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


def test_i3_build_scheduler_wires_shared_slots_should_stop_and_forge_auth(tmp_path: Path) -> None:
    """build_scheduler must give the executor the *same* slots and should_stop object the
    runner gets, and hand Workspaces the forge's own git_auth_header callable (I3)."""
    t = TargetConfig.model_validate({**BASE, "name": "t", "forge": {
        "kind": "ado", "org": "o", "project": "p", "repo": "r"}})
    store = Store("sqlite://")
    fake_forge = FakeForge()
    fake_forge.git_auth_header = lambda: "AUTH-FOR-T"  # type: ignore[method-assign]
    slots = SessionSlots(3)
    sched = build_scheduler(t, cfg=GlobalConfig(), store=store, decider=FakeDecider(),
                            slots=slots, workspaces=tmp_path, traces=None, dry_run_push=True,
                            auth_env={}, forge=fake_forge)
    executor = sched._executor  # noqa: SLF001 - inspecting private wiring is the point of I3
    runner = executor._runner  # noqa: SLF001

    assert executor._slots is slots  # noqa: SLF001
    assert runner._should_stop is executor._should_stop  # noqa: SLF001 - one shared should_stop

    assert executor._should_stop() is False  # noqa: SLF001
    store.set_flag("paused:other", "1")
    assert executor._should_stop() is False  # noqa: SLF001 - another target's pause is ignored
    store.set_flag("paused:t", "1")
    assert executor._should_stop() is True  # noqa: SLF001
    assert runner._should_stop() is True  # noqa: SLF001 - the runner sees the same flip

    assert sched._ws._auth is not None and sched._ws._auth() == "AUTH-FOR-T"  # noqa: SLF001


def test_claude_auth_env_by_mode() -> None:
    sub = claude_auth_env(GlobalConfig(), secret=secrets({"agent-sdlc-claude-token": "tok"}))
    assert sub == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    key = claude_auth_env(GlobalConfig.model_validate({"auth": {"mode": "api_key"}}),
                          secret=secrets({"agent-sdlc-anthropic-key": "k"}))
    assert key == {"ANTHROPIC_API_KEY": "k"}
