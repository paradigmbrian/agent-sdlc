from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Keys that moved to agent-sdlc.yaml (multi-forge spec §2.2).
_GLOBAL_TOP = ("auth", "laya")
_GLOBAL_LIMITS = ("max_daily_agent_turns", "max_daily_tokens", "run_window")


class AdoForgeConfig(BaseModel):
    kind: Literal["ado"]
    org: str
    project: str
    repo: str
    pat_secret: str = "agent-sdlc-ado-pat"

    @property
    def clone_url(self) -> str:
        return f"https://dev.azure.com/{self.org}/{self.project}/_git/{self.repo}"


class GitHubForgeConfig(BaseModel):
    kind: Literal["github"]
    owner: str
    repo: str
    app_id: int | None = Field(default=None, gt=0)  # null until the App exists (spec §7)
    installation_id: int | None = None
    api_url: str = "https://api.github.com"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


ForgeConfig = Annotated[AdoForgeConfig | GitHubForgeConfig, Field(discriminator="kind")]


class IntakeConfig(BaseModel):
    label: str = "agent"
    parked_label: str = "agent:parked"


class RepoConfig(BaseModel):
    base_branch: str = "main"
    branch_prefix: str = "agent/"
    clone_url: str | None = None
    install: list[str]
    commands: dict[str, str]
    command_timeout_s: int = 1200
    env_template: Path | None = None

    @field_validator("install", mode="before")
    @classmethod
    def _install_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @field_validator("install")
    @classmethod
    def _install_non_empty(cls, v: list[str]) -> list[str]:
        if not v or not all(c.strip() for c in v):
            raise ValueError("install needs at least one non-empty command")
        return v

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
    max_denials_per_session: int = 5
    stale_after_minutes: int = 120


class TargetConfig(BaseModel):
    name: str
    forge: ForgeConfig
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    repo: RepoConfig
    policy: PolicyConfig
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="before")
    @classmethod
    def _no_global_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "ado" in data:
            raise ValueError("'ado' was replaced by 'forge' (kind: ado), 'intake' and 'repo'")
        found = [k for k in _GLOBAL_TOP if k in data]
        limits = data.get("limits") or {}
        found += [f"limits.{k}" for k in _GLOBAL_LIMITS if k in limits]
        if found:
            raise ValueError(f"{', '.join(found)} belong in agent-sdlc.yaml, not a target file")
        return data

    @property
    def clone_url(self) -> str:
        return self.repo.clone_url or self.forge.clone_url


def load_target(path: Path) -> TargetConfig:
    return TargetConfig.model_validate(yaml.safe_load(path.read_text()))
