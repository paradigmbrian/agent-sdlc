from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_sdlc.config import GlobalConfig, GlobalLimits, load_config, load_single

TARGET = """\
name: {name}
forge: {{kind: ado, org: o, project: p, repo: r}}
repo: {{install: "true", commands: {{test: "true"}}}}
policy: {{protected_paths: ["infra/**"]}}
"""


def _write(tmp_path: Path, name: str, file: str | None = None) -> Path:
    p = tmp_path / "targets" / (file or f"{name}.yaml")
    p.parent.mkdir(exist_ok=True)
    p.write_text(TARGET.format(name=name))
    return p


def test_load_config_resolves_targets_relative_to_the_file(tmp_path: Path) -> None:
    _write(tmp_path, "a")
    _write(tmp_path, "b")
    cfg_file = tmp_path / "agent-sdlc.yaml"
    cfg_file.write_text("targets: [targets/a.yaml, targets/b.yaml]\n"
                        "limits: {max_concurrent_sessions: 2, max_daily_tokens: 7}\n")
    loaded = load_config(cfg_file)
    assert [t.name for t in loaded.targets] == ["a", "b"]
    assert loaded.config.limits.max_concurrent_sessions == 2
    assert loaded.config.limits.max_daily_tokens == 7
    assert loaded.config.auth.mode == "subscription"
    assert loaded.target("b").name == "b"
    with pytest.raises(KeyError):
        loaded.target("zzz")


def test_duplicate_target_names_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "a")
    _write(tmp_path, "a", file="copy.yaml")
    cfg_file = tmp_path / "agent-sdlc.yaml"
    cfg_file.write_text("targets: [targets/a.yaml, targets/copy.yaml]\n")
    with pytest.raises(ValueError, match="duplicate target names: a"):
        load_config(cfg_file)


def test_load_single_uses_global_defaults(tmp_path: Path) -> None:
    loaded = load_single(_write(tmp_path, "solo"))
    assert [t.name for t in loaded.targets] == ["solo"]
    assert loaded.config == GlobalConfig()
    assert loaded.config.limits == GlobalLimits()
    assert loaded.config.limits.max_daily_agent_turns == 400
    assert loaded.config.laya.default_threshold == 0.8


def test_concurrent_sessions_at_least_one() -> None:
    with pytest.raises(ValidationError):
        GlobalLimits(max_concurrent_sessions=0)


def test_repo_agent_sdlc_yaml_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    loaded = load_config(root / "agent-sdlc.yaml")
    assert "rallysource" in [t.name for t in loaded.targets]
