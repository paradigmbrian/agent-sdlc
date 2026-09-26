from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from agent_sdlc.targets import RunWindow, TargetConfig, load_target


class AuthConfig(BaseModel):
    mode: Literal["subscription", "api_key"] = "subscription"


class LayaConfig(BaseModel):
    model: str = "auto"
    max_ece: float = 0.10
    default_threshold: float = 0.8


class GlobalLimits(BaseModel):
    """Limits of the one Claude subscription, shared by every target (spec §2.1)."""
    max_daily_agent_turns: int = 400
    max_daily_tokens: int = 20_000_000
    max_concurrent_sessions: int = Field(default=1, ge=1)
    run_window: RunWindow | None = None


class GlobalConfig(BaseModel):
    targets: list[Path] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    laya: LayaConfig = Field(default_factory=LayaConfig)
    limits: GlobalLimits = Field(default_factory=GlobalLimits)


@dataclass(frozen=True)
class Loaded:
    config: GlobalConfig
    targets: list[TargetConfig]

    def target(self, name: str) -> TargetConfig:
        for t in self.targets:
            if t.name == name:
                return t
        raise KeyError(name)


def _check_unique(targets: list[TargetConfig]) -> None:
    names = [t.name for t in targets]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ValueError(f"duplicate target names: {', '.join(dup)}")


def load_config(path: Path) -> Loaded:
    cfg = GlobalConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    targets = [load_target(p if p.is_absolute() else path.parent / p) for p in cfg.targets]
    _check_unique(targets)
    return Loaded(cfg, targets)


def load_single(target_path: Path) -> Loaded:
    return Loaded(GlobalConfig(), [load_target(target_path)])
