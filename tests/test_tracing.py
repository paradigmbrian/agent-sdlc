import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_sdlc.store import Store
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import Stage, WorkItem

WI = WorkItem(9, "Fix it", "", "", "Bug", (), "u")


@pytest.fixture
def store() -> Store:
    s = Store("sqlite://")
    s.add_item("rallysource", WI, "agent/9-fix-it")
    return s


def test_trace_timeline(store: Store) -> None:
    it = store.get_by_ref("rallysource", 9)
    store.add_event("intake", {"title": "Fix it", "branch": "agent/9-fix-it"}, item=it)
    impl = replace(it, stage=Stage.IMPLEMENT)
    store.add_event("tool_denied", {"role": "implementer", "tool": "Read",
                                    "category": "outside_worktree",
                                    "reason": "path is outside the worktree"}, item=impl)
    store.add_event("agent_session", {"role": "implementer", "turns": 31, "input_tokens": 1000,
                                      "output_tokens": 200, "duration_ms": 65000, "denials": 1,
                                      "escalated": "outside_worktree"}, item=impl)
    store.add_event("transition", {"from": "implement", "to": "parked",
                                   "park_reason": "policy", "note": "stopped\nmore"}, item=impl)
    out = render_trace(store, it.id)
    assert out.splitlines()[0].startswith('rallysource#9 "Fix it"   stage: triage')
    assert "branch agent/9-fix-it" in out
    assert "DENIED     implementer Read [outside_worktree]" in out
    assert "31 turns 1,200 tok 1m05s 1 denied ESCALATED outside_worktree" in out
    assert "→ parked (policy): stopped" in out


def test_trace_item_without_events(store: Store) -> None:
    assert "(no events recorded)" in render_trace(store, store.get_by_ref("rallysource", 9).id)


def test_trace_unknown_item(store: Store) -> None:
    with pytest.raises(KeyError):
        render_trace(store, 404)


def test_trace_full_prints_tool_calls(store: Store, tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "tool_use", "name": "Read",
                             "input": {"file_path": "src/a.ts"}}) + "\n")
    item = store.get_by_ref("rallysource", 9)
    store.add_event("agent_session", {"role": "planner", "transcript": str(t)}, item=item)
    out = render_trace(store, item.id, full=True)
    assert 'Read {"file_path": "src/a.ts"}' in out
    t.unlink()
    assert "(transcript missing)" in render_trace(store, item.id, full=True)
