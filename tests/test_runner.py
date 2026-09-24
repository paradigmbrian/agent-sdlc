import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import agent_sdlc.agents.runner as runner_mod
from agent_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    implementer_prompt,
    reviewer_prompt,
)
from agent_sdlc.agents.runner import (
    ClaudeAgentRunner,
    agent_env,
    check_tool,
    evaluate_tool,
    parse_usage_limit,
)
from agent_sdlc.policy import CommandPolicy, PathPolicy
from agent_sdlc.types import (
    AgentInfraError,
    AgentInterrupted,
    AgentResult,
    CommandResult,
    Denial,
    Usage,
    UsageLimitError,
    WorkItem,
)

PP = PathPolicy(["infra/**", "**/.env*"])
CP = CommandPolicy(["npm test"])
WI = WorkItem(5, "Fix login", "Login broken", "Login works", "Bug", ("agent",), "u")


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


def test_check_tool_bash_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (root / "link").symlink_to(outside)

    reason = check_tool(IMPLEMENTER, root, PathPolicy([]), CommandPolicy(["cat"]), "Bash",
                        {"command": "cat link/secret.txt"})
    assert reason == "path is outside the worktree: link/secret.txt"


def test_check_tool_bash_allows_allowlisted_absolute_executable(tmp_path: Path) -> None:
    cp = CommandPolicy(["/usr/local/bin/eslint ."])
    assert check_tool(IMPLEMENTER, tmp_path, PP, cp, "Bash",
                      {"command": "/usr/local/bin/eslint ."}) is None


def test_check_tool_bash_denies_flag_equals_path(tmp_path: Path) -> None:
    cp = CommandPolicy(["npm run lint"])
    reason = check_tool(IMPLEMENTER, tmp_path, PP, cp, "Bash",
                        {"command": "npm run lint --config=/etc/x"})
    assert reason == "path is outside the worktree: --config=/etc/x"


def test_agent_env_isolates_config(tmp_path: Path) -> None:
    env = agent_env(tmp_path / "cfg", {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"


def test_agent_env_blanks_ado_pat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SDLC_ADO_PAT", "secret")
    env = agent_env(tmp_path, {})
    assert env["AGENT_SDLC_ADO_PAT"] == ""


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
                    usage: dict[str, Any] | None = None, result: str | None = None,
                    subtype: str = "success", api_error_status: int | None = None) -> None:
            self.is_error = is_error
            self.num_turns = num_turns
            self.usage = usage
            self.result = result
            self.subtype = subtype
            self.api_error_status = api_error_status

    class RateLimitInfo:
        def __init__(self, status: str, resets_at: int | None = None) -> None:
            self.status = status
            self.resets_at = resets_at

    class RateLimitEvent:
        def __init__(self, rate_limit_info: Any) -> None:
            self.rate_limit_info = rate_limit_info

    class ClaudeSDKError(Exception):
        pass

    class ResultError(ClaudeSDKError):
        def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
            data = data or {}
            self.subtype = data.get("subtype")
            self.api_error_status = data.get("api_error_status")
            super().__init__(message)

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
    sdk.RateLimitInfo = RateLimitInfo  # type: ignore[attr-defined]
    sdk.RateLimitEvent = RateLimitEvent  # type: ignore[attr-defined]
    sdk.ClaudeSDKError = ClaudeSDKError  # type: ignore[attr-defined]
    sdk.ResultError = ResultError  # type: ignore[attr-defined]

    sdk_types = types.ModuleType("claude_agent_sdk.types")
    sdk_types.HookContext = object  # type: ignore[attr-defined]
    sdk_types.HookInput = object  # type: ignore[attr-defined]
    sdk_types.SyncHookJSONOutput = dict  # type: ignore[attr-defined]
    sdk_types.SandboxSettings = dict  # type: ignore[attr-defined]

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
    assert opts.env == agent_env(tmp_path / "cfg", auth_env, tmp_path / "agent-home")
    assert captured["prompt"] == "do the thing"

    assert result.text == "done"
    # I4: cache reads are tracked separately and excluded from input_tokens.
    assert result.usage == Usage(turns=3, input_tokens=12, output_tokens=5, cache_read_tokens=1)
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
    with pytest.raises(AgentInfraError, match="agent session ended without a result"):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))


# --- final review fix wave ---------------------------------------------------------------


def test_c2_agent_env_neutralizes_all_inherited_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = agent_env(tmp_path / "cfg", {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, tmp_path / "home")
    assert env["SSH_AUTH_SOCK"] == "" and env["AWS_SECRET_ACCESS_KEY"] == ""
    assert env["GITHUB_TOKEN"] == ""
    assert "PATH" not in env  # allowlisted: inherited unchanged
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    import os
    leaked = {k for k in os.environ if k not in env} - {
        "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "TERM", "NVM_DIR", "NVM_BIN",
        "CLAUDE_CODE_ENTRYPOINT"}
    assert leaked == set()


def test_c2_run_enables_sdk_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(num_turns=1, result="ok", usage={}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}, home=tmp_path / "h")
    asyncio.run(runner.run(PLANNER, "p", tmp_path, max_turns=2))
    assert captured["options"].sandbox == {
        "enabled": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "network": {"allowedDomains": [], "allowUnixSockets": [], "allowAllUnixSockets": False,
                    "allowLocalBinding": False},
    }
    assert captured["options"].env["HOME"] == str(tmp_path / "h")
    assert (tmp_path / "h").is_dir()


@pytest.mark.parametrize("cmd", [
    "rg --pre sh -e x f", "rg --pre=sh x", "rg --pre-glob '*' --pre cat x",
    "tree -o .env", "tree -R", "tree -aR", "tree --fromfile list", "tree -ofoo",
])
def test_c2_rg_pre_and_tree_output_denied(tmp_path: Path, cmd: str) -> None:
    assert check_tool(PLANNER, tmp_path, PP, CP, "Bash", {"command": cmd}) is not None


@pytest.mark.parametrize("cmd", [
    "find . -fprint out", "find . -fprintf out x", "find . -fls out", "find . -fprint0 out",
])
def test_c2_find_file_output_actions_denied(tmp_path: Path, cmd: str) -> None:
    assert check_tool(PLANNER, tmp_path, PP, CP, "Bash", {"command": cmd}) is not None


def test_c2_plain_rg_and_tree_still_allowed(tmp_path: Path) -> None:
    for cmd in ("rg -n TODO src", "tree -a src", "tree -L 2"):
        assert check_tool(PLANNER, tmp_path, PP, CP, "Bash", {"command": cmd}) is None, cmd


def test_m3_glob_pattern_is_path_checked(tmp_path: Path) -> None:
    def chk(inp: dict[str, Any]) -> str | None:
        return check_tool(PLANNER, tmp_path, PP, CP, "Glob", inp)

    assert chk({"pattern": "/etc/*"}) == "path is outside the worktree"
    assert chk({"pattern": "../**/*.env"}) == "path is outside the worktree"
    assert chk({"pattern": "src/../../*"}) == "path is outside the worktree"
    assert chk({"pattern": "~/.ssh/*"}) == "path is outside the worktree"
    assert chk({"pattern": "**/*.py"}) is None
    assert chk({"pattern": "src/../*.py"}) is None


@pytest.mark.parametrize("text", ["There is a rate limiting bug", "limit reached in loop"])
def test_i2_is_error_without_429_is_not_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(is_error=True, num_turns=2, result=text,
                                    subtype="error_during_execution",
                                    usage={"input_tokens": 7, "output_tokens": 3}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    res = asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
    assert res.is_error and res.error == "error_during_execution"
    assert res.usage == Usage(2, 7, 3)


def test_i2_api_error_status_429_raises_with_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(is_error=True, num_turns=2, result="API Error",
                                    api_error_status=429,
                                    usage={"input_tokens": 7, "output_tokens": 3}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError) as ei:
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
    assert ei.value.usage == Usage(2, 7, 3) and ei.value.reset_at is None


def test_m11_rate_limit_event_reset_time_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.RateLimitEvent(sdk.RateLimitInfo("rejected", resets_at=1760000000)))
    script.append(sdk.ResultMessage(is_error=True, num_turns=1, result="API Error",
                                    api_error_status=429, usage={}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError) as ei:
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
    assert ei.value.reset_at == datetime.fromtimestamp(1760000000, UTC)


def test_m11_usage_limit_text_reset_time_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultMessage(is_error=True, num_turns=1,
                                    result="Claude AI usage limit reached|1760000000", usage={}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError) as ei:
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))
    assert ei.value.reset_at == datetime.fromtimestamp(1760000000, UTC)


def test_i1_sdk_errors_become_agent_infra_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ClaudeSDKError("CLI connection lost"))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(AgentInfraError, match="CLI connection lost"):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))


def test_i2_result_error_with_429_status_is_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script.append(sdk.ResultError("Claude Code returned an error result: API Error",
                                  {"api_error_status": 429}))
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    with pytest.raises(UsageLimitError):
        asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=2))


@pytest.mark.parametrize("text", ["rate limit", "limit reached", "Rate limiting bug in foo"])
def test_i2_parse_usage_limit_is_narrow(text: str) -> None:
    assert parse_usage_limit(text) is False


def test_evaluate_tool_returns_categorized_denial(tmp_path: Path) -> None:
    d = evaluate_tool(IMPLEMENTER, tmp_path, PP, CP, "Read", {"file_path": "/etc/hosts"})
    assert d == Denial("Read", "outside_worktree", "path is outside the worktree",
                       '{"file_path": "/etc/hosts"}')
    assert evaluate_tool(IMPLEMENTER, tmp_path, PP, CP, "Write", {"file_path": "src/a.ts"}) is None


def test_evaluate_tool_truncates_input(tmp_path: Path) -> None:
    d = evaluate_tool(IMPLEMENTER, tmp_path, PP, CP, "Bash", {"command": "curl " + "x" * 2000})
    assert d is not None and d.category == "command_not_allowlisted"
    assert len(d.input) == 500 and d.input.endswith("…")


def test_agent_result_denied_is_derived_from_denials() -> None:
    r = AgentResult("t", Usage(), (Denial("Read", "outside_worktree",
                                          "path is outside the worktree"),))
    assert r.denied == ("Read: path is outside the worktree",)


# --- Task 5: transcript, escalation, token budget, session metadata ----------------------


def _hook_call(tool: str, inp: dict[str, Any]) -> Any:
    async def call(client: Any) -> None:
        hook = client.options.hooks["PreToolUse"][0].hooks[0]
        await hook({"tool_name": tool, "tool_input": inp}, "tu", None)
    return call


def _assistant(sdk: Any, mid: str, inp: int, out: int) -> Any:
    msg = sdk.AssistantMessage([sdk.TextBlock("working")])
    msg.usage, msg.message_id = {"input_tokens": inp, "output_tokens": out}, mid
    return msg


def test_run_escalates_on_outside_worktree_and_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_hook_call("Read", {"file_path": "/etc/hosts"}),
               sdk.AssistantMessage([sdk.TextBlock("reading")]),
               sdk.ResultMessage(is_error=True, num_turns=2, subtype="error_during_execution",
                                 usage={"input_tokens": 5, "output_tokens": 1})]
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {})
    res = asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=5))
    assert captured["client"].interrupted is True
    assert res.escalated == "outside_worktree" and res.role == "implementer"
    assert res.denials[0].category == "outside_worktree" and res.usage.turns == 2


def test_run_escalates_at_denial_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_hook_call("Bash", {"command": "git push"}),
               _hook_call("Bash", {"command": "curl x"}),
               sdk.ResultMessage(num_turns=1, result="r", usage={})]
    runner = ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}, max_denials=2)
    res = asyncio.run(runner.run(IMPLEMENTER, "p", tmp_path, max_turns=5))
    assert res.escalated == "denial_threshold" and len(res.denials) == 2


def test_run_below_threshold_does_not_escalate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_hook_call("Bash", {"command": "git push"}),
               sdk.ResultMessage(num_turns=1, result="r", usage={})]
    res = asyncio.run(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
        IMPLEMENTER, "p", tmp_path, max_turns=5))
    assert res.escalated is None and captured["client"].interrupted is False


def test_run_token_budget_interrupts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_assistant(sdk, "m1", 600, 500),
               sdk.ResultMessage(num_turns=1, result="r",
                                 usage={"input_tokens": 600, "output_tokens": 500})]
    res = asyncio.run(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
        IMPLEMENTER, "p", tmp_path, max_turns=5, token_budget=1000))
    assert res.escalated == "budget" and captured["client"].interrupted is True


def test_run_escalation_without_result_estimates_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_hook_call("Read", {"file_path": "/etc/hosts"}), _assistant(sdk, "m1", 10, 2),
               _assistant(sdk, "m1", 10, 2)]  # same message id counted once
    res = asyncio.run(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
        IMPLEMENTER, "p", tmp_path, max_turns=5))
    assert res.escalated == "outside_worktree" and res.usage_estimated is True
    assert res.usage == Usage(1, 10, 2)


def test_run_escalation_grace_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    monkeypatch.setattr(runner_mod, "_INTERRUPT_GRACE_S", 0.05)

    async def hang(client: Any) -> None:
        await asyncio.sleep(5)

    script += [_hook_call("Read", {"file_path": "/etc/hosts"}),
               sdk.AssistantMessage([sdk.TextBlock("x")]), hang]
    res = asyncio.run(asyncio.wait_for(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
        IMPLEMENTER, "p", tmp_path, max_turns=5), timeout=2))
    assert res.escalated == "outside_worktree" and res.usage_estimated is True


def test_run_writes_transcript_and_session_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    result = sdk.ResultMessage(num_turns=1, result="done", usage={})
    result.session_id, result.duration_ms, result.total_cost_usd = "s-1", 1500, 0.02
    script += [sdk.AssistantMessage([sdk.TextBlock("hi")]),
               _hook_call("Bash", {"command": "git push"}), result]
    trace = tmp_path / "traces" / "t.jsonl"
    res = asyncio.run(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
        PLANNER, "plan it", tmp_path, max_turns=5, trace=trace))
    types_ = [json.loads(line)["type"] for line in trace.read_text().splitlines()]
    assert types_ == ["prompt", "assistant_text", "denied", "result"]
    assert (res.trace, res.session_id, res.duration_ms, res.cost_usd) == (
        str(trace), "s-1", 1500, 0.02)


def test_run_infra_error_carries_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script: list[Any] = []
    sdk = _install_fake_sdk(monkeypatch, captured, script)
    script += [_hook_call("Bash", {"command": "git push"}), sdk.ClaudeSDKError("boom")]
    with pytest.raises(AgentInfraError) as ei:
        asyncio.run(ClaudeAgentRunner(PP, CP, tmp_path / "cfg", {}).run(
            IMPLEMENTER, "p", tmp_path, max_turns=5))
    partial = ei.value.partial
    assert partial is not None and partial.role == "implementer" and len(partial.denials) == 1
