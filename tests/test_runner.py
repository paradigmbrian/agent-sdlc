import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from laya_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    implementer_prompt,
    reviewer_prompt,
)
from laya_sdlc.agents.runner import ClaudeAgentRunner, agent_env, check_tool, parse_usage_limit
from laya_sdlc.policy import CommandPolicy, PathPolicy
from laya_sdlc.types import AgentInterrupted, CommandResult, Usage, UsageLimitError, WorkItem

PP = PathPolicy(["infra/**", "**/.env*"])
CP = CommandPolicy(["npm test"])
WI = WorkItem(5, "Fix login", "Login broken", "Login works", "Bug", ("laya",), "u")


def test_role_tools() -> None:
    assert "Write" not in PLANNER.tools and "Edit" not in REVIEWER.tools
    assert {"Edit", "Write"} <= set(IMPLEMENTER.tools)


def test_check_tool_rules(tmp_path: Path) -> None:
    def chk(role, tool, inp):  # type: ignore[no-untyped-def]
        return check_tool(role, tmp_path, PP, CP, tool, inp)

    assert (chk(PLANNER, "Write", {"file_path": "a.ts"})
            == "tool Write is not permitted for planner")
    assert chk(IMPLEMENTER, "Write", {"file_path": "src/a.ts"}) is None
    assert (chk(IMPLEMENTER, "Edit", {"file_path": "infra/x.bicep"})
            == "protected path: infra/x.bicep")
    assert chk(IMPLEMENTER, "Write", {"file_path": "../x"}) == "path is outside the worktree"
    assert chk(IMPLEMENTER, "Read", {"file_path": "/etc/hosts"}) == "path is outside the worktree"
    assert chk(IMPLEMENTER, "Grep", {"pattern": "x"}) is None
    assert (chk(IMPLEMENTER, "Glob", {"pattern": "*", "path": "/"})
            == "path is outside the worktree")
    assert chk(IMPLEMENTER, "Bash", {"command": "npm test -- a.spec.ts"}) is None
    assert chk(IMPLEMENTER, "Bash", {"command": "git push"}) == "command not allowlisted: git"
    assert (chk(IMPLEMENTER, "WebFetch", {"url": "x"})
            == "tool WebFetch is not permitted for implementer")


def test_check_tool_fails_closed_on_policy_exception(tmp_path: Path) -> None:
    # A NUL byte makes PathPolicy._relative's Path.resolve() raise ValueError.
    reason = check_tool(IMPLEMENTER, tmp_path, PP, CP, "Write", {"file_path": "a\x00b"})
    assert reason is not None
    assert reason.startswith("policy check failed: ValueError")

    # A symlink loop makes Path.resolve() raise RuntimeError.
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    reason = check_tool(IMPLEMENTER, tmp_path, PP, CP, "Read", {"file_path": "loop/x"})
    assert reason is not None
    assert reason.startswith("policy check failed: RuntimeError")


def test_check_tool_bash_no_path_escape(tmp_path: Path) -> None:
    cp = CommandPolicy(["npm run lint"])

    def chk(cmd: str) -> str | None:
        return check_tool(IMPLEMENTER, tmp_path, PP, cp, "Bash", {"command": cmd})

    assert chk("cat /etc/hosts") == "path is outside the worktree: /etc/hosts"
    assert chk("cat ~/.ssh/id_rsa") == "path is outside the worktree: ~/.ssh/id_rsa"
    assert chk("grep -r x ../../") == "path is outside the worktree: ../../"
    assert chk("ls ../other") == "path is outside the worktree: ../other"
    assert chk("cat src/a.ts") is None
    assert chk("grep -rn foo apps") is None
    assert chk("npm run lint") is None


def test_agent_env_isolates_config(tmp_path: Path) -> None:
    env = agent_env(tmp_path / "cfg", {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"


def test_agent_env_blanks_ado_pat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYA_SDLC_ADO_PAT", "secret")
    env = agent_env(tmp_path, {})
    assert env["LAYA_SDLC_ADO_PAT"] == ""


def test_agent_env_blanks_inactive_anthropic_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked")
    env = agent_env(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"


def test_agent_env_blanks_inactive_oauth_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "leaked")
    env = agent_env(tmp_path, {"ANTHROPIC_API_KEY": "k"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["ANTHROPIC_API_KEY"] == "k"


@pytest.mark.parametrize("text,expected", [
    ("Claude AI usage limit reached|1760000000", True),
    ("API Error: 429 rate_limit_error", True),
    ("You've hit your limit · resets 5pm", True),
    ("TypeError: cannot read properties of undefined", False),
])
def test_parse_usage_limit(text: str, expected: bool) -> None:
    assert parse_usage_limit(text) is expected


def test_prompts_include_context() -> None:
    p = implementer_prompt(WI, "1. do x", "test failed: y")
    assert "Fix login" in p and "1. do x" in p and "test failed: y" in p
    r = reviewer_prompt(WI, "plan", "+diff", [CommandResult("test", "npm test", 1, "boom", 1.0)])
    assert "+diff" in r and "test (exit 1)" in r and "boom" in r


# --- ClaudeAgentRunner.run, against a fake claude_agent_sdk injected into sys.modules ---


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], script: list[Any]
) -> types.ModuleType:
    """Build and install a minimal stand-in for the `claude_agent_sdk` package. `script` is
    consumed lazily (by reference) inside `receive_response`, so tests can build message
    instances from the returned module's own classes (matching runner.py's isinstance checks)
    and append them to `script` after this call. Items in `script` are yielded as messages,
    except: a BaseException is raised instead of yielded, and a callable is awaited (if it
    returns an awaitable) with the client instance and produces no message — used to invoke the
    registered PreToolUse hook mid-stream, as the real SDK would when the model calls a tool."""

    class ClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class HookMatcher:
        def __init__(self, matcher: Any = None, hooks: Any = None, timeout: Any = None) -> None:
            self.matcher = matcher
            self.hooks = hooks or []
            self.timeout = timeout

    class TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class AssistantMessage:
        def __init__(self, content: list[Any]) -> None:
            self.content = content

    class ResultMessage:
        def __init__(self, is_error: bool = False, num_turns: int = 0,
                    usage: dict[str, Any] | None = None, result: str | None = None) -> None:
            self.is_error = is_error
            self.num_turns = num_turns
            self.usage = usage
            self.result = result

    class ClaudeSDKClient:
        def __init__(self, options: Any = None) -> None:
            self.options = options
            self.interrupted = False
            captured["options"] = options
            captured["client"] = self

        async def __aenter__(self) -> "ClaudeSDKClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def query(self, prompt: str) -> None:
            captured["prompt"] = prompt

        async def receive_response(self) -> Any:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    out = item(self)
                    if asyncio.iscoroutine(out):
                        await out
                    continue
                yield item

        async def interrupt(self) -> None:
            self.interrupted = True

    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    sdk.HookMatcher = HookMatcher  # type: ignore[attr-defined]
    sdk.TextBlock = TextBlock  # type: ignore[attr-defined]
    sdk.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    sdk.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    sdk.ClaudeSDKClient = ClaudeSDKClient  # type: ignore[attr-defined]

    sdk_types = types.ModuleType("claude_agent_sdk.types")
    sdk_types.HookContext = object  # type: ignore[attr-defined]
    sdk_types.HookInput = object  # type: ignore[attr-defined]
    sdk_types.SyncHookJSONOutput = dict  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", sdk_types)
    return sdk


def test_run_builds_options_and_maps_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(
        is_error=False, num_turns=3, result="done",
        usage={"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2,
               "cache_read_input_tokens": 1}))

    auth_env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", auth_env)
    result = asyncio.run(runner.run(IMPLEMENTER, "do the thing", tmp_path, max_turns=4))

    opts = captured["options"]
    assert opts.tools == list(IMPLEMENTER.tools)
    assert opts.allowed_tools == list(IMPLEMENTER.tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.max_turns == 4
    assert opts.env == agent_env(tmp_path / "cfg", auth_env)
    assert captured["prompt"] == "do the thing"

    assert result.text == "done"
    assert result.usage == Usage(turns=3, input_tokens=13, output_tokens=5)
    assert result.is_error is False
    assert result.denied == ()


def test_run_pre_tool_use_hook_denies_and_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)

    async def invoke_hooks(client: Any) -> None:
        hook = client.options.hooks["PreToolUse"][0].hooks[0]
        captured["deny_output"] = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "infra/x"}}, "tu1", None)
        captured["allow_output"] = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "src/a.ts"}}, "tu2", None)

    script.append(invoke_hooks)
    script.append(sdk.ResultMessage(is_error=False, num_turns=1, result="ok", usage={}))

    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    result = asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))

    assert captured["deny_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert captured["allow_output"] == {}
    assert any("protected path: infra/x" in d for d in result.denied)


def test_run_is_error_with_usage_limit_text_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(
        is_error=True, num_turns=2, result="Claude AI usage limit reached|123", usage={}))

    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))


def test_run_client_exception_with_429_raises_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = [RuntimeError("API Error: 429 rate_limit_error")]
    _install_fake_sdk(monkeypatch, captured, script)

    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))


def test_run_should_stop_interrupts_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.AssistantMessage([sdk.TextBlock("hi")]))

    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}, should_stop=lambda: True)
    with pytest.raises(AgentInterrupted):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
    assert captured["client"].interrupted is True


def test_run_without_result_message_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.AssistantMessage([sdk.TextBlock("hi")]))

    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(RuntimeError, match="agent session ended without a result"):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
