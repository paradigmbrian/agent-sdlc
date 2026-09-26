# Multiple Forges and Parallel Targets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each agent-sdlc target use Azure DevOps or GitHub (Issues + PRs via a GitHub App), make
the repo explicit per target, and serve several targets from one process in parallel, starting with
`paradigmbrian/triathlon-agent`.

**Architecture:** A provider-neutral `ForgePort` replaces `AdoPort`, implemented by `AdoForge` (the
existing ADO client, renamed) and a new `GitHubForge` (httpx + GitHub App installation tokens). A new
`agent-sdlc.yaml` holds subscription-wide settings; target files hold per-repo settings. Items are
keyed by `(target, external_id)` in a fresh DB. A `Supervisor` runs one thread per target, sharing
one Laya decider (locked), one session semaphore, the kill switch and the daily budget.

**Tech Stack:** Python 3.12, uv, pydantic 2.13, SQLAlchemy 2.0 (SQLite, WAL), httpx 0.28, respx
0.23 (tests), PyJWT 2.15 with the `crypto` extra (new), claude-agent-sdk, laya, pytest, ruff, mypy
(strict).

**Spec:** `docs/superpowers/specs/2026-09-26-multi-forge-design.md` (read it before Task 1; this
plan argues from it). Base specs: `2026-09-23-agent-sdlc-design.md`,
`2026-09-24-trace-and-guardrails-design.md`.

## Global Constraints

- Branch: all work on `feat/multi-forge`. Never push (ask Brian first for any push).
- Baseline before Task 1: `uv run pytest -q` → 374 passed, 3 deselected; `uv run ruff check .` and
  `uv run mypy` clean. Every task ends with all three green.
- GitHub REST: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2026-03-10` (verified
  against docs.github.com on 2026-09-26; its breaking changes remove only fields this code never
  reads: `merge_commit_sha`, singular `assignee`, `rate`).
- GitHub App JWT: RS256, `iat` = now − 60 s, `exp` = now + 540 s (docs require ≤ 10 min), `iss` =
  the App id as a string. Installation tokens last 1 hour; do not assume a token length (GitHub's
  2026 stateless `ghs_APPID_JWT` format is longer than 40 chars).
- New dependency: `pyjwt[crypto]>=2.15.0` (adds `cryptography`). No other new runtime deps.
- Secrets (keychain service / env var): ADO PAT `forge.pat_secret` (default `agent-sdlc-ado-pat`) /
  `AGENT_SDLC_ADO_PAT`; GitHub App key `agent-sdlc-github-app-<app_id>` /
  `AGENT_SDLC_GITHUB_APP_KEY`. Never log, persist, comment or put in exceptions any token or key.
- New default DB: `~/.agent-sdlc/agent-sdlc-v2.db`. Never open or modify `~/.agent-sdlc/state.db`.
- Pushes and branch deletes only for refs under `refs/heads/<repo.branch_prefix>`.
- Code style: ruff line length 100, `from __future__ import annotations`, strict mypy, comments only
  where the reason is non-obvious (match existing modules).
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- Existing tests change only by the mechanical renames each task lists, or where the spec changes
  the asserted behaviour (named in the task). Never weaken an assertion to get green.

## Review Focus

1. A parked label containing `:` (`agent:parked`) must be URL-encoded when removed from a GitHub
   issue (`DELETE …/labels/agent%3Aparked`), or removal 404s and the item never requeues. Test in
   Task 6.
2. A GitHub issue with a `null` or empty body must map to an empty description and no acceptance
   criteria, not crash. Test in Task 6.
3. More than 100 open intake issues or PR comments must all be read (pagination), or items and
   change requests are silently missed. Test in Task 6 (issues) and Task 7 (comments).
4. The git auth header must be fetched per git call, so a verify longer than the token's life
   does not push with an expired token. Test in Task 2 (Workspaces calls the callable every
   authenticated git command).
5. `/agent` at the start of a review body (not only a conversation comment) must count as a change
   request. Test in Task 3.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/agent_sdlc/targets.py` | modify | Target file models: `forge` union, `intake`, `repo`, `policy`, per-target `limits`; rejects global keys |
| `src/agent_sdlc/config.py` | create | `agent-sdlc.yaml` models (`auth`, `laya`, global `limits`), `load_config`, `load_single` |
| `src/agent_sdlc/adapters/errors.py` | create | `ForgeError`, `redact()` |
| `src/agent_sdlc/adapters/ado.py` | modify | `AdoClient` → `AdoForge` implementing `ForgePort` |
| `src/agent_sdlc/adapters/github_auth.py` | create | GitHub App JWT, installation token cache, bot login |
| `src/agent_sdlc/adapters/github.py` | create | `GitHubForge` implementing `ForgePort` |
| `src/agent_sdlc/ports.py` | modify | `ForgePort` replaces `AdoPort` |
| `src/agent_sdlc/types.py` | modify | `PrComment.kind/changes_requested`, `Item.external_id` |
| `src/agent_sdlc/store.py` | modify | surrogate item key, `external_id`, `labels.target`, WAL + busy timeout |
| `src/agent_sdlc/workspaces.py` | modify | install list, `repo.base_branch`, per-call git auth |
| `src/agent_sdlc/orchestrator/reporting.py` | modify | forge-specific wording |
| `src/agent_sdlc/orchestrator/transitions.py` | modify | changes-requested review is a change request |
| `src/agent_sdlc/orchestrator/stages.py` | modify | `forge`, `external_id`, session slots |
| `src/agent_sdlc/orchestrator/scheduler.py` | modify | `forge`, global limits, per-target flags, stoppable loop |
| `src/agent_sdlc/orchestrator/slots.py` | create | `SessionSlots` semaphore |
| `src/agent_sdlc/orchestrator/runtime.py` | create | build forge + scheduler for one target |
| `src/agent_sdlc/orchestrator/supervisor.py` | create | one thread per target, restart, run once |
| `src/agent_sdlc/decisions/decider.py` | modify | `LockedDecider` |
| `src/agent_sdlc/logctx.py` | modify | `[target#id stage]` log context |
| `src/agent_sdlc/tracing.py`, `labeling.py` | modify | refs, label target |
| `src/agent_sdlc/cli.py` | modify | `--config`, refs, grouped status, per-target pause, Supervisor |
| `agent-sdlc.yaml` | create | global config |
| `targets/rallysource.yaml` | modify | new shape, same values |
| `targets/triathlon.yaml` | create | GitHub target |
| `tests/fakes.py` | modify | `FakeForge` (ado or github flavour) |
| `tests/test_config.py`, `tests/test_github_auth.py`, `tests/test_github.py`, `tests/test_slots.py`, `tests/test_supervisor.py`, `tests/test_runtime.py`, `tests/e2e/test_multi_forge.py` | create | new tests |
| `README.md` | modify | GitHub setup, multi-target usage |

---

### Task 1: Split configuration into global and per-target files

**Files:**
- Modify: `src/agent_sdlc/targets.py` (whole file)
- Create: `src/agent_sdlc/config.py`
- Modify: `src/agent_sdlc/workspaces.py:135-190` (`create`, `install`, `_range`)
- Modify: `src/agent_sdlc/adapters/ado.py:51-60` (constructor) and every `self._cfg.intake_tag` / `branch_prefix` / `base_branch` / `repo_https_url` use
- Modify: `src/agent_sdlc/orchestrator/scheduler.py` (constructor, `_agent_work_allowed`, `_intake`, `_requeue_untagged`, `requeue_item`, `_park_side_effects`)
- Modify: `src/agent_sdlc/cli.py` (`_parser`, `_runtime`, `main`)
- Modify: `targets/rallysource.yaml`; Create: `agent-sdlc.yaml`
- Test: `tests/test_targets.py`, `tests/test_config.py` (new), `tests/test_workspaces.py`, `tests/test_scheduler.py`, `tests/test_ado.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `agent_sdlc.targets.{AdoForgeConfig, GitHubForgeConfig, ForgeConfig, IntakeConfig, RepoConfig, PolicyConfig, RunWindow, Limits, TargetConfig, load_target}`; `TargetConfig.clone_url -> str`; `RepoConfig.install: list[str]`, `.base_branch: str`, `.branch_prefix: str`.
- Produces: `agent_sdlc.config.{AuthConfig, LayaConfig, GlobalLimits, GlobalConfig, Loaded, load_config(path: Path) -> Loaded, load_single(target_path: Path) -> Loaded}`; `Loaded.config: GlobalConfig`, `Loaded.targets: list[TargetConfig]`, `Loaded.target(name: str) -> TargetConfig`.
- Produces: `Scheduler(..., limits: GlobalLimits | None = None)`.
- Produces: `AdoClient(cfg: AdoForgeConfig, pat: str, *, intake: IntakeConfig | None = None, base_branch: str = "dev", branch_prefix: str = "agent/", http=None, push_url=None, dry_run_push=False)`.

- [ ] **Step 1: Write the failing target-config tests**

Replace `MINIMAL`, `test_loads_pilot_target`, `test_defaults_applied`, `test_clone_url_override`
and `test_rejects_unknown_auth_mode` in `tests/test_targets.py`, and add the new tests below.
Keep the other tests in the file unchanged.

```python
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
```

Update the imports at the top of the file:

```python
from agent_sdlc.targets import (
    AdoForgeConfig,
    GitHubForgeConfig,
    PolicyConfig,
    RunWindow,
    TargetConfig,
    load_target,
)
```

- [ ] **Step 2: Write the failing global-config tests** — create `tests/test_config.py`:

```python
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
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_targets.py tests/test_config.py -q`
Expected: FAIL (ImportError on `AdoForgeConfig` / `agent_sdlc.config`).

- [ ] **Step 4: Rewrite `src/agent_sdlc/targets.py`**

```python
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
```

- [ ] **Step 5: Create `src/agent_sdlc/config.py`**

```python
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
```

- [ ] **Step 6: Rewrite `targets/rallysource.yaml` top blocks and `limits`; create `agent-sdlc.yaml`**

Replace everything above `policy:` in `targets/rallysource.yaml` with the block below, keep the
`policy:` block byte-for-byte, and replace everything from `limits:` to the end of the file with the
`limits:` block below (`auth` and `laya` move to `agent-sdlc.yaml`).

```yaml
name: rallysource
forge:
  kind: ado
  org: MilesThurman
  project: CodvoMigration
  repo: RallySource
intake:
  label: agent
  parked_label: "agent:parked"
repo:
  base_branch: dev
  branch_prefix: agent/
  install: [npm ci]
  commands:
    test: npm run test --workspace=apps/rallysource-api
    lint: npm run lint
    typecheck: npm run type-check --workspace=apps/rallysource-web --workspace=apps/rallysource-teams
    build: npm run build
  command_timeout_s: 1200
  env_template: null
```

```yaml
limits:
  max_concurrent_items: 1
  max_verify_retries: 3
  max_pr_rounds: 3
  max_turns: {plan: 30, implement: 80, review: 30}
  max_item_tokens: 2000000
  max_denials_per_session: 5
  stale_after_minutes: 120
```

Create `agent-sdlc.yaml` at the repository root:

```yaml
# Settings of the one Claude subscription and the one agent-sdlc process.
# Per-repo settings live in each target file (spec 2026-09-26 §2).
targets:
  - targets/rallysource.yaml
auth:
  mode: subscription            # subscription | api_key
laya:
  model: auto
  max_ece: 0.10
  default_threshold: 0.8
limits:
  max_daily_agent_turns: 400
  max_daily_tokens: 20000000
  max_concurrent_sessions: 1    # agent sessions across all targets
  run_window: null              # e.g. {start: "19:00", end: "07:00"}
```

- [ ] **Step 7: Update the consumers**

`src/agent_sdlc/workspaces.py` — in `create` replace `f"origin/{self._t.ado.base_branch}"` with
`f"origin/{self._t.repo.base_branch}"`; in `_range` replace `self._t.ado.base_branch` with
`self._t.repo.base_branch`; replace `install` with:

```python
    def install(self, wt: Path, log: Path | None = None) -> CommandResult:
        """Run each install command in order, stopping at the first failure (spec §2.2)."""
        start = time.monotonic()
        parts: list[str] = []
        code = 0
        for cmd in self._t.repo.install:
            r = self.run("install", cmd, wt)
            parts.append(f"$ {cmd}\n{r.output}")
            code = r.exit_code
            if not r.ok:
                break
        command = " ; ".join(self._t.repo.install)
        out = "\n".join(parts)
        written = _write_log(log, command, out, code) if log is not None else None
        return CommandResult("install", command, code, out[-_OUTPUT_TAIL:],
                             round(time.monotonic() - start, 2), written)
```

`src/agent_sdlc/adapters/ado.py` — change the import to
`from agent_sdlc.targets import AdoForgeConfig, IntakeConfig` and the constructor to:

```python
class AdoClient:
    def __init__(self, cfg: AdoForgeConfig, pat: str, *, intake: IntakeConfig | None = None,
                 base_branch: str = "dev", branch_prefix: str = "agent/",
                 http: httpx.Client | None = None, push_url: str | None = None,
                 dry_run_push: bool = False) -> None:
        self._cfg = cfg
        self._intake = intake or IntakeConfig()
        self._base_branch = base_branch
        self._branch_prefix = branch_prefix
        self._http = http or httpx.Client(base_url=f"https://dev.azure.com/{cfg.org}",
                                          auth=("", pat), timeout=30)
        self._auth_header = basic_auth_header(pat)
        self._push_url = push_url or cfg.clone_url
        self._dry_run = dry_run_push
        self._self_id: str | None = None
```

and in the body replace `self._cfg.branch_prefix` → `self._branch_prefix`,
`self._cfg.intake_tag` → `self._intake.label`, `self._cfg.base_branch` → `self._base_branch`.

`src/agent_sdlc/orchestrator/scheduler.py` — add `from agent_sdlc.config import GlobalLimits`; add
the keyword parameter `limits: GlobalLimits | None = None` to `Scheduler.__init__` and store
`self._limits = limits or GlobalLimits()`; replace `_agent_work_allowed` with:

```python
    def _agent_work_allowed(self, now: datetime) -> bool:
        lim = self._limits
        if lim.run_window and not lim.run_window.contains(now.time()):
            return False
        used = self._store.daily_usage(now.date())
        return used.turns < lim.max_daily_agent_turns and used.tokens < lim.max_daily_tokens
```

and replace every `self._t.ado.parked_tag` → `self._t.intake.parked_label`,
`self._t.ado.branch_prefix` → `self._t.repo.branch_prefix`.

`src/agent_sdlc/cli.py`:
- add `from agent_sdlc.config import GlobalConfig, load_config`;
- in `_parser` add, before `--target`:
  `p.add_argument("--config", default=os.environ.get("AGENT_SDLC_CONFIG", str(_ROOT / "agent-sdlc.yaml")))`;
- add:

```python
def _global_config(path: str) -> GlobalConfig:
    p = Path(path)
    return load_config(p).config if p.exists() else GlobalConfig()
```

- change `_runtime(target, store, ...)` to `_runtime(cfg: GlobalConfig, target: TargetConfig, store, ...)`; inside it use `cfg.auth.mode`, `cfg.laya.model`, `cfg.laya.default_threshold`; build the client with
  `AdoClient(target.forge, pat, intake=target.intake, base_branch=target.repo.base_branch, branch_prefix=target.repo.branch_prefix, dry_run_push=dry_run_push)` after `assert isinstance(target.forge, AdoForgeConfig)` (import `AdoForgeConfig` from `agent_sdlc.targets`); build the command policy with `CommandPolicy([*target.repo.install, *target.repo.commands.values()])`; pass `limits=cfg.limits` to `Scheduler(...)`;
- in `main`, after `target = load_target(...)`, add `cfg = _global_config(args.config)`, pass `cfg` as the first argument of every `_runtime(...)` call, and in `calibrate` use `cfg.laya.max_ece` instead of `target.laya.max_ece`.

- [ ] **Step 8: Update existing tests for the new shape (mechanical)**

- `tests/conftest.py` `target` fixture: replace `"ado": {"org": "o", "project": "p", "repo": "r"},` with
  `"forge": {"kind": "ado", "org": "o", "project": "p", "repo": "r"},` and add
  `"base_branch": "dev",` as the first key of the `"repo"` dict (the fixture origin's branch is `dev`).
- `tests/test_ado.py`: `from agent_sdlc.targets import AdoForgeConfig, TargetConfig`;
  `CFG = AdoForgeConfig(kind="ado", org="MilesThurman", project="CodvoMigration", repo="RallySource")`.
  Every `AdoClient(CFG, ...)` call keeps working (defaults: base `dev`, prefix `agent/`, label `agent`).
- `tests/test_scheduler.py`: add `from agent_sdlc.config import GlobalLimits`. In
  `test_run_window_blocks_agent_stages_not_polling` delete the `night = …` assignment and construct
  `Scheduler(target=target, …, limits=GlobalLimits(run_window=RunWindow(start=time(19), end=time(7))))`.
  Any test that sets `max_daily_agent_turns` or `max_daily_tokens` via `target.limits.model_copy`
  passes `limits=GlobalLimits(<same key>=<same value>)` to `Scheduler(...)` instead
  (`grep -n "max_daily" tests/test_scheduler.py` lists them).

- [ ] **Step 9: Add a workspace test for install lists**

Append to `tests/test_workspaces.py`:

```python
def test_install_runs_each_command_and_stops_at_first_failure(
    tmp_path: Path, target: TargetConfig
) -> None:
    multi = target.model_copy(update={"repo": target.repo.model_copy(
        update={"install": ["echo one", "sh -c 'exit 3'", "echo never"]})})
    ws = Workspaces(tmp_path / "w", multi)
    wt = ws.create(1, "agent/1-a")
    r = ws.install(wt, log=tmp_path / "install.log")
    assert r.exit_code == 3 and "one" in r.output and "never" not in r.output
    assert r.command == "echo one ; sh -c 'exit 3' ; echo never"
    assert "$ echo one" in (tmp_path / "install.log").read_text()
```

- [ ] **Step 10: Run the whole suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass (374 + the new tests), ruff and mypy clean.

- [ ] **Step 11: Commit**

```bash
git add -A src tests targets agent-sdlc.yaml
git commit -m "feat: split config into agent-sdlc.yaml and per-target files with a forge union

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Forge port, `ForgeError` and `AdoForge`

**Files:**
- Create: `src/agent_sdlc/adapters/errors.py`
- Modify: `src/agent_sdlc/ports.py` (replace `AdoPort`)
- Modify: `src/agent_sdlc/types.py` (`PrComment`)
- Modify: `src/agent_sdlc/adapters/ado.py` (class rename, port methods)
- Modify: `src/agent_sdlc/workspaces.py` (`__init__`, `_git`)
- Modify: `src/agent_sdlc/orchestrator/stages.py`, `orchestrator/scheduler.py`, `labeling.py`, `cli.py` (use `forge`)
- Modify: `tests/fakes.py` (`FakeAdo` → `FakeForge`), and the tests listed in Step 7
- Test: `tests/test_ado.py`, `tests/test_workspaces.py`

**Interfaces:**
- Consumes: Task 1 `AdoClient` constructor (renamed here to `AdoForge`, same parameters).
- Produces: `agent_sdlc.adapters.errors.ForgeError(Exception)`, `redact(text: str, secrets: Iterable[str]) -> str`.
- Produces: `agent_sdlc.ports.ForgePort` (exact protocol in Step 3).
- Produces: `PrComment(thread_id: int, comment_id: int, author: str, content: str, kind: str = "thread", changes_requested: bool = False)`, `PrComment.key -> f"{kind}:{thread_id}:{comment_id}"`.
- Produces: `Workspaces(root: Path, target: TargetConfig, git_auth: Callable[[], str] | None = None)`.
- Produces: `StageExecutor(*, target, forge: ForgePort, decider, runner, workspaces, path_policy, decisions_for, traces=None, clock=None)`; `Scheduler(*, target, store, executor, forge: ForgePort, workspaces, clock=None, limits=None)`; `label_triage(forge: ForgePort, decider, store, limit, ask)`.
- Produces: `tests.fakes.FakeForge(origin: Path | None = None, kind: str = "ado", …)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ado.py` (after Step 7's renames these names exist):

```python
def test_ado_forge_port_attributes() -> None:
    client = AdoForge(CFG, "pat", http=httpx.Client(base_url=BASE))
    assert client.kind == "ado" and client.label_word == "tag"
    assert client.pr_ref(12) == "!12" and client.item_ref(5) == "AB#5"
    assert client.git_auth_header() == basic_auth_header("pat")


def test_redact_removes_secrets() -> None:
    assert redact("push failed for ghs_abc and key", ["ghs_abc", ""]) == \
        "push failed for [redacted] and key"


def test_pr_comment_key_includes_kind() -> None:
    assert PrComment(1, 2, "a", "c").key == "thread:1:2"
    assert PrComment(0, 9, "a", "c", kind="review").key == "review:0:9"
```

Append to `tests/test_workspaces.py`:

```python
def test_git_auth_callable_is_called_for_every_authenticated_git_call(
    tmp_path: Path, target: TargetConfig
) -> None:
    calls: list[int] = []

    def header() -> str:
        calls.append(1)
        return "X-Test: 1"

    ws = Workspaces(tmp_path / "w", target, git_auth=header)
    ws.create(1, "agent/1-a")   # clone (auth)
    ws.create(2, "agent/2-b")   # fetch (auth)
    assert len(calls) == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ado.py tests/test_workspaces.py -q`
Expected: FAIL (ImportError: `AdoForge`, `redact`; `Workspaces` has no `git_auth`).

- [ ] **Step 3: Create `errors.py` and the port**

`src/agent_sdlc/adapters/errors.py`:

```python
from __future__ import annotations

from collections.abc import Iterable


class ForgeError(Exception):
    """An ADO or GitHub call failed. Messages never contain tokens or keys."""


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return text
```

`src/agent_sdlc/ports.py` — replace the `AdoPort` class with:

```python
class ForgePort(Protocol):
    """Issue tracker + code host for one target: ADO work items/Repos or GitHub issues/PRs.
    Item ids are the provider's own numbers (Item.external_id)."""
    kind: str
    label_word: str

    def list_intake(self) -> list[WorkItem]: ...
    def list_closed(self, limit: int) -> list[WorkItem]: ...
    def get_item(self, id: int) -> WorkItem: ...
    def comment_item(self, id: int, html: str) -> None: ...
    def set_label(self, id: int, label: str, present: bool) -> None: ...
    def has_label(self, id: int, label: str) -> bool: ...
    def git_auth_header(self) -> str: ...
    def push_branch(self, worktree: Path, branch: str) -> None: ...
    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int: ...
    def update_pr(self, pr_id: int, body: str, item_id: int) -> None: ...
    def pr_status(self, pr_id: int) -> str: ...
    def pr_comments(self, pr_id: int) -> list[PrComment]: ...
    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None: ...
    def comment_pr(self, pr_id: int, text: str) -> None: ...
    def delete_branch(self, branch: str) -> None: ...
    def pr_ref(self, pr_id: int) -> str: ...
    def item_ref(self, item_id: int) -> str: ...
```

`src/agent_sdlc/types.py` — replace `PrComment` with:

```python
@dataclass(frozen=True)
class PrComment:
    thread_id: int
    comment_id: int
    author: str
    content: str
    kind: str = "thread"             # ado: thread · github: conversation | review_comment | review
    changes_requested: bool = False  # github: a review in state CHANGES_REQUESTED

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.thread_id}:{self.comment_id}"
```

- [ ] **Step 4: Turn `AdoClient` into `AdoForge`**

In `src/agent_sdlc/adapters/ado.py`:
- delete `class AdoError`; add `from agent_sdlc.adapters.errors import ForgeError`; replace every
  `AdoError(` with `ForgeError(`;
- rename the class `AdoClient` → `AdoForge` and add class attributes `kind = "ado"` and
  `label_word = "tag"`;
- rename methods: `get_work_item` → `get_item`, `comment_work_item(self, id, html_text)` →
  `comment_item(self, id: int, html: str)` (body `json={"text": html}`), `has_tag` → `has_label`
  (`return label in self.get_item(id).tags`), `set_tag` → `set_label` (same body, `tag` →
  `label`); internal callers of `get_work_item` use `get_item`;
- `create_pr(self, branch, title, body, item_id: int)`: rename `work_item_id` → `item_id`;
- replace `update_pr` and `reply_pr` and add the three port helpers:

```python
    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        # item_id is unused: ADO links the work item through workItemRefs on create.
        if self._dry_run:
            log.info("dry-run: would update PR %s description", pr_id)
            return
        self._req("PATCH", f"{self._repo}/pullrequests/{pr_id}", json={"description": body})

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        if self._dry_run:
            log.info("dry-run: would reply to PR %s thread %s: %s", pr_id, comment.thread_id,
                     text)
            return
        self._req("POST",
                  f"{self._repo}/pullRequests/{pr_id}/threads/{comment.thread_id}/comments",
                  json={"content": text, "parentCommentId": comment.comment_id,
                        "commentType": 1})

    def git_auth_header(self) -> str:
        return self._auth_header

    def pr_ref(self, pr_id: int) -> str:
        return f"!{pr_id}"

    def item_ref(self, item_id: int) -> str:
        return f"AB#{item_id}"
```

- [ ] **Step 5: Per-call git auth in `Workspaces`**

In `src/agent_sdlc/workspaces.py` change the constructor parameter
`git_auth_header: str | None = None` to `git_auth: Callable[[], str] | None = None`, store it as
`self._auth = git_auth`, and in `_git` replace the auth lines with:

```python
        if auth and self._auth is not None:
            # Fetched per call: GitHub installation tokens expire after an hour.
            cmd += ["-c", f"http.extraheader={self._auth()}"]
```

- [ ] **Step 6: Use the port in the orchestrator**

- `src/agent_sdlc/orchestrator/stages.py`: import `ForgePort` instead of `AdoPort`; constructor
  parameter `ado: AdoPort` → `forge: ForgePort`, attribute `self._ado` → `self._forge`; rename calls:
  `get_work_item` → `get_item`, `comment_work_item` → `comment_item`; in `_pr_open` call
  `self._forge.update_pr(item.pr_id, body, item.id)`; in `_awaiting` call
  `self._forge.reply_pr(item.pr_id, c, reply)`.
- `src/agent_sdlc/orchestrator/scheduler.py`: replace `from agent_sdlc.adapters.ado import AdoError`
  with `from agent_sdlc.adapters.errors import ForgeError`; `_INFRA_ERRORS = (httpx.HTTPError,
  GitError, ForgeError, OSError)`; constructor `ado: AdoPort` → `forge: ForgePort`,
  `self._ado` → `self._forge`; `has_tag` → `has_label`, `set_tag` → `set_label`,
  `comment_work_item` → `comment_item`.
- `src/agent_sdlc/labeling.py`: `AdoPort` → `ForgePort`; `label_triage(forge: ForgePort, …)` with
  `for wi in forge.list_closed(limit):`.
- `src/agent_sdlc/cli.py` `_runtime`: `AdoClient` → `AdoForge`; build Workspaces with
  `git_auth=ado.git_auth_header` (drop the `basic_auth_header` import if unused); pass `forge=ado`
  to `StageExecutor` and `Scheduler`; rename the local `ado` → `forge`.

- [ ] **Step 7: Replace `FakeAdo` with `FakeForge` and rename test call sites**

In `tests/fakes.py` replace the `FakeAdo` class with:

```python
@dataclass
class FakeForge:
    origin: Path | None = None                         # local bare repo to push into
    kind: str = "ado"                                  # "ado" | "github" wording
    items: dict[int, WorkItem] = field(default_factory=dict)
    tags: dict[int, set[str]] = field(default_factory=dict)
    wi_comments: list[tuple[int, str]] = field(default_factory=list)
    prs: dict[int, dict[str, Any]] = field(default_factory=dict)
    pr_threads: dict[int, list[PrComment]] = field(default_factory=dict)
    replies: list[tuple[int, int, str]] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)

    @property
    def label_word(self) -> str:
        return "tag" if self.kind == "ado" else "label"

    def pr_ref(self, pr_id: int) -> str:
        return f"!{pr_id}" if self.kind == "ado" else f"#{pr_id}"

    def item_ref(self, item_id: int) -> str:
        return f"AB#{item_id}" if self.kind == "ado" else f"#{item_id}"

    def git_auth_header(self) -> str:
        return ""

    def add(self, wi: WorkItem) -> None:
        self.items[wi.id] = wi
        self.tags[wi.id] = set(wi.tags)

    def list_intake(self) -> list[WorkItem]:
        return [wi for i, wi in self.items.items() if "agent" in self.tags[i]]

    def list_closed(self, limit: int) -> list[WorkItem]:
        return list(self.items.values())[:limit]

    def get_item(self, id: int) -> WorkItem:
        return self.items[id]

    def comment_item(self, id: int, html: str) -> None:
        self.wi_comments.append((id, html))

    def set_label(self, id: int, label: str, present: bool) -> None:
        (self.tags[id].add if present else self.tags[id].discard)(label)

    def has_label(self, id: int, label: str) -> bool:
        return label in self.tags[id]

    def push_branch(self, worktree: Path, branch: str) -> None:
        assert branch.startswith("agent/")
        if self.origin is not None:
            subprocess.run(["git", "push", str(self.origin), f"HEAD:refs/heads/{branch}"],
                           cwd=worktree, check=True, capture_output=True)

    def _body(self, body: str, item_id: int) -> str:
        return body if self.kind == "ado" else f"{body}\n\nCloses #{item_id}"

    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        pr_id = 100 + len(self.prs)
        self.prs[pr_id] = {"branch": branch, "title": title, "body": self._body(body, item_id),
                           "status": "active", "work_item": item_id, "updates": 0}
        self.pr_threads[pr_id] = []
        return pr_id

    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        self.prs[pr_id]["body"] = self._body(body, item_id)
        self.prs[pr_id]["updates"] += 1

    def pr_status(self, pr_id: int) -> str:
        return str(self.prs[pr_id]["status"])

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        return list(self.pr_threads[pr_id])

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        self.replies.append((pr_id, comment.thread_id, text))

    def comment_pr(self, pr_id: int, text: str) -> None:
        self.replies.append((pr_id, 0, text))

    def delete_branch(self, branch: str) -> None:
        self.deleted_branches.append(branch)
```

Apply these renames across `tests/` (every occurrence):

| Find | Replace |
|---|---|
| `FakeAdo` | `FakeForge` |
| `ado=` (keyword to `StageExecutor(`, `Scheduler(`) | `forge=` |
| `.get_work_item(` | `.get_item(` |
| `.comment_work_item(` | `.comment_item(` |
| `.set_tag(` / `.has_tag(` | `.set_label(` / `.has_label(` |
| `def set_tag(` / `def has_tag(` in `RaisingAdo` (test_scheduler) and the strings `"set_tag"` / `"has_tag"` in its `fail` set and callers | `set_label` / `has_label` |
| `AdoClient` | `AdoForge` |
| `AdoError` (import from `agent_sdlc.adapters.ado`) | `ForgeError` (import from `agent_sdlc.adapters.errors`) |
| `e2e`: `self.ado` / `env.ado` | `self.forge` / `env.forge` |
| `def create_pr(self, branch, title, body, work_item_id)` in `tests/test_stages.py:172` | `…, item_id: int)` |

Then fix the three signature-driven call sites:
- `tests/test_ado.py:175`: `client.update_pr(42, "new body", 5)`;
- `tests/test_ado.py:185`: `client.reply_pr(42, PrComment(7, 3, "a", "c"), "thanks")`, and
  `tests/test_ado.py:204`: `client.reply_pr(0, PrComment(1, 1, "a", "c"), "x")` (import `PrComment`
  from `agent_sdlc.types`);
- `tests/test_stages.py:130`: `assert res.data["seen_comments"] == ["thread:1:1", "thread:2:1"]`
  (spec §3.1 changes the key format).

Add the imports the new tests in Step 1 use to `tests/test_ado.py`:
`from agent_sdlc.adapters.ado import API, AdoForge, html_to_text`,
`from agent_sdlc.adapters.errors import ForgeError, redact`.

Verify nothing old is left:

Run: `grep -rnE "AdoClient|AdoPort|AdoError|get_work_item|comment_work_item|set_tag|has_tag|FakeAdo|git_auth_header=" src tests`
Expected: no output.

- [ ] **Step 8: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add -A src tests
git commit -m "refactor: ForgePort and AdoForge replace AdoPort and AdoClient

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Forge wording in comments and changes-requested reviews

**Files:**
- Modify: `src/agent_sdlc/orchestrator/reporting.py` (`pr_title`, `commit_message`, `pr_body`, `park_comment_html`, `plan_comment_html`)
- Modify: `src/agent_sdlc/orchestrator/transitions.py:381-387` (`classify_comment`)
- Modify: `src/agent_sdlc/orchestrator/stages.py` (`_implement`, `_pr_open`, `_awaiting`)
- Modify: `src/agent_sdlc/orchestrator/scheduler.py` (`_park_side_effects`)
- Test: `tests/test_reporting.py`, `tests/test_transitions.py`, `tests/test_stages.py`

**Interfaces:**
- Consumes: `ForgePort.item_ref/pr_ref/label_word` (Task 2), `PrComment.changes_requested` (Task 2).
- Produces: `pr_title(wi: WorkItem, ref: str) -> str`; `commit_message(wi: WorkItem, round_: int, ref: str) -> str`; `pr_body(item, wi, ref: str, decisions, checks, review_notes) -> str`; `park_comment_html(item, *, label_word: str = "tag", parked_label: str = "agent:parked") -> str`; `plan_comment_html(plan: str, pr_ref: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reporting.py`:

```python
def test_github_wording() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.RED,
                parked_from=Stage.VERIFY, data={"park_note": "red"})
    h = park_comment_html(item, label_word="label", parked_label="agent:parked")
    assert "<code>agent:parked</code> label" in h and " tag" not in h
    assert "for PR #42" in plan_comment_html("p", "#42")
    assert pr_title(WI, "#5") == "fix: Approve <button> broken (#5)"
    assert commit_message(WI, 0, "#5").endswith("\n\n#5")
    assert pr_body(ITEM, WI, "#5", DEC, CHECKS, "").startswith("Automated change for #5 ")


def test_custom_parked_label_is_escaped() -> None:
    item = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.RED,
                parked_from=Stage.VERIFY, data={"park_note": "n"})
    assert "<code>needs&lt;me&gt;</code> tag" in park_comment_html(item, parked_label="needs<me>")
```

Append to `tests/test_transitions.py`:

```python
def test_changes_requested_review_is_always_a_change_request() -> None:
    c = PrComment(0, 9, "brian", "(changes requested with no summary)", kind="review",
                  changes_requested=True)
    assert classify_comment(c, None) == "change_request"
    noise = {"comment_intent": decision("comment", "comment_intent", "noise")}
    assert classify_comment(c, noise) == "change_request"


def test_slash_agent_in_a_review_body_is_a_change_request() -> None:
    c = PrComment(0, 9, "brian", "/agent rename the helper", kind="review")
    assert classify_comment(c, None) == "change_request"
```

(`decision` comes from `tests.fakes`; add `from tests.fakes import decision` if the file lacks it,
and import `PrComment` and `classify_comment` if missing.)

Append to `tests/test_stages.py`:

```python
async def test_changes_requested_review_labels_with_its_own_source(  # type: ignore[no-untyped-def]
    parts
) -> None:
    ex, forge, *_ = parts
    pr = forge.create_pr("agent/5-add-feature", "t", "b", 5)
    forge.pr_threads[pr].append(PrComment(0, 1, "brian", "(changes requested with no summary)",
                                          kind="review", changes_requested=True))
    res = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))
    assert res.transition.to is Stage.IMPLEMENT
    assert [(lab.gold, lab.source) for lab in res.labels] == [
        ("change_request", "changes_requested")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_reporting.py tests/test_transitions.py tests/test_stages.py -q`
Expected: FAIL (`pr_title()` takes 1 positional argument; `classify_comment` returns the Laya
answer; no label for the review).

- [ ] **Step 3: Implement the wording**

In `src/agent_sdlc/orchestrator/reporting.py`:

```python
def pr_title(wi: WorkItem, ref: str) -> str:
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title} ({ref})"[:400]


def commit_message(wi: WorkItem, round_: int, ref: str) -> str:
    suffix = f" (revision {round_})" if round_ else ""
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title}{suffix}\n\n{ref}"
```

`pr_body` gains `ref: str` as its third parameter
(`def pr_body(item: Item, wi: WorkItem, ref: str, decisions: list[Decision], checks: list[dict[str, Any]], review_notes: str) -> str:`)
and its first part becomes
`f"Automated change for {ref} by agent-sdlc. **Human review required before merge.**"`.

`park_comment_html` becomes:

```python
def park_comment_html(item: Item, *, label_word: str = "tag",
                      parked_label: str = "agent:parked") -> str:
    reason = item.park_reason.value if item.park_reason else "unknown"
    stage = item.parked_from.value if item.parked_from else "unknown"
    note = html.escape(str(item.data.get("park_note", "")))
    lab = f"<code>{html.escape(parked_label)}</code> {label_word}"

    is_gate_park = item.park_reason in GATE_PARKS
    is_gate_stage = item.parked_from in {Stage.TRIAGE, Stage.PLAN, Stage.REVIEW}

    if item.park_reason is ParkReason.MANIFEST:
        resume = html.escape(str(item.data.get("manifest_resume", Stage.VERIFY.value)))
        guidance = (f"Removing the {lab} approves these dependency "
                    f"changes; install and {resume} will run with them.")
    elif is_gate_park and is_gate_stage and item.parked_from is not None:
        next_stages = {Stage.TRIAGE: "plan", Stage.PLAN: "implement", Stage.REVIEW: "pr_open"}
        next_stage = next_stages.get(item.parked_from, "unknown")
        guidance = (f"To continue, update the item if needed and remove the {lab} to approve "
                    f"proceeding to the <code>{next_stage}</code> stage.")
    else:
        retry = "implement" if item.park_reason is ParkReason.PR_ROUNDS else stage
        guidance = (f"To continue, update the item if needed and remove the {lab} to retry the "
                    f"<code>{retry}</code> stage with fresh retry counters.")

    return (f"<p><b>agent-sdlc parked this item</b> at stage <code>{stage}</code> "
            f"(reason: <code>{reason}</code>).</p><pre>{note}</pre>"
            f"{_park_detail(item)}{_denials_html(item)}<p>{guidance}</p>")
```

and:

```python
def plan_comment_html(plan: str, pr_ref: str) -> str:
    return (f"<p><b>agent-sdlc plan</b> for PR {html.escape(pr_ref)}:</p>"
            f"<pre>{html.escape(plan)}</pre>")
```

- [ ] **Step 4: Classify changes-requested reviews and label them**

`src/agent_sdlc/orchestrator/transitions.py`:

```python
def classify_comment(c: PrComment, ds: dict[str, Decision] | None) -> str:
    if c.changes_requested or c.content.strip().lower().startswith("/agent"):
        return "change_request"
    if ds is None:
        return "uncertain"
    d = ds["comment_intent"]
    return d.answer if d.actionable else "uncertain"
```

In `src/agent_sdlc/orchestrator/stages.py` `_awaiting`, replace the `/agent` label block with:

```python
            slash = c.content.strip().lower().startswith("/agent")
            if slash or c.changes_requested:
                d = ds["comment_intent"]
                labels.append(LabelInput("comment", "comment_intent", d.raw_probs,
                                         "change_request",
                                         "slash_command" if slash else "changes_requested"))
```

In `_implement`: `self._ws.commit(wt, commit_message(wi, item.pr_rounds, self._forge.item_ref(wi.id)))`.
In `_pr_open`:

```python
        ref = self._forge.item_ref(wi.id)
        body = pr_body(item, wi, ref, self._decisions_for(item.id), item.data.get("checks", []),
                       str(item.data.get("review_notes", "")))
        if item.pr_id:
            self._forge.update_pr(item.pr_id, body, wi.id)
            pr_id = item.pr_id
        else:
            pr_id = self._forge.create_pr(item.branch, pr_title(wi, ref), body, wi.id)
            if pr_id:  # dry-run returns 0: there is no PR to point at (M6)
                self._forge.comment_item(
                    wi.id, plan_comment_html(str(item.data.get("plan", "")),
                                             self._forge.pr_ref(pr_id)))
```

In `src/agent_sdlc/orchestrator/scheduler.py` `_park_side_effects`:

```python
            self._forge.comment_item(item.id, park_comment_html(
                item, label_word=self._forge.label_word,
                parked_label=self._t.intake.parked_label))
```

- [ ] **Step 5: Update existing reporting tests for the new signatures (mechanical)**

In `tests/test_reporting.py`: `pr_title(WI)` → `pr_title(WI, "AB#5")`;
`pr_title(WorkItem(6, …))` → `pr_title(WorkItem(6, …), "AB#6")`; every `pr_body(X, WI, DEC, …)` →
`pr_body(X, WI, "AB#5", DEC, …)`; `plan_comment_html("<b>x</b>", 42)` →
`plan_comment_html("<b>x</b>", "!42")`; `commit_message(WI, 0)` → `commit_message(WI, 0, "AB#5")`
(and any other `commit_message(WI, n)` → add `"AB#5"`). ADO assertions (`"AB#5"`, `"PR !42"`,
`"agent:parked</code> tag"`) stay as they are.

- [ ] **Step 6: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add -A src tests
git commit -m "feat: forge-specific wording; changes-requested reviews are change requests

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Items keyed by (target, external id); WAL; refs in logs and traces

**Files:**
- Modify: `src/agent_sdlc/types.py` (`Item`)
- Modify: `src/agent_sdlc/store.py` (`ItemRow`, `LabelRow`, `LabelInput`, `Store.__init__`, `add_item`, new `get_by_ref`, `find_external`)
- Modify: `src/agent_sdlc/orchestrator/scheduler.py`, `orchestrator/stages.py` (use `external_id`)
- Modify: `src/agent_sdlc/logctx.py`, `tracing.py`, `labeling.py`, `cli.py`
- Test: `tests/test_store.py`, `tests/test_logctx.py`, plus mechanical updates listed in Step 8

**Interfaces:**
- Produces: `Item.external_id: int = 0` (keyword; always set by the store; 0 only in hand-built test items).
- Produces: `Store.add_item(target: str, wi: WorkItem, branch: str) -> Item | None` (None when that target already tracks the id); `Store.get_by_ref(target: str, external_id: int) -> Item` (KeyError if absent); `Store.find_external(external_id: int, targets: Iterable[str] | None = None) -> list[Item]`.
- Produces: `LabelInput(gate, question, raw_probs, gold, source, decision_id=None, target=None)`.
- Produces: `log_context(item: str, stage: str)` where `item` is `"<target>#<external_id>"`.
- Produces: `label_triage(forge, decider, store, limit, ask, target: str | None = None)`.

- [ ] **Step 1: Write the failing store tests**

Append to `tests/test_store.py`:

```python
def test_same_external_id_under_two_targets(store: Store) -> None:
    a = store.add_item("rallysource", WI, "agent/1-a")
    b = store.add_item("triathlon", WI, "agent/1-b")
    assert a is not None and b is not None and a.id != b.id
    assert (a.external_id, b.external_id) == (1, 1)
    assert store.add_item("triathlon", WI, "agent/1-b") is None
    assert store.get_by_ref("triathlon", 1).branch == "agent/1-b"
    assert {i.target for i in store.find_external(1)} == {"rallysource", "triathlon"}
    assert [i.target for i in store.find_external(1, ["triathlon"])] == ["triathlon"]
    with pytest.raises(KeyError):
        store.get_by_ref("nope", 1)


def test_labels_record_target(store: Store) -> None:
    store.add_label(LabelInput("triage", "kind", {"bug": 1.0}, "bug", "manual", target="tri"))
    assert store.labels("triage", "kind") == [({"bug": 1.0}, "bug")]
    assert store.label_targets("triage", "kind") == ["tri"]


def test_file_db_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    s = Store(f"sqlite:///{tmp_path / 'x.db'}")
    with s._engine.connect() as c:  # noqa: SLF001 - pragma check
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
```

(Add `from pathlib import Path` and `import pytest` to the file if missing, and import `LabelInput`
from `agent_sdlc.store`.)

Replace the assertions in `tests/test_logctx.py`:
`with log_context(7, "plan"):` → `with log_context("t#7", "plan"):`,
`with log_context(4821, "implement"):` → `with log_context("rallysource#4821", "implement"):`, and
`"[#4821 implement] agent_sdlc.test: hello"` → `"[rallysource#4821 implement] agent_sdlc.test: hello"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py tests/test_logctx.py -q`
Expected: FAIL (IntegrityError on the second `add_item` — id 1 is the primary key — and missing
methods).

- [ ] **Step 3: Store changes**

`src/agent_sdlc/types.py` — add a field at the end of `Item` (after `usage`):

```python
    external_id: int = 0   # the forge's item number; set by the Store (0 only in test fixtures)
```

`src/agent_sdlc/store.py`:
- imports: `from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, create_engine, event, select`;
- `ItemRow`:

```python
class ItemRow(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("target", "external_id", name="uq_items_target_external"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[int]
    # …remaining columns unchanged…
```

- `LabelRow` gains `target: Mapped[str | None]`; `LabelInput` gains a last field
  `target: str | None = None`; `_label_row` passes `target=lab.target`;
- `_to_item` passes `external_id=r.external_id`;
- a module-level pragma hook and the constructor:

```python
def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")   # several target threads write (spec §4.1)
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


class Store:
    def __init__(self, url: str) -> None:
        self._engine = create_engine(url)
        if self._engine.dialect.name == "sqlite":
            event.listen(self._engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(self._engine)
```

- item methods:

```python
    def add_item(self, target: str, wi: WorkItem, branch: str) -> Item | None:
        """The new item, or None when the target already tracks this external id."""
        with self._session() as s, s.begin():
            q = select(ItemRow.id).where(ItemRow.target == target,
                                         ItemRow.external_id == wi.id)
            if s.scalars(q).first() is not None:
                return None
            row = ItemRow(target=target, external_id=wi.id, title=wi.title, branch=branch,
                          stage=Stage.TRIAGE.value, data={}, usage={})
            s.add(row)
            s.flush()
            return _to_item(row)

    def get_by_ref(self, target: str, external_id: int) -> Item:
        with self._session() as s:
            q = select(ItemRow).where(ItemRow.target == target,
                                      ItemRow.external_id == external_id)
            row = s.scalars(q).first()
            if row is None:
                raise KeyError(f"{target}#{external_id}")
            return _to_item(row)

    def find_external(self, external_id: int,
                      targets: Iterable[str] | None = None) -> list[Item]:
        with self._session() as s:
            q = select(ItemRow).where(ItemRow.external_id == external_id)
            if targets is not None:
                q = q.where(ItemRow.target.in_(list(targets)))
            return [_to_item(r) for r in s.scalars(q.order_by(ItemRow.target))]

    def label_targets(self, gate: str, question: str) -> list[str | None]:
        with self._session() as s:
            q = (select(LabelRow.target).where(LabelRow.gate == gate,
                                               LabelRow.question == question)
                 .order_by(LabelRow.id))
            return list(s.scalars(q))
```

- [ ] **Step 4: Log context**

`src/agent_sdlc/logctx.py`: `FORMAT = "%(asctime)s %(levelname)s [%(item)s %(stage)s] %(name)s: %(message)s"`
and:

```python
@contextmanager
def log_context(item: str, stage: str) -> Iterator[None]:
    """Tag every log record emitted inside the block with `<target>#<id>` and the stage."""
    t_item, t_stage = _item.set(item), _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(t_stage)
        _item.reset(t_item)
```

- [ ] **Step 5: Orchestrator uses `external_id` for everything outside the store**

`src/agent_sdlc/orchestrator/scheduler.py`:

```python
def _ref(item: Item) -> str:
    return f"{item.target}#{item.external_id}"
```

- `_intake`:

```python
        for wi in intake:
            if self._t.intake.parked_label in wi.tags:
                continue
            branch = f"{self._t.repo.branch_prefix}{wi.id}-{slugify(wi.title)}"
            item = self._store.add_item(self._t.name, wi, branch)
            if item is not None:
                self._store.add_event("intake", {"title": wi.title, "branch": branch}, item=item)
                log.info("intake: %s#%s %s", self._t.name, wi.id, wi.title)
```

- `_warn_stale` and `_step`: `log_context(_ref(item), item.stage.value)`;
- `_step` busy flag: `f"{item.external_id}|{item.stage.value}|{now.isoformat()}"`;
- `_requeue_untagged`: `self._forge.has_label(item.external_id, …)`; log `_ref(item)`;
- `requeue_item`: `self._forge.set_label(item.external_id, self._t.intake.parked_label, False)`;
- `_approval_labels`: each `LabelInput(gate, q, latest[q].raw_probs, gold, source, target=self._t.name)`;
- `_park_side_effects`: `set_label(item.external_id, …)` and `comment_item(item.external_id, …)`;
- `_side_effects`: `self._ws.remove(item.external_id, item.branch)`.

`src/agent_sdlc/orchestrator/stages.py`:
- every `self._forge.get_item(item.id)` → `self._forge.get_item(item.external_id)`;
- every `self._ws.create(item.id, item.branch)` → `self._ws.create(item.external_id, item.branch)`;
- `_trace_path`: `return (self._traces / item.target / str(item.external_id) / f"{ts}-{item.stage.value}-a{item.attempt}-{name}.{ext}")`;
- `_awaiting`'s `LabelInput(...)` gets `target=item.target`;
- `self._decisions_for(item.id)` stays (surrogate id).

`src/agent_sdlc/tracing.py` `render_trace`: head becomes
`head = f'{item.target}#{item.external_id} "{item.title}"   stage: {item.stage.value}'`.

`src/agent_sdlc/labeling.py`:
- `label_triage(forge, decider, store, limit, ask, target: str | None = None)` and
  `LabelInput("triage", q, d.raw_probs, gold, "manual", target=target)`;
- in `label_logged`, the abandoned header:
  `it = store.get(item_id)` then `print(f"\n##### {it.target}#{it.external_id} (PR abandoned) #####")`;
- `_label_one` passes `target=None` implicitly (decision labels reach their target through the item).

`src/agent_sdlc/cli.py`:
- `_DEFAULT_DB = f"sqlite:///{_STATE / 'agent-sdlc-v2.db'}"`;
- `trace`: `item = store.get_by_ref(target.name, args.item_id)` inside the existing `try`
  (KeyError → `print(f"no item #{args.item_id}")`, return 1), then `render_trace(store, item.id, args.full)`;
- `requeue`: resolve `item = store.get_by_ref(target.name, args.item_id)` first; the `--local`
  branch uses that `item`; the other branch calls `scheduler.requeue_item(item.id)`;
- `_status` item rows print `#{i.external_id}` instead of `#{i.id}`;
- `label triage`: `label_triage(forge, decider, store, args.limit, input, target=target.name)`.

- [ ] **Step 6: Run the new tests**

Run: `uv run pytest tests/test_store.py tests/test_logctx.py -q`
Expected: PASS.

- [ ] **Step 7: Run the suite to find the mechanical fallout**

Run: `uv run pytest -q`
Expected: failures only in the files listed in Step 8.

- [ ] **Step 8: Update tests for surrogate ids (mechanical)**

Rules (apply in `tests/test_scheduler.py`, `tests/test_store.py`, `tests/test_tracing.py`,
`tests/test_metrics.py`, `tests/test_labeling.py`, `tests/test_cli.py`, `tests/test_stages.py`,
`tests/e2e/test_pipeline.py`):

1. `store.get(N)` where `N` is a work item number (a literal like `5`, `9`, or `WI.id`) →
   `store.get_by_ref("<target>", N)`, where `<target>` is the name the test passed to
   `add_item` (`"fixture"` in scheduler/e2e tests, `"rallysource"` in CLI tests, `"t"` in
   store/metrics/labeling tests, whatever `tests/test_tracing.py` uses). The e2e `Env.item`
   property becomes `return self.store.get_by_ref("fixture", 5)`.
2. `store.get(x.id)` where `x` is an `Item` stays.
3. `[i.id for i in ex.seen]` → `[i.external_id for i in ex.seen]`.
4. `assert store.add_item(...) is True` / `is False` → `is not None` / `is None`; `assert
   store.add_item(...)` / `assert not store.add_item(...)` stay.
5. `render_trace(store, N)` → `render_trace(store, store.get_by_ref("<target>", N).id)` except the
   missing-item case (`render_trace(store, 404)` keeps raising `KeyError`).
6. `add_event(..., item=store.get(N))` → `item=store.get_by_ref("<target>", N)`.
7. `tests/test_stages.py` helper: `Item(5, "fixture", WI.title, "agent/5-add-feature", stage)` →
   `Item(5, "fixture", WI.title, "agent/5-add-feature", stage, external_id=5)`.
8. Trace-path assertions `traces / "5"` → `traces / "fixture" / "5"`; trace head assertions
   `'#9 "Fix it"'` → `'<target>#9 "Fix it"'`.
9. Labels built by tests that compare `LabelInput` equality get `target=` where the code now sets it
   (`"fixture"` for scheduler approval labels and awaiting slash/changes-requested labels).

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add -A src tests
git commit -m "feat: key items by (target, external id); WAL; target#id in logs and traces

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: GitHub App authentication

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Create: `src/agent_sdlc/adapters/github_auth.py`
- Test: `tests/test_github_auth.py`

**Interfaces:**
- Consumes: `ForgeError` (Task 2).
- Produces: `API_VERSION = "2026-03-10"`, `HEADERS: dict[str, str]`, `GitHubAppAuth(*, app_id: int, private_key: str, owner: str, repo: str, http: httpx.Client, installation_id: int | None = None, clock: Callable[[], float] = time.time)` with `app_jwt() -> str`, `token() -> str`, `invalidate() -> None`, `bot_login() -> str`.

- [ ] **Step 1: Add the dependency**

Run: `uv add "pyjwt[crypto]>=2.15.0"`
Expected: `pyproject.toml` lists `pyjwt[crypto]>=2.15.0`; `uv.lock` updated.

- [ ] **Step 2: Write the failing tests** — create `tests/test_github_auth.py`:

```python
from datetime import datetime

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github_auth import API_VERSION, GitHubAppAuth

API = "https://api.github.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
T0 = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def _auth(clock: Clock, installation_id: int | None = None) -> GitHubAppAuth:
    return GitHubAppAuth(app_id=42, private_key=PEM, owner="o", repo="r",
                         http=httpx.Client(base_url=API), installation_id=installation_id,
                         clock=clock)


def _token_route(token: str, expires: str = "2027-01-15T08:00:00Z") -> respx.Route:
    return respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": token, "expires_at": expires}))


def test_app_jwt_claims() -> None:
    token = _auth(Clock()).app_jwt()
    claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": int(T0) - 60, "exp": int(T0) + 540, "iss": "42"}


@respx.mock
def test_installation_lookup_token_cache_and_headers() -> None:
    lookup = respx.get(f"{API}/repos/o/r/installation").mock(
        return_value=httpx.Response(200, json={"id": 7}))
    mint = _token_route("ghs_first")
    auth = _auth(Clock())
    assert auth.token() == "ghs_first"
    assert auth.token() == "ghs_first"
    assert lookup.call_count == 1 and mint.call_count == 1
    req = mint.calls[0].request
    assert req.headers["Authorization"].startswith("Bearer ")
    assert req.headers["X-GitHub-Api-Version"] == API_VERSION


@respx.mock
def test_token_refreshes_five_minutes_before_expiry() -> None:
    clock = Clock()
    expires = "2027-01-15T08:00:00+00:00"
    exp_ts = datetime.fromisoformat(expires).timestamp()
    clock.now = exp_ts - 3600
    mint = _token_route("ghs_a", expires)
    auth = _auth(clock, installation_id=7)
    assert auth.token() == "ghs_a"
    clock.now = exp_ts - 301
    auth.token()
    assert mint.call_count == 1
    clock.now = exp_ts - 299
    auth.token()
    assert mint.call_count == 2


@respx.mock
def test_invalidate_forces_a_new_token() -> None:
    mint = _token_route("ghs_a")
    auth = _auth(Clock(), installation_id=7)
    auth.token()
    auth.invalidate()
    auth.token()
    assert mint.call_count == 2


@respx.mock
def test_mint_failure_raises_without_secrets() -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(ForgeError) as e:
        _auth(Clock(), installation_id=7).token()
    assert "401" in str(e.value) and "BEGIN" not in str(e.value) and "Bearer" not in str(e.value)


def test_bad_key_raises_without_key_material() -> None:
    auth = GitHubAppAuth(app_id=42, private_key="-----BEGIN nonsense-----", owner="o", repo="r",
                         http=httpx.Client(base_url=API), installation_id=7)
    with pytest.raises(ForgeError) as e:
        auth.app_jwt()
    assert "nonsense" not in str(e.value)


@respx.mock
def test_bot_login_from_app_slug() -> None:
    route = respx.get(f"{API}/app").mock(
        return_value=httpx.Response(200, json={"slug": "agent-sdlc-bot"}))
    auth = _auth(Clock(), installation_id=7)
    assert auth.bot_login() == "agent-sdlc-bot[bot]"
    assert auth.bot_login() == "agent-sdlc-bot[bot]"
    assert route.call_count == 1
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_github_auth.py -q`
Expected: FAIL (ModuleNotFoundError: `agent_sdlc.adapters.github_auth`).

- [ ] **Step 4: Implement `src/agent_sdlc/adapters/github_auth.py`**

```python
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import jwt

from agent_sdlc.adapters.errors import ForgeError

API_VERSION = "2026-03-10"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
_REFRESH_BEFORE_S = 300   # refresh when fewer than 5 minutes remain (spec §3.3)


class GitHubAppAuth:
    """Installation tokens for one GitHub App installation, minted from the App's private key.
    Thread-safe; the key and tokens never appear in exceptions."""

    def __init__(self, *, app_id: int, private_key: str, owner: str, repo: str,
                 http: httpx.Client, installation_id: int | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._app_id = app_id
        self._key = private_key
        self._owner, self._repo = owner, repo
        self._http = http
        self._installation_id = installation_id
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._bot: str | None = None

    def app_jwt(self) -> str:
        now = int(self._clock())
        try:
            return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)},
                              self._key, algorithm="RS256")
        except Exception as e:  # malformed key: never echo it
            raise ForgeError(f"GitHub App private key could not be used ({type(e).__name__})"
                             ) from None

    def _app_request(self, method: str, path: str) -> Any:
        r = self._http.request(method, path,
                               headers={**HEADERS, "Authorization": f"Bearer {self.app_jwt()}"})
        if r.status_code >= 400:
            raise ForgeError(f"GitHub App auth {method} {path} failed: HTTP {r.status_code}")
        return r.json()

    def _installation(self) -> int:
        if self._installation_id is None:
            res = self._app_request("GET", f"/repos/{self._owner}/{self._repo}/installation")
            self._installation_id = int(res["id"])
        return self._installation_id

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._expires - self._clock() < _REFRESH_BEFORE_S:
                res = self._app_request(
                    "POST", f"/app/installations/{self._installation()}/access_tokens")
                self._token = str(res["token"])
                self._expires = datetime.fromisoformat(
                    str(res["expires_at"]).replace("Z", "+00:00")).timestamp()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def bot_login(self) -> str:
        if self._bot is None:
            self._bot = f"{self._app_request('GET', '/app')['slug']}[bot]"
        return self._bot
```

- [ ] **Step 5: Run the tests, the suite, lint and types**

Run: `uv run pytest tests/test_github_auth.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/agent_sdlc/adapters/github_auth.py tests/test_github_auth.py
git commit -m "feat: GitHub App installation-token auth

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `GitHubForge` — issues, labels, comments, git auth

**Files:**
- Create: `src/agent_sdlc/adapters/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Consumes: `GitHubAppAuth`, `HEADERS` (Task 5); `ForgeError`, `redact` (Task 2); `GitHubForgeConfig`, `IntakeConfig` (Task 1).
- Produces: `split_acceptance(body: str) -> tuple[str, str]`; `class AppAuth(Protocol)` with `token() -> str`, `invalidate() -> None`, `bot_login() -> str`; `GitHubForge(cfg: GitHubForgeConfig, auth: AppAuth, *, intake: IntakeConfig, base_branch: str, branch_prefix: str, http: httpx.Client, push_url: str | None = None, dry_run_push: bool = False)` implementing the item half of `ForgePort` (PR half in Task 7).

- [ ] **Step 1: Write the failing tests** — create `tests/test_github.py`:

```python
import base64
from urllib.parse import unquote

import httpx
import pytest
import respx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github import GitHubForge, split_acceptance
from agent_sdlc.targets import GitHubForgeConfig, IntakeConfig

API = "https://api.github.com"
REPO = f"{API}/repos/paradigmbrian/triathlon-agent"
CFG = GitHubForgeConfig(kind="github", owner="paradigmbrian", repo="triathlon-agent", app_id=42)


class FakeAuth:
    def __init__(self) -> None:
        self.tokens = ["ghs_one", "ghs_two"]
        self.invalidated = 0

    def token(self) -> str:
        return self.tokens[0]

    def invalidate(self) -> None:
        self.invalidated += 1
        self.tokens.pop(0)

    def bot_login(self) -> str:
        return "agent-sdlc-bot[bot]"


def forge(dry_run: bool = False, auth: FakeAuth | None = None,
          push_url: str | None = None) -> GitHubForge:
    return GitHubForge(CFG, auth or FakeAuth(), intake=IntakeConfig(), base_branch="main",
                       branch_prefix="agent/", http=httpx.Client(base_url=API),
                       push_url=push_url, dry_run_push=dry_run)


def issue(n: int, body: str | None = "Do it", labels: tuple[str, ...] = ("agent",),
          pr: bool = False) -> dict[str, object]:
    d: dict[str, object] = {"number": n, "title": f"Issue {n}", "body": body,
                            "labels": [{"name": x} for x in labels],
                            "html_url": f"https://github.com/x/{n}"}
    if pr:
        d["pull_request"] = {"url": "u"}
    return d


def test_split_acceptance() -> None:
    body = "Intro\n\n## Acceptance criteria\n- a\n- b\n\n## Notes\nlater"
    assert split_acceptance(body) == ("Intro\n\n## Notes\nlater", "- a\n- b")
    assert split_acceptance("### acceptance criteria:\nx\n#### sub\ny") == ("", "x\n#### sub\ny")
    assert split_acceptance("no section") == ("no section", "")
    assert split_acceptance("") == ("", "")


def test_port_attributes_and_git_header() -> None:
    f = forge()
    assert f.kind == "github" and f.label_word == "label"
    assert f.pr_ref(3) == "#3" and f.item_ref(5) == "#5"
    scheme, value = f.git_auth_header().split(": ", 1)[1].split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == "x-access-token:ghs_one"


@respx.mock
def test_list_intake_drops_prs_and_maps_items() -> None:
    route = respx.get(f"{REPO}/issues").mock(return_value=httpx.Response(200, json=[
        issue(5, "Body\n## Acceptance criteria\nworks", ("agent", "bug")),
        issue(6, pr=True),
        issue(7, None),
    ]))
    items = forge().list_intake()
    assert [i.id for i in items] == [5, 7]
    first, second = items
    assert (first.description, first.acceptance_criteria) == ("Body", "works")
    assert first.work_item_type == "Bug" and first.tags == ("agent", "bug")
    assert (second.description, second.acceptance_criteria, second.work_item_type) == \
        ("", "", "Issue")
    params = route.calls[0].request.url.params
    assert params["labels"] == "agent" and params["state"] == "open"
    assert route.calls[0].request.headers["Authorization"] == "Bearer ghs_one"


@respx.mock
def test_list_intake_reads_every_page() -> None:
    page1 = [issue(n) for n in range(1, 101)]
    page2 = [issue(101)]
    respx.get(f"{REPO}/issues", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=page1))
    respx.get(f"{REPO}/issues", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=page2))
    assert len(forge().list_intake()) == 101


@respx.mock
def test_get_item_refuses_a_pull_request() -> None:
    respx.get(f"{REPO}/issues/6").mock(return_value=httpx.Response(200, json=issue(6, pr=True)))
    with pytest.raises(ForgeError, match="pull request"):
        forge().get_item(6)


@respx.mock
def test_labels_add_remove_encoded_and_has() -> None:
    add = respx.post(f"{REPO}/issues/5/labels").mock(return_value=httpx.Response(200, json=[]))
    rm = respx.delete(url__regex=rf"{REPO}/issues/5/labels/.+").mock(
        return_value=httpx.Response(404))
    respx.get(f"{REPO}/issues/5").mock(
        return_value=httpx.Response(200, json=issue(5, labels=("agent", "agent:parked"))))
    f = forge()
    f.set_label(5, "agent:parked", True)
    f.set_label(5, "agent:parked", False)   # 404: already gone is success
    assert add.calls[0].request.content == b'{"labels":["agent:parked"]}'
    raw_path = rm.calls[0].request.url.raw_path.decode()
    assert raw_path.endswith("/labels/agent%3Aparked")
    assert unquote(raw_path).endswith("/labels/agent:parked")
    assert f.has_label(5, "agent:parked") and not f.has_label(5, "nope")


@respx.mock
def test_comment_item_posts_html() -> None:
    route = respx.post(f"{REPO}/issues/5/comments").mock(return_value=httpx.Response(201))
    forge(dry_run=True).comment_item(5, "<p>hi</p>")   # issue comments stay real in dry run
    assert route.calls[0].request.content == b'{"body":"<p>hi</p>"}'


@respx.mock
def test_401_refreshes_once_then_retries() -> None:
    route = respx.get(f"{REPO}/issues/5").mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json=issue(5))])
    auth = FakeAuth()
    assert forge(auth=auth).get_item(5).id == 5
    assert auth.invalidated == 1
    assert route.calls[1].request.headers["Authorization"] == "Bearer ghs_two"


@respx.mock
def test_errors_and_rate_limits_never_leak_tokens() -> None:
    respx.get(f"{REPO}/issues/5").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"}))
    respx.get(f"{REPO}/issues/6").mock(return_value=httpx.Response(429))
    respx.get(f"{REPO}/issues/7").mock(return_value=httpx.Response(404))
    f = forge()
    with pytest.raises(ForgeError, match="rate limit.*1900000000") as e:
        f.get_item(5)
    assert "ghs_" not in str(e.value)
    with pytest.raises(ForgeError, match="rate limit"):
        f.get_item(6)
    with pytest.raises(ForgeError, match="HTTP 404"):
        f.get_item(7)


@respx.mock
def test_list_closed_limits_and_drops_prs() -> None:
    respx.get(f"{REPO}/issues").mock(return_value=httpx.Response(200, json=[
        issue(1), issue(2, pr=True), issue(3), issue(4)]))
    assert [i.id for i in forge().list_closed(2)] == [1, 3]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github.py -q`
Expected: FAIL (ModuleNotFoundError: `agent_sdlc.adapters.github`).

- [ ] **Step 3: Implement the item half of `src/agent_sdlc/adapters/github.py`**

```python
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github_auth import HEADERS
from agent_sdlc.targets import GitHubForgeConfig, IntakeConfig
from agent_sdlc.types import PrComment, WorkItem

log = logging.getLogger(__name__)
_PER_PAGE = 100
_MAX_PAGES = 50
_AC_HEADING = re.compile(r"^(#{1,6})[ \t]*acceptance criteria[ \t]*:?[ \t]*$",
                         re.IGNORECASE | re.MULTILINE)


class AppAuth(Protocol):
    def token(self) -> str: ...
    def invalidate(self) -> None: ...
    def bot_login(self) -> str: ...


def split_acceptance(body: str) -> tuple[str, str]:
    """(description without the section, acceptance criteria) from an issue body."""
    m = _AC_HEADING.search(body)
    if m is None:
        return body.strip(), ""
    level = len(m.group(1))
    rest = body[m.end():]
    nxt = re.search(rf"^#{{1,{level}}}[ \t]", rest, re.MULTILINE)
    criteria = rest[: nxt.start()] if nxt else rest
    after = rest[nxt.start():] if nxt else ""
    return (body[: m.start()] + after).strip(), criteria.strip()


class GitHubForge:
    kind = "github"
    label_word = "label"

    def __init__(self, cfg: GitHubForgeConfig, auth: AppAuth, *, intake: IntakeConfig,
                 base_branch: str, branch_prefix: str, http: httpx.Client,
                 push_url: str | None = None, dry_run_push: bool = False) -> None:
        self._cfg = cfg
        self._auth = auth
        self._intake = intake
        self._base_branch = base_branch
        self._branch_prefix = branch_prefix
        self._http = http
        self._push_url = push_url or cfg.clone_url
        self._dry_run = dry_run_push
        self._repo = f"/repos/{cfg.owner}/{cfg.repo}"

    # plumbing --------------------------------------------------------------
    def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json: Any = None, ok: tuple[int, ...] = ()) -> Any:
        r = self._send(method, path, params, json)
        if r.status_code == 401:   # expired or revoked token: refresh once (spec §3.3)
            self._auth.invalidate()
            r = self._send(method, path, params, json)
        if r.status_code in ok:
            return None
        if r.status_code == 429 or (
                r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"):
            raise ForgeError(f"GitHub rate limit on {method} {path}; resets at "
                             f"{r.headers.get('x-ratelimit-reset', 'unknown')}")
        if r.status_code >= 400:
            raise ForgeError(f"GitHub {method} {path} failed: HTTP {r.status_code}")
        return r.json() if r.content else None

    def _send(self, method: str, path: str, params: dict[str, Any] | None,
              json: Any) -> httpx.Response:
        headers = {**HEADERS, "Authorization": f"Bearer {self._auth.token()}"}
        return self._http.request(method, path, params=params, json=json, headers=headers)

    def _pages(self, path: str, params: dict[str, Any] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._req("GET", path,
                              params={**(params or {}), "per_page": _PER_PAGE, "page": page})
            out += batch
            if len(batch) < _PER_PAGE or (limit is not None and len(out) >= limit):
                break
        return out

    def _check_branch(self, branch: str) -> None:
        if not branch.startswith(self._branch_prefix):
            raise ForgeError(f"refusing ref outside {self._branch_prefix}*: {branch}")

    def git_auth_header(self) -> str:
        cred = base64.b64encode(f"x-access-token:{self._auth.token()}".encode()).decode()
        return f"Authorization: Basic {cred}"

    def pr_ref(self, pr_id: int) -> str:
        return f"#{pr_id}"

    def item_ref(self, item_id: int) -> str:
        return f"#{item_id}"

    # issues ----------------------------------------------------------------
    @staticmethod
    def _to_item(i: dict[str, Any]) -> WorkItem:
        description, criteria = split_acceptance(str(i.get("body") or ""))
        labels = tuple(str(x["name"] if isinstance(x, dict) else x) for x in i.get("labels", []))
        return WorkItem(
            id=int(i["number"]), title=str(i.get("title", "")), description=description,
            acceptance_criteria=criteria,
            work_item_type="Bug" if "bug" in {x.lower() for x in labels} else "Issue",
            tags=labels, url=str(i.get("html_url", "")))

    def list_intake(self) -> list[WorkItem]:
        raw = self._pages(f"{self._repo}/issues", {"state": "open", "labels": self._intake.label,
                                                  "sort": "created", "direction": "asc"})
        return [self._to_item(i) for i in raw if "pull_request" not in i]

    def list_closed(self, limit: int) -> list[WorkItem]:
        raw = self._pages(f"{self._repo}/issues", {"state": "closed",
                                                  "labels": self._intake.label,
                                                  "sort": "updated", "direction": "desc"},
                          limit=limit)
        return [self._to_item(i) for i in raw if "pull_request" not in i][:limit]

    def get_item(self, id: int) -> WorkItem:
        i = self._req("GET", f"{self._repo}/issues/{id}")
        if "pull_request" in i:
            raise ForgeError(f"#{id} is a pull request, not an issue")
        return self._to_item(i)

    def comment_item(self, id: int, html: str) -> None:
        self._req("POST", f"{self._repo}/issues/{id}/comments", json={"body": html})

    def has_label(self, id: int, label: str) -> bool:
        return label in self.get_item(id).tags

    def set_label(self, id: int, label: str, present: bool) -> None:
        if present:
            self._req("POST", f"{self._repo}/issues/{id}/labels", json={"labels": [label]})
        else:
            self._req("DELETE", f"{self._repo}/issues/{id}/labels/{quote(label, safe='')}",
                      ok=(404,))
```

`Path` and `PrComment` are imported for Task 7; if ruff reports them unused at this step, add
them in Task 7 instead.

- [ ] **Step 4: Run the tests, lint and types**

Run: `uv run pytest tests/test_github.py -q && uv run ruff check . && uv run mypy`
Expected: all pass. If the pagination test fails because respx matches both routes on `page`,
confirm the request carries `page=1` then `page=2` (`route.calls[0].request.url.params`), which is
what `_pages` sends.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/adapters/github.py tests/test_github.py
git commit -m "feat: GitHubForge issues, labels and comments

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `GitHubForge` — push, pull requests and PR feedback

**Files:**
- Modify: `src/agent_sdlc/adapters/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Consumes: Task 6 `GitHubForge`, `git_env` from `agent_sdlc.workspaces`, `redact`.
- Produces: the PR half of `ForgePort` on `GitHubForge`: `push_branch`, `create_pr`, `update_pr`, `pr_status`, `pr_comments`, `reply_pr`, `comment_pr`, `delete_branch`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_github.py`:

```python
from pathlib import Path

from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import PrComment
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git


def user(login: str) -> dict[str, str]:
    return {"login": login}


@respx.mock
def test_create_and_update_pr_carry_closes_line() -> None:
    create = respx.post(f"{REPO}/pulls").mock(
        return_value=httpx.Response(201, json={"number": 12}))
    update = respx.patch(f"{REPO}/pulls/12").mock(return_value=httpx.Response(200, json={}))
    f = forge()
    assert f.create_pr("agent/5-x", "fix: x (#5)", "body", 5) == 12
    sent = httpx.Response(200, content=create.calls[0].request.content).json()
    assert sent == {"title": "fix: x (#5)", "head": "agent/5-x", "base": "main",
                    "body": "body\n\nCloses #5"}
    f.update_pr(12, "new", 5)
    assert httpx.Response(200, content=update.calls[0].request.content).json() == {
        "body": "new\n\nCloses #5"}
    with pytest.raises(ForgeError):
        f.create_pr("main", "t", "b", 5)


@respx.mock
def test_pr_status_mapping() -> None:
    respx.get(f"{REPO}/pulls/1").mock(return_value=httpx.Response(
        200, json={"state": "closed", "merged": True}))
    respx.get(f"{REPO}/pulls/2").mock(return_value=httpx.Response(
        200, json={"state": "closed", "merged": False}))
    respx.get(f"{REPO}/pulls/3").mock(return_value=httpx.Response(
        200, json={"state": "open", "merged": False}))
    f = forge()
    assert [f.pr_status(n) for n in (1, 2, 3)] == ["completed", "abandoned", "active"]


@respx.mock
def test_pr_comments_merge_three_sources_and_skip_the_bot() -> None:
    respx.get(f"{REPO}/issues/12/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 1, "user": user("brian"), "body": "please add docs"},
        {"id": 2, "user": user("agent-sdlc-bot[bot]"), "body": "my own reply"},
        {"id": 3, "user": user("brian"), "body": "   "},
    ]))
    respx.get(f"{REPO}/pulls/12/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 10, "user": user("brian"), "body": "rename this", "in_reply_to_id": None},
        {"id": 11, "user": user("brian"), "body": "and this", "in_reply_to_id": 10},
    ]))
    respx.get(f"{REPO}/pulls/12/reviews").mock(return_value=httpx.Response(200, json=[
        {"id": 20, "user": user("brian"), "body": "", "state": "CHANGES_REQUESTED"},
        {"id": 21, "user": user("brian"), "body": "", "state": "APPROVED"},
        {"id": 22, "user": user("brian"), "body": "/agent tidy up", "state": "COMMENTED"},
        {"id": 23, "user": user("brian"), "body": "draft", "state": "PENDING"},
    ]))
    got = forge().pr_comments(12)
    assert [(c.kind, c.thread_id, c.comment_id, c.content, c.changes_requested) for c in got] == [
        ("conversation", 0, 1, "please add docs", False),
        ("review_comment", 10, 10, "rename this", False),
        ("review_comment", 10, 11, "and this", False),
        ("review", 0, 20, "(changes requested with no summary)", True),
        ("review", 0, 22, "/agent tidy up", False),
    ]
    assert len({c.key for c in got}) == 5


@respx.mock
def test_pr_comments_read_every_page() -> None:
    first = [{"id": n, "user": user("brian"), "body": f"c{n}"} for n in range(1, 101)]
    respx.get(f"{REPO}/issues/12/comments", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=first))
    respx.get(f"{REPO}/issues/12/comments", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[{"id": 101, "user": user("b"), "body": "last"}]))
    respx.get(f"{REPO}/pulls/12/comments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REPO}/pulls/12/reviews").mock(return_value=httpx.Response(200, json=[]))
    assert len(forge().pr_comments(12)) == 101


@respx.mock
def test_reply_routing() -> None:
    thread = respx.post(f"{REPO}/pulls/12/comments/10/replies").mock(
        return_value=httpx.Response(201))
    convo = respx.post(f"{REPO}/issues/12/comments").mock(return_value=httpx.Response(201))
    f = forge()
    f.reply_pr(12, PrComment(10, 11, "brian", "why?", kind="review_comment"), "Because.")
    f.reply_pr(12, PrComment(0, 1, "brian", "line one\nline two", kind="conversation"), "Sure.")
    assert httpx.Response(200, content=thread.calls[0].request.content).json() == {
        "body": "Because."}
    body = httpx.Response(200, content=convo.calls[0].request.content).json()["body"]
    assert body == "> line one\n> line two\n\n@brian Sure."


@respx.mock
def test_delete_branch_tolerates_missing_and_refuses_other_refs() -> None:
    route = respx.delete(f"{REPO}/git/refs/heads/agent/5-x").mock(
        return_value=httpx.Response(422))
    f = forge()
    f.delete_branch("agent/5-x")
    assert route.call_count == 1
    with pytest.raises(ForgeError):
        f.delete_branch("main")


@respx.mock
def test_dry_run_makes_pr_side_effects_no_ops() -> None:
    f = forge(dry_run=True)
    assert f.create_pr("agent/5-x", "t", "b", 5) == 0
    f.update_pr(0, "b", 5)
    f.comment_pr(0, "x")
    f.reply_pr(0, PrComment(0, 1, "a", "c", kind="conversation"), "x")
    f.delete_branch("agent/5-x")
    assert f.pr_status(0) == "active" and f.pr_comments(0) == []
    assert respx.calls.call_count == 0


def test_push_branch_to_local_origin(tmp_path: Path, target: TargetConfig,
                                     origin_repo: Path) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "agent/5-x")
    (wt / "f.txt").write_text("x")
    ws.commit(wt, "feat: f")
    f = forge(push_url=str(origin_repo))
    f.push_branch(wt, "agent/5-x")
    assert "agent/5-x" in git("branch", "--list", "agent/*", cwd=origin_repo)
    with pytest.raises(ForgeError):
        f.push_branch(wt, "main")


def test_push_failure_is_redacted(tmp_path: Path, target: TargetConfig) -> None:
    ws = Workspaces(tmp_path / "w", target)
    wt = ws.create(5, "agent/5-x")
    f = forge(push_url=str(tmp_path / "missing-ghs_one.git"))
    with pytest.raises(ForgeError) as e:
        f.push_branch(wt, "agent/5-x")
    assert "ghs_one" not in str(e.value)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github.py -q`
Expected: FAIL (`GitHubForge` has no `create_pr`, …).

- [ ] **Step 3: Implement the PR half** — append to the `GitHubForge` class (add
`import subprocess` and `from agent_sdlc.workspaces import git_env` and
`from agent_sdlc.adapters.errors import ForgeError, redact` to the imports):

```python
    # git & pull requests ---------------------------------------------------
    def push_branch(self, worktree: Path, branch: str) -> None:
        self._check_branch(branch)
        if self._dry_run:
            log.info("dry-run: would push %s", branch)
            return
        token = self._auth.token()
        r = subprocess.run(
            ["git", "-c", f"http.extraheader={self.git_auth_header()}", "push", self._push_url,
             f"HEAD:refs/heads/{branch}"],
            cwd=worktree, capture_output=True, text=True, env=git_env())
        if r.returncode != 0:
            raise ForgeError(f"push failed: {redact(r.stderr.strip(), [token])}")

    @staticmethod
    def _closes(body: str, item_id: int) -> str:
        return f"{body}\n\nCloses #{item_id}"

    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        self._check_branch(branch)
        if self._dry_run:
            log.info("dry-run: would open PR %s\n%s", title, body)
            return 0
        res = self._req("POST", f"{self._repo}/pulls", json={
            "title": title, "head": branch, "base": self._base_branch,
            "body": self._closes(body, item_id)})
        return int(res["number"])

    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        if self._dry_run:
            log.info("dry-run: would update PR %s description", pr_id)
            return
        self._req("PATCH", f"{self._repo}/pulls/{pr_id}",
                  json={"body": self._closes(body, item_id)})

    def pr_status(self, pr_id: int) -> str:
        if self._dry_run:
            return "active"
        pr = self._req("GET", f"{self._repo}/pulls/{pr_id}")
        if pr.get("merged"):
            return "completed"
        return "abandoned" if pr.get("state") == "closed" else "active"

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        if self._dry_run:
            return []
        me = self._auth.bot_login()
        out: list[PrComment] = []
        for c in self._pages(f"{self._repo}/issues/{pr_id}/comments"):
            author, body = str(c["user"]["login"]), str(c.get("body") or "")
            if author != me and body.strip():
                out.append(PrComment(0, int(c["id"]), author, body, kind="conversation"))
        for c in self._pages(f"{self._repo}/pulls/{pr_id}/comments"):
            author, body = str(c["user"]["login"]), str(c.get("body") or "")
            if author != me and body.strip():
                out.append(PrComment(int(c.get("in_reply_to_id") or c["id"]), int(c["id"]),
                                     author, body, kind="review_comment"))
        for r in self._pages(f"{self._repo}/pulls/{pr_id}/reviews"):
            author, body, state = str(r["user"]["login"]), str(r.get("body") or ""), r["state"]
            changes = state == "CHANGES_REQUESTED"
            if author == me or state == "PENDING" or not (body.strip() or changes):
                continue
            out.append(PrComment(0, int(r["id"]), author,
                                 body.strip() or "(changes requested with no summary)",
                                 kind="review", changes_requested=changes))
        return out

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        if self._dry_run:
            log.info("dry-run: would reply on PR %s: %s", pr_id, text)
            return
        if comment.kind == "review_comment":
            self._req("POST",
                      f"{self._repo}/pulls/{pr_id}/comments/{comment.thread_id}/replies",
                      json={"body": text})
            return
        quoted = "\n".join(f"> {line}" for line in comment.content[:300].splitlines())
        self.comment_pr(pr_id, f"{quoted}\n\n@{comment.author} {text}")

    def comment_pr(self, pr_id: int, text: str) -> None:
        if self._dry_run:
            log.info("dry-run: would comment on PR %s: %s", pr_id, text)
            return
        self._req("POST", f"{self._repo}/issues/{pr_id}/comments", json={"body": text})

    def delete_branch(self, branch: str) -> None:
        self._check_branch(branch)
        if self._dry_run:
            log.info("dry-run: would delete branch %s", branch)
            return
        self._req("DELETE", f"{self._repo}/git/refs/heads/{branch}", ok=(404, 422))
```

- [ ] **Step 4: Run the tests, the suite, lint and types**

Run: `uv run pytest tests/test_github.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass. `GitHubForge` must now satisfy `ForgePort`; add this check at the bottom of
`tests/test_github.py` so mypy enforces it:

```python
def test_github_forge_satisfies_the_port() -> None:
    from agent_sdlc.ports import ForgePort
    port: ForgePort = forge()
    assert port.kind == "github"
```

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/adapters/github.py tests/test_github.py
git commit -m "feat: GitHubForge push, pull requests and PR feedback

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Per-target control, session slots and a locked decider

**Files:**
- Create: `src/agent_sdlc/orchestrator/slots.py`
- Modify: `src/agent_sdlc/orchestrator/scheduler.py` (`tick`, `_step`, `run_forever`, new `stop_requested`)
- Modify: `src/agent_sdlc/orchestrator/stages.py` (`__init__`, `_run_agent`)
- Modify: `src/agent_sdlc/decisions/decider.py` (`LockedDecider`)
- Modify: `src/agent_sdlc/cli.py` (`_status` reads per-target flags; `pause`/`resume` flags)
- Test: `tests/test_slots.py` (new), `tests/test_scheduler.py`, `tests/test_stages.py`, `tests/test_decider.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `SessionSlots(n: int, poll_s: float = 5.0)` with context manager `hold(should_stop: Callable[[], bool]) -> Iterator[None]` (raises `AgentInterrupted` when stopped while waiting).
- Produces: `stop_requested(store: Store, target: str) -> bool` in `agent_sdlc.orchestrator.scheduler`.
- Produces: `Scheduler.run_forever(poll_s: int = 60, stop: threading.Event | None = None)`; flags `last_tick:<target>`, `busy:<target>`, `paused:<target>`.
- Produces: `StageExecutor(..., slots: SessionSlots | None = None, should_stop: Callable[[], bool] = lambda: False)`.
- Produces: `LockedDecider(inner: DeciderPort)` implementing `DeciderPort`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_slots.py`:

```python
import threading
import time

import pytest

from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.types import AgentInterrupted


def test_second_holder_waits_for_the_first() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    order: list[str] = []
    entered = threading.Event()

    def first() -> None:
        with slots.hold(lambda: False):
            entered.set()
            time.sleep(0.1)
            order.append("first done")

    t = threading.Thread(target=first)
    t.start()
    entered.wait()
    with slots.hold(lambda: False):
        order.append("second in")
    t.join()
    assert order == ["first done", "second in"]


def test_stop_while_waiting_raises_interrupted() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    with slots.hold(lambda: False), pytest.raises(AgentInterrupted):
        with slots.hold(lambda: True):
            pass


def test_slot_released_after_exception() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    with pytest.raises(RuntimeError), slots.hold(lambda: False):
        raise RuntimeError("boom")
    with slots.hold(lambda: True):   # acquires immediately: the slot was released
        pass
```

Append to `tests/test_scheduler.py`:

```python
async def test_per_target_pause_and_flags(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN)))
    store.set_flag("paused:fixture", "1")
    await sched(env, ex).tick()
    assert ex.seen == [] and store.get_flag("last_tick:fixture") == NOW.isoformat()
    store.set_flag("paused:fixture", None)
    store.set_flag("paused:other", "1")          # another target's pause does not apply
    await sched(env, ex).tick()
    assert [i.external_id for i in ex.seen] == [5]
    assert store.get_flag("busy:fixture") is None


def test_stop_requested(env) -> None:  # type: ignore[no-untyped-def]
    from agent_sdlc.orchestrator.scheduler import stop_requested
    store = env[0]
    assert not stop_requested(store, "fixture")
    store.set_flag("paused:fixture", "1")
    assert stop_requested(store, "fixture") and not stop_requested(store, "other")
    store.set_flag("paused:fixture", None)
    store.set_flag("paused", "1")
    assert stop_requested(store, "other")


async def test_run_forever_stops_on_event(env) -> None:  # type: ignore[no-untyped-def]
    import threading
    stop = threading.Event()

    class Once(ScriptedExecutor):
        async def run(self, item: Item) -> StepResult:
            stop.set()
            return StepResult(Transition(Stage.PLAN))

    await sched(env, Once()).run_forever(poll_s=60, stop=stop)
    assert env[0].get_by_ref("fixture", 5).stage is Stage.PLAN
```

In `tests/test_scheduler.py` update the existing busy test (around line 517): `store.get_flag("busy")`
→ `store.get_flag("busy:fixture")` (two places; spec §4.1 makes the flag per target).

Append to `tests/test_stages.py`:

```python
async def test_agent_waits_for_a_session_slot_and_honours_pause(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    from agent_sdlc.orchestrator.slots import SessionSlots
    from agent_sdlc.types import AgentInterrupted
    slots = SessionSlots(1, poll_s=0.01)
    forge = FakeForge(origin=origin_repo)
    forge.add(WI)
    ex = StageExecutor(target=target, forge=forge, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=Workspaces(tmp_path / "ws", target),
                       path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [], slots=slots, should_stop=lambda: True)
    with slots.hold(lambda: False):     # another target holds the only slot
        with pytest.raises(AgentInterrupted):
            await ex.run(item(Stage.PLAN))
    res = await ex.run(item(Stage.PLAN))  # slot free again: runs even though should_stop is set
    assert res.transition.to is Stage.IMPLEMENT
```

Append to `tests/test_decider.py`:

```python
def test_locked_decider_delegates() -> None:
    from agent_sdlc.decisions.decider import LockedDecider
    from tests.fakes import FakeDecider
    inner = FakeDecider()
    out = LockedDecider(inner).decide("plan", {"plan": "p"})
    assert set(out) == {"plan_addresses_item", "plan_scope_ok"}
    assert inner.calls == [("plan", {"plan": "p"})]
```

In `tests/test_cli.py`, the status tests set and read the per-target flags now:
`store.set_flag("last_tick", …)` → `store.set_flag("last_tick:rallysource", …)` and
`store.set_flag("busy", …)` → `store.set_flag("busy:rallysource", …)` (spec §4.1). Add:

```python
def test_pause_one_target(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "pause", "--target-name", "rallysource") == 0
    store = Store(db)
    assert store.get_flag("paused:rallysource") == "1" and store.get_flag("paused") is None
    assert run(db, "resume") == 0                       # global resume leaves it paused
    assert Store(db).get_flag("paused:rallysource") == "1"
    assert run(db, "resume", "--target-name", "rallysource") == 0
    assert Store(db).get_flag("paused:rallysource") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_slots.py tests/test_scheduler.py tests/test_stages.py tests/test_decider.py tests/test_cli.py -q`
Expected: FAIL (missing module/flags/arguments).

- [ ] **Step 3: Implement `src/agent_sdlc/orchestrator/slots.py`**

```python
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from agent_sdlc.types import AgentInterrupted


class SessionSlots:
    """Caps concurrent agent sessions across all targets (spec §5.2)."""

    def __init__(self, n: int, poll_s: float = 5.0) -> None:
        self._sem = threading.BoundedSemaphore(n)
        self._poll_s = poll_s

    @contextmanager
    def hold(self, should_stop: Callable[[], bool]) -> Iterator[None]:
        while not self._sem.acquire(timeout=self._poll_s):
            if should_stop():
                raise AgentInterrupted("paused while waiting for an agent session slot")
        try:
            yield
        finally:
            self._sem.release()
```

- [ ] **Step 4: Scheduler per-target flags and a stoppable loop**

In `src/agent_sdlc/orchestrator/scheduler.py` add `import threading` and:

```python
def stop_requested(store: Store, target: str) -> bool:
    """The global kill switch or this target's pause (spec §5.2)."""
    return store.get_flag("paused") == "1" or store.get_flag(f"paused:{target}") == "1"
```

Replace `run_forever`, `_paused`, the first two lines of `tick`, and the flag lines of `_step`:

```python
    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            if stop is None:
                await asyncio.sleep(poll_s)
            else:
                await asyncio.to_thread(stop.wait, poll_s)

    def _paused(self, now: datetime) -> bool:
        if stop_requested(self._store, self._t.name):
            return True
        until = self._store.get_flag("paused_until")
        return bool(until and datetime.fromisoformat(until) > now)
```

In `tick`: `self._store.set_flag(f"last_tick:{self._t.name}", now.isoformat())`.
In `_step`: the two `set_flag("busy", …)` calls use `f"busy:{self._t.name}"`.

- [ ] **Step 5: Session slots in `StageExecutor`, and `LockedDecider`**

`src/agent_sdlc/orchestrator/stages.py`: import `SessionSlots` from
`agent_sdlc.orchestrator.slots` and `contextlib`; add keyword parameters
`slots: SessionSlots | None = None, should_stop: Callable[[], bool] = lambda: False` and store them;
in `_run_agent` wrap the runner call:

```python
        hold = (self._slots.hold(self._should_stop) if self._slots is not None
                else contextlib.nullcontext())
        with hold:
            res = await self._runner.run(role, prompt, wt, self._turns(stage),
                                         trace=self._trace_path(item, role.name, "jsonl"),
                                         token_budget=budget)
```

`src/agent_sdlc/decisions/decider.py`: add `import threading`, import `DeciderPort` from
`agent_sdlc.ports`, and:

```python
class LockedDecider:
    """One Laya model shared by every target thread; predict is not thread-safe (spec §5.2)."""

    def __init__(self, inner: DeciderPort) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def decide(self, gate: str, state: dict[str, Any]) -> dict[str, Decision]:
        with self._lock:
            return self._inner.decide(gate, state)
```

(If importing `agent_sdlc.ports` from `decider.py` creates an import cycle through
`agent_sdlc.agents.roles`, type the parameter as `Decider | Any` — `ports` imports `roles`, which
imports `decisions.gates`, not `decider`, so the plain import is expected to work.)

- [ ] **Step 6: CLI pause/resume per target and status flags**

In `src/agent_sdlc/cli.py` `_parser`, give `pause` and `resume` subparsers
`add_argument("--target-name")`. In `main`:

```python
    if args.cmd == "pause":
        key = f"paused:{args.target_name}" if args.target_name else "paused"
        store.set_flag(key, "1")
        store.add_event("pause", {"target": args.target_name} if args.target_name else None)
    elif args.cmd == "resume":
        if args.target_name:
            store.set_flag(f"paused:{args.target_name}", None)
        else:
            store.set_flag("paused", None)
            store.set_flag("paused_until", None)
        store.add_event("resume", {"target": args.target_name} if args.target_name else None)
```

In `_status`, read `store.get_flag(f"last_tick:{target.name}")` and
`store.get_flag(f"busy:{target.name}")`, and print
`paused: yes` when `stop_requested(store, target.name)` (import it from the scheduler module).

- [ ] **Step 7: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A src tests
git commit -m "feat: per-target pause and flags, shared session slots, locked decider

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Runtime builder and Supervisor

**Files:**
- Create: `src/agent_sdlc/orchestrator/runtime.py`
- Create: `src/agent_sdlc/orchestrator/supervisor.py`
- Modify: `src/agent_sdlc/secrets.py` (GitHub key constant)
- Test: `tests/test_runtime.py`, `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `AdoForge` (Task 2), `GitHubForge`/`GitHubAppAuth` (Tasks 5–7), `SessionSlots`, `stop_requested`, `LockedDecider` (Task 8), `GlobalConfig` (Task 1).
- Produces: `secrets.github_app_key(app_id: int) -> tuple[str, str]` returning `(f"agent-sdlc-github-app-{app_id}", "AGENT_SDLC_GITHUB_APP_KEY")`.
- Produces: `make_forge(target: TargetConfig, *, dry_run_push: bool, secret: Callable[[str, str], str] = get_secret) -> ForgePort`; `claude_auth_env(cfg: GlobalConfig, secret=get_secret) -> dict[str, str]`; `build_scheduler(target, *, cfg: GlobalConfig, store: Store, decider: DeciderPort, slots: SessionSlots, workspaces: Path, traces: Path | None, dry_run_push: bool, auth_env: dict[str, str], forge: ForgePort | None = None) -> Scheduler`.
- Produces: `Supervisor(targets: list[TargetConfig], build: Callable[[TargetConfig], Scheduler], *, restart_s: float = 60.0)` with `run_once() -> None`, `run_forever(poll_s: int) -> None`, `stop() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime.py`:

```python
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
```

Create `tests/test_supervisor.py`:

```python
import threading

from agent_sdlc.orchestrator.supervisor import Supervisor
from agent_sdlc.targets import TargetConfig

BASE = {"repo": {"install": "true", "commands": {"test": "true"}},
        "policy": {"protected_paths": ["infra/**"]},
        "forge": {"kind": "ado", "org": "o", "project": "p", "repo": "r"}}
A = TargetConfig.model_validate({**BASE, "name": "a"})
B = TargetConfig.model_validate({**BASE, "name": "b"})


class FakeScheduler:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier
        self.threads: list[int] = []
        self.ticks = 0

    async def tick(self) -> None:
        self.threads.append(threading.get_ident())
        if self.barrier is not None:
            self.barrier.wait(timeout=5)     # both targets must be inside tick at once
        self.ticks += 1

    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None:
        await self.tick()
        raise RuntimeError("crash")          # the supervisor restarts it


def test_run_once_ticks_targets_in_parallel_threads() -> None:
    barrier = threading.Barrier(2)
    scheds = {"a": FakeScheduler(barrier), "b": FakeScheduler(barrier)}
    Supervisor([A, B], lambda t: scheds[t.name]).run_once()  # type: ignore[arg-type,return-value]
    assert scheds["a"].ticks == 1 and scheds["b"].ticks == 1
    assert scheds["a"].threads != scheds["b"].threads


def test_run_once_build_failure_does_not_block_other_targets() -> None:
    ok = FakeScheduler()

    def build(t: TargetConfig) -> FakeScheduler:
        if t.name == "a":
            raise ValueError("set forge.app_id for target a")
        return ok

    Supervisor([A, B], build).run_once()  # type: ignore[arg-type]
    assert ok.ticks == 1


def test_run_forever_restarts_a_crashed_target() -> None:
    builds: list[str] = []
    sup: Supervisor

    def build(t: TargetConfig) -> FakeScheduler:
        builds.append(t.name)
        if len(builds) >= 3:
            sup.stop()
        return FakeScheduler()

    sup = Supervisor([A], build, restart_s=0.01)  # type: ignore[arg-type]
    sup.run_forever(poll_s=60)
    assert builds[:3] == ["a", "a", "a"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runtime.py tests/test_supervisor.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `runtime.py`**

Add to `src/agent_sdlc/secrets.py`:

```python
def github_app_key(app_id: int) -> tuple[str, str]:
    return (f"agent-sdlc-github-app-{app_id}", "AGENT_SDLC_GITHUB_APP_KEY")
```

Create `src/agent_sdlc/orchestrator/runtime.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from agent_sdlc.adapters.ado import AdoForge
from agent_sdlc.adapters.github import GitHubForge
from agent_sdlc.adapters.github_auth import GitHubAppAuth
from agent_sdlc.agents.runner import ClaudeAgentRunner
from agent_sdlc.config import GlobalConfig
from agent_sdlc.orchestrator.scheduler import Scheduler, stop_requested
from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.policy import CommandPolicy, PathPolicy
from agent_sdlc.ports import DeciderPort, ForgePort
from agent_sdlc.secrets import ANTHROPIC_KEY, CLAUDE_TOKEN, get_secret, github_app_key
from agent_sdlc.store import Store
from agent_sdlc.targets import AdoForgeConfig, TargetConfig
from agent_sdlc.workspaces import Workspaces

Secret = Callable[[str, str], str]


def make_forge(target: TargetConfig, *, dry_run_push: bool,
               secret: Secret = get_secret) -> ForgePort:
    f, repo = target.forge, target.repo
    if isinstance(f, AdoForgeConfig):
        return AdoForge(f, secret(f.pat_secret, "AGENT_SDLC_ADO_PAT"), intake=target.intake,
                        base_branch=repo.base_branch, branch_prefix=repo.branch_prefix,
                        dry_run_push=dry_run_push)
    if f.app_id is None:
        raise ValueError(f"set forge.app_id for target {target.name} (spec §7)")
    http = httpx.Client(base_url=f.api_url, timeout=30)
    auth = GitHubAppAuth(app_id=f.app_id, private_key=secret(*github_app_key(f.app_id)),
                         owner=f.owner, repo=f.repo, http=http,
                         installation_id=f.installation_id)
    return GitHubForge(f, auth, intake=target.intake, base_branch=repo.base_branch,
                       branch_prefix=repo.branch_prefix, http=http, dry_run_push=dry_run_push)


def claude_auth_env(cfg: GlobalConfig, secret: Secret = get_secret) -> dict[str, str]:
    if cfg.auth.mode == "subscription":
        return {"CLAUDE_CODE_OAUTH_TOKEN": secret(*CLAUDE_TOKEN)}
    return {"ANTHROPIC_API_KEY": secret(*ANTHROPIC_KEY)}


def build_scheduler(target: TargetConfig, *, cfg: GlobalConfig, store: Store,
                    decider: DeciderPort, slots: SessionSlots, workspaces: Path,
                    traces: Path | None, dry_run_push: bool, auth_env: dict[str, str],
                    forge: ForgePort | None = None) -> Scheduler:
    forge = forge or make_forge(target, dry_run_push=dry_run_push)
    ws = Workspaces(workspaces.resolve(), target, git_auth=forge.git_auth_header)
    pp = PathPolicy(target.policy.protected_paths)
    cp = CommandPolicy([*target.repo.install, *target.repo.commands.values()])

    def should_stop() -> bool:
        return stop_requested(store, target.name)

    runner = ClaudeAgentRunner(pp, cp, Path("~/.agent-sdlc/claude-config").expanduser(),
                               auth_env, should_stop=should_stop, home=ws.home,
                               max_denials=target.limits.max_denials_per_session)
    executor = StageExecutor(target=target, forge=forge, decider=decider, runner=runner,
                             workspaces=ws, path_policy=pp, decisions_for=store.decisions_for,
                             traces=traces, slots=slots, should_stop=should_stop)
    return Scheduler(target=target, store=store, executor=executor, forge=forge,
                     workspaces=ws, limits=cfg.limits)
```

- [ ] **Step 4: Implement `supervisor.py`**

```python
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from agent_sdlc.targets import TargetConfig

log = logging.getLogger(__name__)


class Runnable(Protocol):
    async def tick(self) -> None: ...
    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None: ...


class Supervisor:
    """One thread and event loop per target; a crashed or unbuildable target is retried after
    `restart_s` without affecting the others (spec §5.1)."""

    def __init__(self, targets: list[TargetConfig],
                 build: Callable[[TargetConfig], Runnable], *, restart_s: float = 60.0) -> None:
        self._targets = targets
        self._build = build
        self._restart_s = restart_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _once(self, t: TargetConfig) -> None:
        try:
            asyncio.run(self._build(t).tick())
        except Exception:
            log.exception("target %s failed this tick", t.name)

    def _serve(self, t: TargetConfig, poll_s: int) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._build(t).run_forever(poll_s, self._stop))
            except Exception:
                log.exception("target %s stopped; restarting in %ss", t.name, self._restart_s)
            self._stop.wait(self._restart_s)

    def _join(self, threads: list[threading.Thread]) -> None:
        try:
            while any(th.is_alive() for th in threads):
                for th in threads:
                    th.join(0.5)
        except KeyboardInterrupt:
            self._stop.set()
            raise

    def run_once(self) -> None:
        threads = [threading.Thread(target=self._once, args=(t,), name=f"target-{t.name}",
                                    daemon=True) for t in self._targets]
        for th in threads:
            th.start()
        self._join(threads)

    def run_forever(self, poll_s: int) -> None:
        threads = [threading.Thread(target=self._serve, args=(t, poll_s),
                                    name=f"target-{t.name}", daemon=True)
                   for t in self._targets]
        for th in threads:
            th.start()
        self._join(threads)
```

- [ ] **Step 5: Run the tests, the suite, lint and types**

Run: `uv run pytest tests/test_runtime.py tests/test_supervisor.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "feat: runtime builder and per-target Supervisor threads

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Multi-target CLI

**Files:**
- Modify: `src/agent_sdlc/cli.py` (whole-file restructure described below)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`, `load_single`, `Loaded` (Task 1); `build_scheduler`, `make_forge`, `claude_auth_env` (Task 9); `Supervisor` (Task 9); `LockedDecider` (Task 8); `Store.get_by_ref/find_external` (Task 4).
- Produces: `resolve_ref(store: Store, names: list[str], ref: str) -> Item` raising `LookupError` with a user-facing message; CLI flags `--config`, `--target` (a file; overrides `--config`), `--target-name` on `pause`, `resume`, `status`, `metrics`, `label`; `trace`/`requeue` take a `ref` string.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
TARGET_YAML = """\
name: {name}
forge: {forge}
repo: {{install: "true", commands: {{test: "true"}}}}
policy: {{protected_paths: ["infra/**"]}}
"""


@pytest.fixture
def two(tmp_path: Path) -> Path:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a.yaml").write_text(TARGET_YAML.format(
        name="rally", forge="{kind: ado, org: o, project: p, repo: r}"))
    (tmp_path / "t" / "b.yaml").write_text(TARGET_YAML.format(
        name="tri", forge="{kind: github, owner: o, repo: r, app_id: 1}"))
    cfg = tmp_path / "agent-sdlc.yaml"
    cfg.write_text("targets: [t/a.yaml, t/b.yaml]\n")
    return cfg


def run_cfg(cfg: Path, db: str, *args: str) -> int:
    return main(["--config", str(cfg), "--db", db, *args])


def test_resolve_ref(db: str) -> None:
    from agent_sdlc.cli import resolve_ref
    store = Store(db)
    store.add_item("rally", WorkItem(5, "A", "", "", "Bug", (), "u"), "b1")
    store.add_item("tri", WorkItem(5, "B", "", "", "Bug", (), "u"), "b2")
    store.add_item("tri", WorkItem(6, "C", "", "", "Bug", (), "u"), "b3")
    names = ["rally", "tri"]
    assert resolve_ref(store, names, "tri#5").title == "B"
    assert resolve_ref(store, names, "6").title == "C"
    with pytest.raises(LookupError, match="rally#5, tri#5"):
        resolve_ref(store, names, "5")
    with pytest.raises(LookupError, match="no item nope#5"):
        resolve_ref(store, names, "nope#5")
    with pytest.raises(LookupError, match="no item 404"):
        resolve_ref(store, names, "404")
    with pytest.raises(LookupError, match="use <target>#<id>"):
        resolve_ref(store, names, "abc")


def test_status_groups_targets(two: Path, db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rally", WorkItem(5, "Ado thing", "", "", "Bug", (), "u"), "b1")
    store.add_item("tri", WorkItem(5, "Gh thing", "", "", "Bug", (), "u"), "b2")
    store.set_flag("paused:tri", "1")
    assert run_cfg(two, db, "status") == 0
    out = capsys.readouterr().out
    assert "target: rally  forge: ado  paused: no" in out
    assert "target: tri  forge: github  paused: yes" in out
    assert out.index("Ado thing") < out.index("target: tri") < out.index("Gh thing")
    assert run_cfg(two, db, "status", "--target-name", "tri") == 0
    assert "target: rally" not in capsys.readouterr().out


def test_trace_by_ref_and_ambiguous(two: Path, db: str,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rally", WorkItem(5, "A", "", "", "Bug", (), "u"), "agent/5-a")
    store.add_item("tri", WorkItem(5, "B", "", "", "Bug", (), "u"), "agent/5-b")
    assert run_cfg(two, db, "trace", "tri#5") == 0
    assert 'tri#5 "B"' in capsys.readouterr().out
    assert run_cfg(two, db, "trace", "5") == 1
    assert "rally#5, tri#5" in capsys.readouterr().out


def test_requeue_local_by_ref(two: Path, db: str) -> None:
    store = Store(db)
    it = store.add_item("tri", WorkItem(7, "B", "", "", "Bug", (), "u"), "b")
    assert it is not None
    store.save(replace(it, stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run_cfg(two, db, "requeue", "tri#7", "--local") == 0
    assert Store(db).get_by_ref("tri", 7).stage is Stage.VERIFY
```

Update `test_m12_defaults_resolve_from_project_root` (spec §2.1 changes the default from a target
file to `agent-sdlc.yaml`): delete `monkeypatch.delenv("AGENT_SDLC_TARGET", …)`, add
`monkeypatch.delenv("AGENT_SDLC_CONFIG", raising=False)` and
`monkeypatch.delenv("AGENT_SDLC_TARGET", raising=False)`, and replace
`assert Path(args.target) == ROOT / "targets" / "rallysource.yaml"` with
`assert Path(args.config) == ROOT / "agent-sdlc.yaml" and args.target is None`. Keep the rest.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL (`resolve_ref` missing; `trace` expects an int).

- [ ] **Step 3: Restructure `src/agent_sdlc/cli.py`**

Parser changes:
- `--config` (default env `AGENT_SDLC_CONFIG` or `_ROOT / "agent-sdlc.yaml"`);
- `--target` default `os.environ.get("AGENT_SDLC_TARGET")` (None means "use --config");
- `requeue` and `trace`: positional `ref` (str) instead of `item_id` (int);
- `status`, `metrics`, `label`, `pause`, `resume`: `--target-name`.

Replace `_runtime` and `_global_config` with:

```python
def _load(args: argparse.Namespace) -> Loaded:
    return load_single(Path(args.target)) if args.target else load_config(Path(args.config))


def _decider(cfg: GlobalConfig, store: Store) -> DeciderPort:
    from agent_sdlc.decisions.decider import Decider, LayaPredictor, LockedDecider
    return LockedDecider(Decider(LayaPredictor(cfg.laya.model), store.calibration,
                                 cfg.laya.default_threshold))


def _scheduler(loaded: Loaded, target: TargetConfig, store: Store, args: argparse.Namespace,
               decider: DeciderPort, slots: SessionSlots, dry_run_push: bool) -> Scheduler:
    return build_scheduler(target, cfg=loaded.config, store=store, decider=decider, slots=slots,
                           workspaces=Path(args.workspaces), traces=Path(args.traces),
                           dry_run_push=dry_run_push, auth_env=claude_auth_env(loaded.config))


def resolve_ref(store: Store, names: list[str], ref: str) -> Item:
    """`<target>#<id>`, or a bare id when exactly one configured target has it (spec §4.3)."""
    if "#" in ref:
        name, _, num = ref.partition("#")
        try:
            return store.get_by_ref(name, int(num))
        except (KeyError, ValueError):
            raise LookupError(f"no item {ref}") from None
    if not ref.isdigit():
        raise LookupError(f"no item {ref} (use <target>#<id> or a number)")
    matches = store.find_external(int(ref), names)
    if not matches:
        raise LookupError(f"no item {ref}")
    if len(matches) > 1:
        refs = ", ".join(f"{m.target}#{m.external_id}" for m in matches)
        raise LookupError(f"{ref} is ambiguous: {refs}")
    return matches[0]


def _selected(loaded: Loaded, name: str | None) -> list[TargetConfig]:
    return [loaded.target(name)] if name else loaded.targets
```

`_status(loaded, store, name)` prints one global line then a block per selected target:

```python
def _status(loaded: Loaded, store: Store, name: str | None) -> None:
    now = datetime.now(UTC)
    paused = "yes" if store.get_flag("paused") == "1" else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"paused: {paused}  paused_until: {until}  "
          f"today: {today.turns} turns, {today.tokens:,} tokens")
    for target in _selected(loaded, name):
        _target_status(target, store, now)
```

`_target_status(target, store, now)` is the body of the old `_status` from
`tick = store.get_flag(f"last_tick:{target.name}")` down to the item loop, preceded by:

```python
    tp = "yes" if stop_requested(store, target.name) else "no"
    print(f"\ntarget: {target.name}  forge: {target.forge.kind}  paused: {tp}")
```

with the PR column `pr = f" PR {'!' if target.forge.kind == 'ado' else '#'}{i.pr_id}" if i.pr_id else ""`
and the busy line `busy: {target.name}#{item_id} {stage} for …`. (Old status tests assert
`"busy: #9 implement for 10m"`; update them to `"busy: rallysource#9 implement for 10m"` and
`"busy: rallysource#9 implement for 3h  STUCK?"` — the format change is spec §4.2's `target#id`
reference. `"paused: no"` still appears in the global line.)

`main`:

```python
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(Path(args.logs))
    loaded = _load(args)
    store = _store(args.db)
    names = [t.name for t in loaded.targets]

    if args.cmd == "pause":
        key = f"paused:{args.target_name}" if args.target_name else "paused"
        store.set_flag(key, "1")
        store.add_event("pause", {"target": args.target_name} if args.target_name else None)
    elif args.cmd == "resume":
        if args.target_name:
            store.set_flag(f"paused:{args.target_name}", None)
        else:
            store.set_flag("paused", None)
            store.set_flag("paused_until", None)
        store.add_event("resume", {"target": args.target_name} if args.target_name else None)
    elif args.cmd == "status":
        _status(loaded, store, args.target_name)
    elif args.cmd in ("trace", "requeue"):
        try:
            item = resolve_ref(store, names, args.ref)
        except LookupError as e:
            print(e)
            return 1
        if args.cmd == "trace":
            print(render_trace(store, item.id, args.full))
        elif args.local:
            new = requeue(item)
            store.save(new, events=[EventInput("requeue", {
                "from_reason": item.park_reason.value if item.park_reason else None,
                "to": new.stage.value, "approved": False, "local": True})], at=item)
        else:
            target = loaded.target(item.target)
            sched = _scheduler(loaded, target, store, args, _decider(loaded.config, store),
                               SessionSlots(1), dry_run_push=False)
            sched.requeue_item(item.id)
    elif args.cmd == "calibrate":
        gates = [args.gate] if args.gate else sorted(GATES)
        for gate in gates:
            for q in GATES[gate]:
                r = calibrate_question(store, gate, q, loaded.config.laya.max_ece, args.promote)
                print(f"{gate}.{q}: n={r.n} T={r.temperature:.3f} ECE={r.ece:.3f} "
                      f"acc={r.accuracy:.3f} mode={r.mode} — {r.message}")
    elif args.cmd == "label":
        if args.gate == "triage" and not args.abandoned:
            if not args.target_name and len(names) > 1:
                print(f"choose a target with --target-name ({', '.join(names)})")
                return 1
            target = loaded.target(args.target_name or names[0])
            forge = make_forge(target, dry_run_push=True)
            n = label_triage(forge, _decider(loaded.config, store), store, args.limit, input,
                             target=target.name)
        else:
            n = label_logged(store, args.gate, args.limit, input, abandoned_only=args.abandoned)
        print(f"recorded {n} labels")
    elif args.cmd == "metrics":
        now = datetime.now(UTC)
        for target in _selected(loaded, args.target_name):
            print(render_metrics(store, target.name, now - timedelta(days=args.days), now))
    elif args.cmd == "run":
        store.set_flag("poll_s", str(args.poll))
        decider = _decider(loaded.config, store)
        slots = SessionSlots(loaded.config.limits.max_concurrent_sessions)
        sup = Supervisor(loaded.targets, lambda t: _scheduler(
            loaded, t, store, args, decider, slots, args.dry_run_push))
        if args.once:
            sup.run_once()
        else:
            sup.run_forever(args.poll)
    return 0
```

Imports to add: `Loaded, GlobalConfig, load_config, load_single` from `agent_sdlc.config`;
`build_scheduler, claude_auth_env, make_forge` from `agent_sdlc.orchestrator.runtime`;
`Scheduler, in_flight, stop_requested` from `agent_sdlc.orchestrator.scheduler`; `SessionSlots`;
`Supervisor`; `DeciderPort` from `agent_sdlc.ports`; `Item` from `agent_sdlc.types`. Keep the
heavy imports (`runtime`, `decider`) inside functions if the CLI tests' import time regresses
noticeably (laya loads model weights only in `LayaPredictor.__init__`, so module imports are cheap).

Existing single-target tests call `main(["--target", …rallysource.yaml, …])`; `requeue 9 --local`
and `trace 9` still work because a bare id resolves when only one target is configured.

- [ ] **Step 4: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat: multi-target CLI with target#id refs and grouped status

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: End-to-end GitHub flavour and a two-target run

**Files:**
- Create: `tests/e2e/test_multi_forge.py`

**Interfaces:**
- Consumes: `FakeForge(kind="github")` (Task 2), `Scheduler`, `StageExecutor`, `Supervisor`, `SessionSlots`, `Store` (file DB), `Workspaces`, fixtures `origin_repo`, `target`, `git` from `tests/conftest.py`.

- [ ] **Step 1: Write the tests** — create `tests/e2e/test_multi_forge.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from agent_sdlc.orchestrator.scheduler import Scheduler
from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.orchestrator.stages import StageExecutor
from agent_sdlc.orchestrator.supervisor import Supervisor
from agent_sdlc.policy import PathPolicy
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import PrComment, Stage, WorkItem
from agent_sdlc.workspaces import Workspaces
from tests.conftest import git
from tests.fakes import FakeDecider, FakeForge, FakeRunner

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
WI = WorkItem(5, "Add feature", "Please add feature.txt", "feature.txt exists", "Bug",
              ("agent",), "u")


def _clone_origin(tmp_path: Path, origin: Path, name: str) -> Path:
    dst = tmp_path / f"{name}.git"
    git("clone", "-q", "--bare", str(origin), str(dst), cwd=tmp_path)
    return dst


def _build(target: TargetConfig, store: Store, forge: FakeForge, root: Path,
           slots: SessionSlots | None = None,
           decider: FakeDecider | None = None) -> Scheduler:
    ws = Workspaces(root / "ws", target)
    ex = StageExecutor(target=target, forge=forge, decider=decider or FakeDecider(),
                       runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=store.decisions_for, slots=slots)
    return Scheduler(target=target, store=store, executor=ex, forge=forge, workspaces=ws,
                     clock=lambda: NOW)


async def test_github_flavour_to_pr_and_review_round(tmp_path: Path, target: TargetConfig,
                                                     origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'e2e.db'}")
    gh = target.model_copy(update={"name": "gh"})
    forge = FakeForge(origin=origin_repo, kind="github")
    forge.add(WI)
    sched = _build(gh, store, forge, tmp_path)
    for _ in range(6):
        await sched.tick()
    item = store.get_by_ref("gh", 5)
    assert item.stage is Stage.AWAITING_HUMAN
    pr = forge.prs[100]
    assert pr["title"].endswith("(#5)") and "AB#" not in pr["body"]
    assert pr["body"].startswith("Automated change for #5 ")
    assert pr["body"].endswith("Closes #5")
    assert any("for PR #100" in c for _, c in forge.wi_comments)
    forge.pr_threads[100].append(PrComment(0, 1, "brian", "(changes requested with no summary)",
                                           kind="review", changes_requested=True))
    await sched.tick()
    assert store.get_by_ref("gh", 5).stage is Stage.IMPLEMENT


async def test_github_park_comment_says_label(tmp_path: Path, target: TargetConfig,
                                              origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'e2e.db'}")
    gh = target.model_copy(update={"name": "gh"})
    forge = FakeForge(origin=origin_repo, kind="github")
    forge.add(WI)
    sched = _build(gh, store, forge, tmp_path, decider=FakeDecider(shadow={"triage"}))
    await sched.tick()
    assert store.get_by_ref("gh", 5).stage is Stage.PARKED
    assert "agent:parked" in forge.tags[5]
    assert any("<code>agent:parked</code> label" in c for _, c in forge.wi_comments)


def test_two_targets_in_one_supervisor(tmp_path: Path, target: TargetConfig,
                                       origin_repo: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'two.db'}")
    slots = SessionSlots(1, poll_s=0.01)
    targets, forges = {}, {}
    for name, kind in (("rally", "ado"), ("tri", "github")):
        t = target.model_copy(update={"name": name, "repo": target.repo.model_copy(
            update={"clone_url": str(_clone_origin(tmp_path, origin_repo, name))})})
        f = FakeForge(origin=Path(str(t.repo.clone_url)), kind=kind)
        f.add(WI)
        targets[name], forges[name] = t, f

    def build(t: TargetConfig) -> Scheduler:
        return _build(t, store, forges[t.name], tmp_path / t.name, slots)

    sup = Supervisor(list(targets.values()), build)
    for _ in range(6):
        sup.run_once()
    for name in ("rally", "tri"):
        assert store.get_by_ref(name, 5).stage is Stage.AWAITING_HUMAN, name
    assert forges["rally"].prs[100]["title"].endswith("(AB#5)")
    assert forges["tri"].prs[100]["body"].endswith("Closes #5")
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/e2e/test_multi_forge.py -q`
Expected: PASS. A failure here is a real integration bug in Tasks 1–10: fix it in the owning
module, not in this test.

- [ ] **Step 3: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_multi_forge.py
git commit -m "test: e2e GitHub flavour and two targets in one Supervisor

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: triathlon target, README and pilot check

**Files:**
- Create: `targets/triathlon.yaml`
- Modify: `agent-sdlc.yaml` (commented triathlon entry)
- Modify: `README.md`
- Test: `tests/test_targets.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test** — append to `tests/test_targets.py`:

```python
def test_triathlon_target() -> None:
    t = load_target(ROOT / "targets" / "triathlon.yaml")
    assert isinstance(t.forge, GitHubForgeConfig)
    assert (t.forge.owner, t.forge.repo) == ("paradigmbrian", "triathlon-agent")
    assert t.repo.base_branch == "main"
    assert t.repo.install == ["uv sync --all-packages", "npm ci --prefix web"]
    assert t.clone_url == "https://github.com/paradigmbrian/triathlon-agent.git"
    pp = PathPolicy(t.policy.protected_paths)
    for p in ["migrations/007_x.sql", ".env", "packages/tri-core/.env.local",
              "docker-compose.yml", "setup.sh", "conftest.py", ".claude/settings.local.json",
              ".github/workflows/ci.yml", "web/vite.config.ts", "web/eslint.config.js",
              "web/playwright.config.ts", "web/tsconfig.app.json"]:
        assert pp.is_protected(p), p
    for p in ["packages/tri-core/tests/conftest.py", "packages/tri-core/src/tri_core/db.py",
              "web/src/App.tsx", "web/tests/app.test.tsx"]:
        assert not pp.is_protected(p), p
    mp = PathPolicy(t.policy.manifest_paths)
    assert mp.violations(["pyproject.toml", "packages/tri-web/pyproject.toml", "uv.lock",
                          "web/package.json", "web/package-lock.json", "web/src/a.ts"]) == [
        "packages/tri-web/pyproject.toml", "pyproject.toml", "uv.lock",
        "web/package-lock.json", "web/package.json"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_targets.py::test_triathlon_target -q`
Expected: FAIL (FileNotFoundError).

- [ ] **Step 3: Create `targets/triathlon.yaml`**

```yaml
name: triathlon
forge:
  kind: github
  owner: paradigmbrian
  repo: triathlon-agent
  app_id: null                  # set to the GitHub App id (README: GitHub setup)
  installation_id: null         # null: looked up from the repo
intake:
  label: agent
  parked_label: "agent:parked"
repo:
  base_branch: main
  branch_prefix: agent/
  install:
    - uv sync --all-packages
    - npm ci --prefix web
  commands:
    test: uv run pytest
    lint: uv run ruff check .
    typecheck: uv run mypy
    web_lint: npm run lint --prefix web
    web_test: npm run test --prefix web
    web_build: npm run build --prefix web
  command_timeout_s: 1200
  env_template: null
policy:
  protected_paths:
    - "migrations/**"
    - ".env*"
    - "**/.env*"
    - "docker-compose.yml"
    - "setup.sh"
    - "conftest.py"             # root pytest config; package conftests are test code
    - ".claude/**"
    - ".github/**"
    - "web/*.config.*"          # vite, eslint, playwright
    - "web/tsconfig*.json"
    - "**/*.pem"
    - "**/*.key"
  manifest_paths:
    - "**/pyproject.toml"
    - "uv.lock"
    - "web/package.json"
    - "web/package-lock.json"
  max_diff_lines: 600
limits:
  max_concurrent_items: 1
  max_verify_retries: 3
  max_pr_rounds: 3
  max_turns: {plan: 30, implement: 80, review: 30}
  max_item_tokens: 2000000
  max_denials_per_session: 5
  stale_after_minutes: 120
```

If `pyproject.toml` at the root is not matched by `**/pyproject.toml` in `PathPolicy` (check
`policy.py`'s `_glob_to_regex`: `**/` should match zero directories, as RallySource's
`**/package.json` matching `package.json` shows), keep the test as the source of truth and add
`"pyproject.toml"` explicitly.

In `agent-sdlc.yaml`, below `- targets/rallysource.yaml`, add:

```yaml
  # - targets/triathlon.yaml    # enable after the GitHub setup in README.md
```

- [ ] **Step 4: Update `README.md`**

Make these edits (keep all other text):
1. Intro sentence: "Azure DevOps work items or GitHub issues tagged `agent` are triaged by …, and
   opened as PRs." Add under it: "Design: … plus `docs/superpowers/specs/2026-09-26-multi-forge-design.md`
   (GitHub and parallel targets)."
2. New section **Configuration** before "Everyday use":

```markdown
## Configuration

- `agent-sdlc.yaml` lists the targets and holds settings shared by the one Claude subscription:
  `auth`, `laya`, daily turn/token caps, `max_concurrent_sessions`, `run_window`.
- `targets/<name>.yaml` holds one repository: `forge` (`kind: ado` or `kind: github`),
  `intake` labels, `repo` (base branch, branch prefix, install list, verify commands), `policy`
  and per-item `limits`.
- `--config <file>` picks another global config; `--target <file>` runs a single target with
  default global settings.
- State lives in `~/.agent-sdlc/agent-sdlc-v2.db`. Items are referenced as `<target>#<id>`
  (`agent-sdlc trace triathlon#12`); a bare id works when only one target has it.
```

3. New section **GitHub setup (per repository, done by a human)** with spec §7 steps 1–6 verbatim
   in substance: create a private GitHub App (webhook off; Contents, Issues, Pull requests
   read & write; Metadata read), install it on the repo only; store the key with
   `security add-generic-password -s agent-sdlc-github-app-<app_id> -a $USER -T <resolved interpreter path> -w "$(cat <key>.pem)"`
   and delete the `.pem` (or export `AGENT_SDLC_GITHUB_APP_KEY` in the launching shell); add a
   `main` ruleset requiring a PR with 1 approval and no App bypass; create the `agent` and
   `agent:parked` labels; run the pilot check; set `forge.app_id` in the target file and
   uncomment the target in `agent-sdlc.yaml`.
4. "Everyday use": add `uv run agent-sdlc pause --target-name triathlon` and
   `uv run agent-sdlc status --target-name triathlon`, and change `requeue <id>` / `trace <id>`
   to `requeue <target>#<id>` / `trace <target>#<id>`.
5. "Opt a work item in …": "Opt an item in by adding the `agent` tag (ADO) or label (GitHub). … On a
   PR, start a comment with `/agent` to request a revision; on GitHub a \"Request changes\" review
   also counts."
6. Traces paragraph: `~/.agent-sdlc/traces/<target>/<id>/`; log lines tagged `[<target>#<id> <stage>]`.

- [ ] **Step 5: Pilot check of triathlon verify commands (read-only on the real repo)**

Run, from the scratch directory (not the real checkout):

```bash
S=$(mktemp -d) && git clone -q https://github.com/paradigmbrian/triathlon-agent.git "$S/tri" \
  && cd "$S/tri" && env -u DATABASE_URL -u TEST_DATABASE_URL HOME="$S/home" sh -c '
  uv sync --all-packages && npm ci --prefix web &&
  uv run pytest -q; echo "pytest=$?";
  uv run ruff check .; echo "ruff=$?";
  uv run mypy; echo "mypy=$?";
  npm run lint --prefix web; echo "web_lint=$?";
  npm run test --prefix web; echo "web_test=$?";
  npm run build --prefix web; echo "web_build=$?"'
```

If the clone needs credentials (private repo), use the local checkout as the source instead:
`git clone -q /Users/brian/Development/paradigm/triathlon-agent "$S/tri"` — a clone is read-only for
the source repo. Expected: every `…=0`. For any non-zero result caused by the environment (no
Postgres, no `.env`, no network for a live test), narrow that command in `targets/triathlon.yaml`
(for example `uv run pytest -m "not db and not live"`) and record the reason as a YAML comment on
that line. Do not change the triathlon repository.

- [ ] **Step 6: Run the suite, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add targets/triathlon.yaml agent-sdlc.yaml README.md tests/test_targets.py
git commit -m "feat: triathlon GitHub target and README setup for GitHub and multi-target runs

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Hand-off note for Brian (no code)**

Report: the pilot-check results per command, any narrowed commands, and the remaining manual
steps from spec §7 (create the App, store the key, ruleset, labels, set `app_id`, uncomment the
target), then the manual smoke sequence from spec §8: `run --once --dry-run-push --target
targets/triathlon.yaml` on one real issue, one real run, then both targets under `agent-sdlc.yaml`.
Export any new markdown to the Obsidian vault per Brian's global instructions
(`/Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/…`, kebab-case names).
