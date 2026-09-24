from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_sdlc.metrics import render_metrics
from agent_sdlc.store import Store
from agent_sdlc.types import Stage, WorkItem

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_metrics_report() -> None:
    store = Store("sqlite://")
    store.add_item("t", WorkItem(1, "A", "", "", "Bug", (), "u"), "agent/1-a")
    it = store.get(1)
    at = lambda stage: replace(it, stage=stage)  # noqa: E731
    store.add_event("intake", {"branch": "agent/1-a"}, item=it, ts=T0)
    store.add_event("agent_session", {"role": "planner", "input_tokens": 1000,
                                      "output_tokens": 200, "cache_read_tokens": 3000,
                                      "cost_usd": 0.5}, item=at(Stage.PLAN),
                    ts=T0 + timedelta(minutes=5))
    store.add_event("tool_denied", {"role": "implementer",
                                    "category": "command_not_allowlisted"},
                    item=at(Stage.IMPLEMENT), ts=T0 + timedelta(minutes=10))
    store.add_event("transition", {"from": "verify", "to": "implement"},
                    item=at(Stage.VERIFY), ts=T0 + timedelta(minutes=20))
    store.add_event("transition", {"from": "implement", "to": "parked",
                                   "park_reason": "policy"},
                    item=at(Stage.IMPLEMENT), ts=T0 + timedelta(minutes=30))
    store.add_event("requeue", {"from_reason": "policy", "to": "implement"},
                    item=at(Stage.PARKED), ts=T0 + timedelta(minutes=40))
    store.add_event("transition", {"from": "pr_open", "to": "awaiting_human"},
                    item=at(Stage.PR_OPEN), ts=T0 + timedelta(hours=2))
    store.add_event("outcome", {"result": "merged"}, item=at(Stage.AWAITING_HUMAN),
                    ts=T0 + timedelta(hours=5))
    store.add_event("intake", {}, item=it, ts=T0 - timedelta(days=3))  # outside the window
    out = render_metrics(store, "t", T0 - timedelta(days=1), T0 + timedelta(days=1))
    assert "taken in 1 · merged 1 · abandoned 0" in out and "merge rate 100%" in out
    assert "total 1 · requeued 1 (100%)" in out and "by reason: policy 1" in out
    assert "verify/review retries 1" in out
    assert "tokens per item: median 1,200" in out
    assert "cache-read share 75%" in out and "cost $0.50" in out
    assert "intake → PR open: median 2.0h" in out
    assert "PR open → merged: median 3.0h" in out
    assert "implementer command_not_allowlisted: 1" in out
    assert "triage.clarity: mode shadow · labels 0" in out


def test_metrics_empty_store() -> None:
    out = render_metrics(Store("sqlite://"), "t", T0, T0)
    assert "merge rate n/a" in out and "tokens per item: n/a" in out
