from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from laya_sdlc.agents.roles import Role
from laya_sdlc.policy import CommandPolicy, PathPolicy
from laya_sdlc.types import AgentInterrupted, AgentResult, Usage, UsageLimitError

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_USAGE_LIMIT = re.compile(r"usage limit|rate[_ ]limit|\b429\b|hit your limit|limit reached",
                          re.IGNORECASE)
# Secrets the orchestrator may hold that agent subprocesses must never see.
_BLANKED = ("LAYA_SDLC_ADO_PAT", "AZURE_DEVOPS_EXT_PAT", "SYSTEM_ACCESSTOKEN")
# The two mutually exclusive Claude auth modes; whichever isn't in use must be blanked so an
# inherited value can't silently override the auth_env the caller chose (ruling R11).
_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def parse_usage_limit(text: str) -> bool:
    return bool(_USAGE_LIMIT.search(text or ""))


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
        return path_policy.check_read(str(path), cwd) if path else None
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        reason = command_policy.check(command)
        return reason if reason is not None else _bash_path_violation(command, cwd, path_policy)
    return None


def _bash_path_violation(command: str, cwd: Path, path_policy: PathPolicy) -> str | None:
    """Deny Bash commands that reference a path outside the worktree, even when the command
    itself (e.g. `cat`, `grep`) is allowlisted."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for token in argv:
        candidate = token
        if candidate.startswith("-"):
            if "=" not in candidate:
                continue
            candidate = candidate.split("=", 1)[1]
        if not candidate:
            continue
        if candidate.startswith("/") or candidate.startswith("~"):
            return f"path is outside the worktree: {token}"
        if ".." in candidate.split("/") and path_policy.check_read(candidate, cwd) is not None:
            return f"path is outside the worktree: {token}"
    return None


def agent_env(config_dir: Path, auth_env: dict[str, str]) -> dict[str, str]:
    env = {k: "" for k in _BLANKED if k in os.environ}
    for active, other in (_AUTH_VARS, _AUTH_VARS[::-1]):
        if active in auth_env and other in os.environ:
            env[other] = ""
    env.update({
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    })
    env.update(auth_env)
    return env


class ClaudeAgentRunner:
    def __init__(self, path_policy: PathPolicy, command_policy: CommandPolicy, config_dir: Path,
                 auth_env: dict[str, str], should_stop: Callable[[], bool] = lambda: False):
        self._pp = path_policy
        self._cp = command_policy
        self._config_dir = config_dir
        self._auth_env = auth_env
        self._should_stop = should_stop

    async def run(self, role: Role, prompt: str, cwd: Path, max_turns: int) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            HookMatcher,
            ResultMessage,
            TextBlock,
        )
        from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

        self._config_dir.mkdir(parents=True, exist_ok=True)
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
                "permissionDecisionReason": f"Blocked by laya-sdlc policy: {reason}"}}

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
            env=agent_env(self._config_dir, self._auth_env),
        )
        texts: list[str] = []
        result: Any = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if self._should_stop():
                        await client.interrupt()
                        raise AgentInterrupted(role.name)
                    if isinstance(msg, AssistantMessage):
                        texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                    elif isinstance(msg, ResultMessage):
                        result = msg
        except AgentInterrupted:
            raise
        except Exception as e:
            if parse_usage_limit(str(e)):
                raise UsageLimitError(str(e)) from e
            raise
        if result is None:
            raise RuntimeError(f"{role.name}: agent session ended without a result")
        text = (getattr(result, "result", None) or (texts[-1] if texts else "")).strip()
        if result.is_error and parse_usage_limit(text):
            raise UsageLimitError(text)
        u = getattr(result, "usage", None) or {}
        usage = Usage(
            turns=int(getattr(result, "num_turns", 0) or 0),
            input_tokens=(int(u.get("input_tokens", 0))
                         + int(u.get("cache_creation_input_tokens", 0))
                         + int(u.get("cache_read_input_tokens", 0))),
            output_tokens=int(u.get("output_tokens", 0)),
        )
        return AgentResult(text, usage, tuple(denied), bool(result.is_error))
