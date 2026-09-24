from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_sdlc.agents.roles import Role
from agent_sdlc.policy import CommandPolicy, PathPolicy
from agent_sdlc.types import AgentInfraError, AgentInterrupted, AgentResult, Usage, UsageLimitError

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


def _check_tool(role: Role, cwd: Path, path_policy: PathPolicy, command_policy: CommandPolicy,
                tool_name: str, tool_input: dict[str, Any]) -> str | None:
    if tool_name not in role.tools:
        return f"tool {tool_name} is not permitted for {role.name}"
    if tool_name in _WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return path_policy.check_write(str(path), cwd)
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
                 home: Path | None = None):
        self._pp = path_policy
        self._cp = command_policy
        self._config_dir = config_dir
        self._home = home or config_dir.parent / "agent-home"
        self._auth_env = auth_env
        self._should_stop = should_stop

    async def run(self, role: Role, prompt: str, cwd: Path, max_turns: int) -> AgentResult:
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
        denied: list[str] = []

        async def pre_tool_use(input_data: HookInput, tool_use_id: str | None,
                               context: HookContext) -> SyncHookJSONOutput:
            # input_data is a TypedDict union; only PreToolUse events reach this matcher, and
            # PreToolUseHookInput carries tool_name/tool_input, so a plain dict view is safe here.
            data = cast(dict[str, Any], input_data)
            reason = check_tool(role, cwd, self._pp, self._cp, data.get("tool_name", ""),
                                data.get("tool_input") or {})
            if reason is None:
                return {}
            denied.append(f"{data.get('tool_name')}: {reason}")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by agent-sdlc policy: {reason}"}}

        options = ClaudeAgentOptions(
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
        texts: list[str] = []
        result: Any = None
        reset_at: datetime | None = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if self._should_stop():
                        await client.interrupt()
                        raise AgentInterrupted(role.name)
                    if isinstance(msg, AssistantMessage):
                        texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                    elif isinstance(msg, RateLimitEvent):
                        info = msg.rate_limit_info
                        if info.status == "rejected" and info.resets_at:  # M11
                            reset_at = datetime.fromtimestamp(int(info.resets_at), UTC)
                    elif isinstance(msg, ResultMessage):
                        result = msg
        except AgentInterrupted:
            raise
        except Exception as e:
            if getattr(e, "api_error_status", None) == 429 or parse_usage_limit(str(e)):
                raise UsageLimitError(str(e), reset_at or _reset_from_text(str(e))) from e
            if isinstance(e, ClaudeSDKError):
                raise AgentInfraError(f"{role.name}: {type(e).__name__}: {e}") from e
            raise
        if result is None:
            raise AgentInfraError(f"{role.name}: agent session ended without a result")
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
            raise UsageLimitError(text, reset_at or _reset_from_text(text), usage)
        error = str(getattr(result, "subtype", "") or "error") if result.is_error else ""
        return AgentResult(text, usage, tuple(denied), bool(result.is_error), error)
