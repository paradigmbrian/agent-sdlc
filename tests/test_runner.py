from pathlib import Path

import pytest

from laya_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    implementer_prompt,
    reviewer_prompt,
)
from laya_sdlc.agents.runner import agent_env, check_tool, parse_usage_limit
from laya_sdlc.policy import CommandPolicy, PathPolicy
from laya_sdlc.types import CommandResult, WorkItem

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
