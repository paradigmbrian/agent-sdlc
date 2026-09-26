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
