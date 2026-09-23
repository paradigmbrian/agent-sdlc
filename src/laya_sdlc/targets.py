from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class AdoConfig(BaseModel):
    org: str
    project: str
    repo: str
    intake_tag: str = "laya"
    parked_tag: str = "laya:parked"
    base_branch: str = "dev"
    branch_prefix: str = "laya/"

    @property
    def repo_https_url(self) -> str:
        return f"https://dev.azure.com/{self.org}/{self.project}/_git/{self.repo}"


class RepoConfig(BaseModel):
    clone_url: str | None = None
    install: str
    commands: dict[str, str]
    command_timeout_s: int = 1200
    env_template: Path | None = None

    @field_validator("commands")
    @classmethod
    def _non_empty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("at least one verify command is required")
        return v


class PolicyConfig(BaseModel):
    protected_paths: list[str]
    max_diff_lines: int = 600


class RunWindow(BaseModel):
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


class Limits(BaseModel):
    max_concurrent_items: int = 1
    max_verify_retries: int = 3
    max_pr_rounds: int = 3
    max_turns: dict[str, int] = Field(
        default_factory=lambda: {"plan": 30, "implement": 80, "review": 30})
    max_item_tokens: int = 2_000_000
    max_daily_agent_turns: int = 400
    max_daily_tokens: int = 20_000_000
    run_window: RunWindow | None = None


class AuthConfig(BaseModel):
    mode: Literal["subscription", "api_key"] = "subscription"


class LayaConfig(BaseModel):
    model: str = "auto"
    max_ece: float = 0.10
    default_threshold: float = 0.8


class TargetConfig(BaseModel):
    name: str
    ado: AdoConfig
    repo: RepoConfig
    policy: PolicyConfig
    limits: Limits = Field(default_factory=Limits)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    laya: LayaConfig = Field(default_factory=LayaConfig)

    @property
    def clone_url(self) -> str:
        return self.repo.clone_url or self.ado.repo_https_url


def load_target(path: Path) -> TargetConfig:
    return TargetConfig.model_validate(yaml.safe_load(path.read_text()))
