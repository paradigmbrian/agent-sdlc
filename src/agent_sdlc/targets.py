from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class AdoConfig(BaseModel):
    org: str
    project: str
    repo: str
    intake_tag: str = "agent"
    parked_tag: str = "agent:parked"
    base_branch: str = "dev"
    branch_prefix: str = "agent/"

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
    # Changes to these park the item for human approval before install/verify (spec §5.2).
    manifest_paths: list[str] = Field(default_factory=list)
    max_diff_lines: int = 600

    @model_validator(mode="after")
    def _disjoint(self) -> PolicyConfig:
        both = sorted(set(self.protected_paths) & set(self.manifest_paths))
        if both:
            raise ValueError(f"paths cannot be both protected and manifest: {both}")
        return self


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
    max_denials_per_session: int = 5
    stale_after_minutes: int = 120


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
