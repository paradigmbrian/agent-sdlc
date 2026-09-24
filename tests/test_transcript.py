import json
import os
from pathlib import Path

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_sdlc.agents.transcript import TOOL_RESULT_CHARS, TranscriptWriter
from agent_sdlc.types import Denial


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writer_records_session(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "5" / "t.jsonl"
    w = TranscriptWriter(path)
    w.prompt("implementer", "do it")
    w.message(AssistantMessage(content=[TextBlock("looking"),
                                        ToolUseBlock("tu1", "Read", {"file_path": "a.ts"})],
                               model="m"))
    w.message(UserMessage(content=[ToolResultBlock("tu1", "x" * (TOOL_RESULT_CHARS + 5))]))
    w.denied(Denial("Read", "outside_worktree", "path is outside the worktree"))
    w.message(ResultMessage(subtype="success", duration_ms=1200, duration_api_ms=900,
                            is_error=False, num_turns=2, session_id="s1",
                            total_cost_usd=0.01, usage={"input_tokens": 3}))
    w.close()
    lines = _lines(path)
    assert [x["type"] for x in lines] == [
        "prompt", "assistant_text", "tool_use", "tool_result", "denied", "result"]
    assert lines[2]["name"] == "Read" and lines[2]["input"] == {"file_path": "a.ts"}
    assert str(lines[3]["content"]).endswith("…(+5 chars)")
    assert lines[5]["session_id"] == "s1" and all("ts" in x for x in lines)
    assert w.path == str(path) and w.error is None
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "traces").stat().st_mode & 0o777) == "0o700"


def test_writer_without_path_is_noop() -> None:
    w = TranscriptWriter(None)
    w.prompt("planner", "x")
    w.close()
    assert w.path is None and w.error is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_writer_unwritable_dir_is_noop(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    w = TranscriptWriter(locked / "sub" / "t.jsonl")
    w.prompt("planner", "x")  # must not raise
    w.close()
    assert w.path is None and w.error is not None and "PermissionError" in w.error
