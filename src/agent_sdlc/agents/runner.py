from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_sdlc.agents.roles import Role
from agent_sdlc.agents.transcript import TranscriptWriter
from agent_sdlc.policy import CommandPolicy, PathPolicy, _relative, categorize
from agent_sdlc.types import (
    ESCALATE_CATEGORIES,
    AgentInfraError,
    AgentInterrupted,
    AgentResult,
    Denial,
    Usage,
    UsageLimitError,
)
from agent_sdlc.workspaces import git_env

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Narrow on purpose (I2): only the provider's own usage/rate-limit wording or an HTTP 429 status,
# never free text like "rate limiting" that could appear in an ordinary error result.
_USAGE_LIMIT = re.compile(
    r"usage limit reached|hit your limit|rate_limit_error"
    r"|API Error: 429\b|status(?: code)?:? 429\b",
    re.IGNORECASE)
_RESET_EPOCH = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)
# Inherited variables agent sessions may keep (C2). Everything else in os.environ is set to ""
# because the SDK merges os.environ into the CLI's environment.
_ENV_ALLOW = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "TERM", "NVM_DIR",
                        "NVM_BIN", "CLAUDE_CODE_ENTRYPOINT"})
# SDK sandbox for Bash (C2): no escape hatch, no network, no Unix sockets (e.g. ssh-agent).
SANDBOX: dict[str, Any] = {
    "enabled": True,
    "autoAllowBashIfSandboxed": False,
    "allowUnsandboxedCommands": False,
    "excludedCommands": [],
    "network": {"allowedDomains": [], "allowUnixSockets": [], "allowAllUnixSockets": False,
                "allowLocalBinding": False},
}

log = logging.getLogger(__name__)
# After an escalation interrupt, wait this long for the SDK's final ResultMessage (spec §5.3).
_INTERRUPT_GRACE_S = 30.0


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"Blocked by agent-sdlc policy: {reason}"}}


def _estimate(per_message: dict[str, dict[str, Any]]) -> Usage:
    """Usage summed from AssistantMessage.usage, one entry per API message id."""
    u = per_message.values()
    return Usage(
        turns=len(per_message),
        input_tokens=sum(int(x.get("input_tokens", 0))
                         + int(x.get("cache_creation_input_tokens", 0)) for x in u),
        output_tokens=sum(int(x.get("output_tokens", 0)) for x in u),
        cache_read_tokens=sum(int(x.get("cache_read_input_tokens", 0)) for x in u),
    )


def parse_usage_limit(text: str) -> bool:
    return bool(_USAGE_LIMIT.search(text or ""))


def _reset_from_text(text: str) -> datetime | None:
    m = _RESET_EPOCH.search(text or "")
    return datetime.fromtimestamp(int(m.group(1)), UTC) if m else None


def check_tool(role: Role, cwd: Path, path_policy: PathPolicy, command_policy: CommandPolicy,
               tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return a denial reason, or None to allow. Pure so it can be unit-tested. Fails closed:
    any exception raised while evaluating policy (e.g. a NUL byte or a symlink loop in a path)
    is treated as a denial rather than letting the call through."""
    try:
        return _check_tool(role, cwd, path_policy, command_policy, tool_name, tool_input)
    except Exception as e:
        return f"policy check failed: {type(e).__name__}: {e}"


def _compact(obj: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(obj)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate_tool(role: Role, cwd: Path, path_policy: PathPolicy, command_policy: CommandPolicy,
                  tool_name: str, tool_input: dict[str, Any]) -> Denial | None:
    """check_tool, returned as a categorized Denial (spec §5.3)."""
    reason = check_tool(role, cwd, path_policy, command_policy, tool_name, tool_input)
    if reason is None:
        return None
    return Denial(tool_name, categorize(reason), reason, _compact(tool_input))


def _check_tool(role: Role, cwd: Path, path_policy: PathPolicy, command_policy: CommandPolicy,
                tool_name: str, tool_input: dict[str, Any]) -> str | None:
    if tool_name not in role.tools:
        return f"tool {tool_name} is not permitted for {role.name}"
    if tool_name in _WRITE_TOOLS:
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if (reason := path_policy.check_write(target, cwd)) is not None:
            return reason
        # Write/Edit are outside the Bash sandbox: a gitignored path (node_modules, dist, ...)
        # is invisible to commit, the diff checks and the manifest gate, so deny it (I0).
        # Only in a real worktree, where .git is a file or a directory. Fails closed.
        rel = _relative(target, cwd)
        if (cwd / ".git").exists() and (rel is None or _git_ignored(rel, cwd) is not False):
            return f"protected path: {rel} is gitignored"
        return None
    if tool_name == "Read":
        return path_policy.check_read(str(tool_input.get("file_path", "")), cwd)
    if tool_name in ("Glob", "Grep"):
        path = tool_input.get("path")
        if path and (reason := path_policy.check_read(str(path), cwd)) is not None:
            return reason
        pattern = str(tool_input.get("pattern", "")) if tool_name == "Glob" else ""
        if pattern.startswith("~"):
            return "path is outside the worktree"
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:  # M3
            base = Path(str(path)) if path else cwd
            return path_policy.check_read(str(base / pattern), cwd)
        return None
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        reason = command_policy.check(command)
        return reason if reason is not None else _bash_path_violation(command, cwd, path_policy)
    return None


def _git_ignored(path: str, cwd: Path) -> bool | None:
    """True if git ignores `path` in the worktree at `cwd`, False if not, None on error."""
    try:
        code = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=cwd,
                              capture_output=True, env=git_env()).returncode
    except OSError:
        return None
    return {0: True, 1: False}.get(code)


def _bash_path_violation(command: str, cwd: Path, path_policy: PathPolicy) -> str | None:
    """Deny Bash commands that reference a path outside the worktree, even when the command
    itself (e.g. `cat`, `grep`) is allowlisted and even via a symlink that resolves outside.
    argv[0] (the executable) is not path-checked, since an allowlisted target command may itself
    be an absolute path (e.g. `/usr/local/bin/eslint`)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for token in argv[1:]:
        candidate = token
        if candidate.startswith("-"):
            if "=" not in candidate:
                continue
            candidate = candidate.split("=", 1)[1]
        if not candidate:
            continue
        if candidate.startswith("~"):
            return f"path is outside the worktree: {token}"
        if path_policy.check_read(candidate, cwd) is not None:
            return f"path is outside the worktree: {token}"
    return None


def agent_env(config_dir: Path, auth_env: dict[str, str],
              home: Path | None = None) -> dict[str, str]:
    """Environment overrides for an agent session. The SDK merges os.environ underneath these,
    so every inherited variable outside _ENV_ALLOW is neutralized with "" (credentials such as
    SSH_AUTH_SOCK, cloud keys, tokens and the unused Claude auth var). HOME is a scratch dir."""
    env = {k: "" for k in os.environ if k not in _ENV_ALLOW}
    env.update({
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    })
    if home is not None:
        env["HOME"] = str(home)
    env.update(auth_env)
    return env


class ClaudeAgentRunner:
    def __init__(self, path_policy: PathPolicy, command_policy: CommandPolicy, config_dir: Path,
                 auth_env: dict[str, str], should_stop: Callable[[], bool] = lambda: False,
                 home: Path | None = None, max_denials: int = 5):
        self._pp = path_policy
        self._cp = command_policy
        self._config_dir = config_dir
        self._home = home or config_dir.parent / "agent-home"
        self._auth_env = auth_env
        self._should_stop = should_stop
        self._max_denials = max_denials

    async def run(self, role: Role, prompt: str, cwd: Path, max_turns: int,
                  trace: Path | None = None, token_budget: int | None = None) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ClaudeSDKError,
            HookMatcher,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
        )
        from claude_agent_sdk.types import (
            HookContext,
            HookInput,
            SandboxSettings,
            SyncHookJSONOutput,
        )

        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._home.mkdir(parents=True, exist_ok=True)
        denials: list[Denial] = []
        escalated: str | None = None
        per_message: dict[str, dict[str, Any]] = {}
        writer = TranscriptWriter(trace)
        writer.prompt(role.name, prompt)

        async def pre_tool_use(input_data: HookInput, tool_use_id: str | None,
                               context: HookContext) -> SyncHookJSONOutput:
            nonlocal escalated
            # input_data is a TypedDict union; only PreToolUse events reach this matcher, and
            # PreToolUseHookInput carries tool_name/tool_input, so a plain dict view is safe here.
            data = cast(dict[str, Any], input_data)
            tool = str(data.get("tool_name", ""))
            denial = evaluate_tool(role, cwd, self._pp, self._cp, tool,
                                   data.get("tool_input") or {})
            if denial is None:
                return {}
            denials.append(denial)
            writer.denied(denial)
            log.warning("denied %s %s [%s]: %s", role.name, tool, denial.category, denial.reason)
            newly_escalated = False
            if escalated is None:
                if denial.category in ESCALATE_CATEGORIES:
                    escalated = denial.category
                    newly_escalated = True
                elif len(denials) >= self._max_denials:
                    escalated = "denial_threshold"
                    newly_escalated = True
            output = _deny(denial.reason)
            if newly_escalated:
                output["continue_"] = False
                output["stopReason"] = f"agent-sdlc stopped the session: {escalated}"
            return cast(SyncHookJSONOutput, output)

        options = ClaudeAgentOptions(  # unchanged from before
            system_prompt=role.system_prompt,
            cwd=str(cwd),
            tools=list(role.tools),
            allowed_tools=list(role.tools),
            permission_mode="dontAsk",
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            max_turns=max_turns,
            setting_sources=[],
            strict_mcp_config=True,
            env=agent_env(self._config_dir, self._auth_env, self._home),
            sandbox=cast(SandboxSettings, SANDBOX),
        )

        def partial(usage: Usage | None = None) -> AgentResult:
            return AgentResult("", usage or _estimate(per_message), tuple(denials),
                               escalated=escalated, trace=writer.path,
                               trace_error=writer.error, role=role.name,
                               usage_estimated=usage is None)

        texts: list[str] = []

        def escalated_result() -> AgentResult:
            return replace(partial(), text=texts[-1].strip() if texts else "")

        result: Any = None
        reset_at: datetime | None = None
        stopping = False
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                try:
                    async with asyncio.timeout(None) as window:
                        async for msg in client.receive_response():
                            writer.message(msg)
                            if self._should_stop():
                                await client.interrupt()
                                if escalated is not None:  # keep the policy stop (I4)
                                    return escalated_result()
                                raise AgentInterrupted(role.name, partial=partial())
                            if isinstance(msg, AssistantMessage):
                                texts += [b.text for b in msg.content
                                          if isinstance(b, TextBlock)]
                                u = getattr(msg, "usage", None)
                                if u:
                                    key = getattr(msg, "message_id", None) or f"m{len(per_message)}"
                                    per_message[key] = dict(u)
                            elif isinstance(msg, RateLimitEvent):
                                info = msg.rate_limit_info
                                if info.status == "rejected" and info.resets_at:  # M11
                                    reset_at = datetime.fromtimestamp(int(info.resets_at), UTC)
                            elif isinstance(msg, ResultMessage):
                                result = msg
                            if (escalated is None and token_budget is not None
                                    and _estimate(per_message).tokens > token_budget):
                                escalated = "budget"
                            if escalated is not None and not stopping:
                                stopping = True
                                log.warning("stopping %s session: %s", role.name, escalated)
                                window.reschedule(
                                    asyncio.get_running_loop().time() + _INTERRUPT_GRACE_S)
                                try:
                                    await client.interrupt()
                                except Exception as e:
                                    log.warning("interrupt failed for %s: %s", role.name, e)
                except TimeoutError:
                    if not stopping:
                        raise
        except AgentInterrupted:
            raise
        except Exception as e:
            # A stopped session often ends in an SDK error; the policy stop wins (I1).
            if escalated is not None:
                return escalated_result()
            if getattr(e, "api_error_status", None) == 429 or parse_usage_limit(str(e)):
                raise UsageLimitError(str(e), reset_at or _reset_from_text(str(e)),
                                      partial=partial()) from e
            if isinstance(e, ClaudeSDKError):
                raise AgentInfraError(f"{role.name}: {type(e).__name__}: {e}",
                                      partial=partial()) from e
            raise
        finally:
            writer.close()
        if result is None:
            if escalated is not None:
                return escalated_result()
            raise AgentInfraError(f"{role.name}: agent session ended without a result",
                                  partial=partial())
        text = (getattr(result, "result", None) or (texts[-1] if texts else "")).strip()
        u = getattr(result, "usage", None) or {}
        usage = Usage(
            turns=int(getattr(result, "num_turns", 0) or 0),
            input_tokens=(int(u.get("input_tokens", 0))
                         + int(u.get("cache_creation_input_tokens", 0))),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
        )
        if result.is_error and (getattr(result, "api_error_status", None) == 429
                                or parse_usage_limit(text)):
            raise UsageLimitError(text, reset_at or _reset_from_text(text), usage,
                                  partial=partial(usage))
        error = str(getattr(result, "subtype", "") or "error") if result.is_error else ""
        cost = getattr(result, "total_cost_usd", None)
        return AgentResult(
            text, usage, tuple(denials), bool(result.is_error), error, escalated=escalated,
            session_id=str(getattr(result, "session_id", "") or ""),
            duration_ms=int(getattr(result, "duration_ms", 0) or 0),
            cost_usd=float(cost) if cost is not None else None,
            trace=writer.path, trace_error=writer.error, role=role.name)
