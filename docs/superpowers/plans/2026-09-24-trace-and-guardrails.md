# Tracing, Guardrails and Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every work item's history reconstructable (events, agent transcripts, full check logs), block agents from steering code into host-side verify, escalate policy probing immediately, and report pipeline metrics.

**Architecture:** A new append-only `events` table, written by the Scheduler in the same transaction as each step. Large artifacts (JSONL agent transcripts, full command output) go to `~/.agent-sdlc/traces/<item>/`, and the DB stores their paths. The runner classifies denials, interrupts sessions on escalation or budget overrun, and returns session metadata. The StageExecutor adds a manifest gate and installs according to a digest of the manifests. New CLI commands `trace` and `metrics`, plus additions to `status`, read the events.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (SQLite), Pydantic v2, claude-agent-sdk 0.2.158, pytest (asyncio_mode=auto), ruff, mypy --strict, uv.

**Spec:** `docs/superpowers/specs/2026-09-24-trace-and-guardrails-design.md` (builds on `docs/superpowers/specs/2026-09-23-agent-sdlc-design.md`).

## Global Constraints

- Python `>=3.12`; `uv run pytest`, `uv run ruff check .` and `uv run mypy` (strict) pass at the end of every task.
- Ruff line length 100; lint rules `E,F,I,B,UP`.
- No new runtime dependencies. SDK message types are those of the installed `claude-agent-sdk 0.2.158` (`ToolUseBlock`, `ToolResultBlock`, `UserMessage`, `AssistantMessage`, `ResultMessage`).
- Before editing code that uses SQLAlchemy, Pydantic or claude-agent-sdk, look up current docs for the installed version with Context7 (user rule); the installed package source is authoritative when they disagree.
- Trace and log directories are created `0700`; transcript and command-log files `0600`.
- The Scheduler is the only writer of item state and item events. The CLI writes only `pause`/`resume` events and the `requeue` event for `requeue --local`.
- Existing tests change only in two places: `tests/fakes.py` (`FakeRunner.run` gains `trace`/`token_budget` kwargs and records them) and `tests/test_stages.py:58` (`installed` → `installed_digest`, because the boolean flag is replaced). No assertion is weakened. The e2e `Env` helper gains an optional `traces` kwarg (additive).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.

### Spec deviations (deliberate, recorded here)

1. `AgentResult.denied` stays as a derived read-only property over the new `denials`, so existing tests and the slow test keep working unchanged.
2. Instead of `manifest_digest(wt)`/`install_digest(wt)`, Workspaces exposes the primitives `blob_digest(wt, paths)` and `tracked_files(wt)`. Manifest pattern matching lives in StageExecutor (`self._mp`), so Workspaces stays pattern-free.
3. If a transcript write fails after the file was created, the event keeps the partial file's path and adds `trace_error`. `trace` is null only when the file could not be created.
4. Escalation because of the per-session denial threshold is reported as `escalated="denial_threshold"`.
5. No `transition` event is written for an `awaiting_human → awaiting_human` poll (otherwise one event per minute per open PR).
6. Command output is written to its log file when the command finishes, not streamed.

## Review Focus

1. **A stalled SDK stream after an escalation interrupt** (no `ResultMessage` ever arrives): the session must end within the grace period, with estimated usage. Test: Task 5 `test_run_escalation_grace_timeout`.
2. **Trace or log directory not writable:** the stage must still complete, with `trace_error`/`log=None` recorded instead of a crash. Tests: Task 4 `test_writer_unwritable_dir_is_noop`, Task 6 `test_run_log_unwritable_dir`.
3. **A manifest file deleted on the branch** must still be gated, not treated as "no change". Test: Task 6 `test_blob_digest_tracks_content_and_deletion`.
4. **A `state.db` from before this wave** (items with `data["installed"] = True`, no events): `trace` and `status` must not crash, and install reruns once. Tests: Task 10 `test_trace_item_without_events`, Task 8 `test_legacy_installed_flag_reinstalls_once`.
5. **An open PR polled every minute with no activity** must not grow the events table. Test: Task 9 `test_awaiting_noop_poll_writes_no_events`.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/agent_sdlc/types.py` | modify | `Denial`, `EventInput`, `ParkReason.MANIFEST`; `AgentResult` session fields; `CommandResult.log`; exceptions carry `partial` |
| `src/agent_sdlc/policy.py` | modify | `categorize(reason)` → denial category |
| `src/agent_sdlc/store.py` | modify | `events` table and accessors; `commit_step`/`save` take events; label/decision queries for metrics and labeling |
| `src/agent_sdlc/logctx.py` | create | item/stage logging context, file + stderr configuration |
| `src/agent_sdlc/agents/transcript.py` | create | `TranscriptWriter` (JSONL, never raises) |
| `src/agent_sdlc/agents/runner.py` | modify | `evaluate_tool`, transcript, escalation, token budget, session metadata, partial results |
| `src/agent_sdlc/targets.py`, `targets/rallysource.yaml` | modify | `manifest_paths`, `max_denials_per_session`, `stale_after_minutes`, protected tooling globs |
| `src/agent_sdlc/workspaces.py`, `src/agent_sdlc/ports.py` | modify | command logs, `blob_digest`, `tracked_files`, `diff(paths=)` |
| `src/agent_sdlc/orchestrator/transitions.py` | modify | `manifest` requeue → verify |
| `src/agent_sdlc/orchestrator/reporting.py` | modify | manifest/denial park comments, PR "Blocked tool calls" |
| `src/agent_sdlc/orchestrator/events.py` | create | build `agent_session`/`tool_denied`/`check`/`transition` events |
| `src/agent_sdlc/orchestrator/stages.py` | modify | events, trace paths, escalation, manifest gate, install-by-digest |
| `src/agent_sdlc/orchestrator/scheduler.py` | modify | event writes, log context, `last_tick`, stale warnings, outcomes |
| `src/agent_sdlc/tracing.py` | create | `render_trace` |
| `src/agent_sdlc/metrics.py` | create | `render_metrics` |
| `src/agent_sdlc/labeling.py` | modify | `label_logged(..., abandoned_only=)` |
| `src/agent_sdlc/cli.py` | modify | logging, `--traces/--logs`, `trace`, `metrics`, `label --abandoned`, status additions, CLI events |
| `tests/…` | modify/create | per task |
| `README.md` | modify | operator docs |

---

### Task 1: Denial model and categories

**Files:**
- Modify: `src/agent_sdlc/types.py` (`CommandResult`, `AgentResult`, exceptions, `ParkReason`, new `Denial`, `EventInput`)
- Modify: `src/agent_sdlc/policy.py` (add `categorize`)
- Modify: `src/agent_sdlc/agents/runner.py` (add `evaluate_tool`; hook stores `Denial`s)
- Test: `tests/test_policy.py`, `tests/test_runner.py`

**Interfaces:**
- Produces: `Denial(tool: str, category: str, reason: str, input: str = "")`; `EventInput(kind: str, payload: dict[str, Any] = {})`; `ParkReason.MANIFEST`; `ESCALATE_CATEGORIES: frozenset[str]`; `AgentResult(text, usage, denials=(), is_error=False, error="", escalated=None, session_id="", duration_ms=0, cost_usd=None, trace=None, trace_error=None, role="", usage_estimated=False)` with property `denied -> tuple[str, ...]`; `CommandResult(..., log: str | None = None)`; `UsageLimitError(message, reset_at=None, usage=None, partial=None)`; `AgentInfraError(message, partial=None)`; `policy.categorize(reason: str) -> str`; `runner.evaluate_tool(role, cwd, path_policy, command_policy, tool_name, tool_input) -> Denial | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_policy.py`:

```python
from agent_sdlc.policy import categorize


@pytest.mark.parametrize("reason,category", [
    ("tool Write is not permitted for planner", "tool_not_permitted"),
    ("protected path: infra/x.bicep", "protected_path"),
    ("path is outside the worktree", "outside_worktree"),
    ("path is outside the worktree: /etc/hosts", "outside_worktree"),
    ("command not allowlisted: git", "command_not_allowlisted"),
    ("empty command", "command_not_allowlisted"),
    ("shell operators, substitutions and redirects are not allowed", "shell_syntax"),
    ("command could not be parsed", "shell_syntax"),
    ("find with side-effect actions is not allowed", "side_effect_flag"),
    ("rg --pre/--pre-glob is not allowed", "side_effect_flag"),
    ("tree -o/-R/--fromfile is not allowed", "side_effect_flag"),
    ("git --output is not allowed", "side_effect_flag"),
    ("policy check failed: ValueError: embedded null byte", "policy_error"),
])
def test_categorize(reason: str, category: str) -> None:
    assert categorize(reason) == category
```

(`pytest` is already imported in `tests/test_policy.py`; if not, add `import pytest`.)

Append to `tests/test_runner.py`:

```python
from agent_sdlc.agents.runner import evaluate_tool
from agent_sdlc.types import AgentResult, Denial


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py::test_categorize tests/test_runner.py -k "categorize or evaluate_tool or derived" -v`
Expected: FAIL with `ImportError: cannot import name 'categorize'`.

- [ ] **Step 3: Implement**

In `src/agent_sdlc/types.py`, add `MANIFEST` to `ParkReason` (after `AGENT_ERROR`):

```python
    MANIFEST = "manifest"  # dependency manifests changed; a human approves them before install
```

Replace `CommandResult` with:

```python
@dataclass(frozen=True)
class CommandResult:
    name: str
    command: str
    exit_code: int
    output: str
    duration_s: float
    log: str | None = None         # full output file, when one was written

    @property
    def ok(self) -> bool:
        return self.exit_code == 0
```

Replace `AgentResult` and the three exception classes with:

```python
# Denial categories that stop an agent session immediately (spec §5.3).
ESCALATE_CATEGORIES = frozenset({"outside_worktree", "protected_path"})


@dataclass(frozen=True)
class Denial:
    tool: str
    category: str   # tool_not_permitted | protected_path | outside_worktree |
                    # command_not_allowlisted | shell_syntax | side_effect_flag | policy_error
    reason: str
    input: str = ""  # compact JSON of the tool input, at most 500 chars


@dataclass(frozen=True)
class EventInput:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    text: str
    usage: Usage
    denials: tuple[Denial, ...] = ()
    is_error: bool = False
    error: str = ""                # result subtype when is_error (e.g. "error_max_turns")
    escalated: str | None = None   # a denial category, "denial_threshold" or "budget"
    session_id: str = ""
    duration_ms: int = 0
    cost_usd: float | None = None
    trace: str | None = None       # JSONL transcript path
    trace_error: str | None = None
    role: str = ""
    usage_estimated: bool = False  # no ResultMessage: usage summed from assistant messages

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(f"{d.tool}: {d.reason}" for d in self.denials)


class UsageLimitError(Exception):
    """The model provider refused work because a usage/rate limit window is exhausted."""

    def __init__(self, message: str, reset_at: datetime | None = None,
                 usage: Usage | None = None, partial: AgentResult | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.usage = usage or Usage()  # spent before the limit hit; still counts
        self.partial = partial         # what the session did before it failed, for events


class AgentInterrupted(Exception):
    """The kill switch interrupted an agent session; the stage should be retried later."""


class AgentInfraError(Exception):
    """The agent SDK/CLI failed (connection, process or missing result); retried with backoff."""

    def __init__(self, message: str, partial: AgentResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial
```

In `src/agent_sdlc/policy.py`, add after `_OUTSIDE`:

```python
def categorize(reason: str) -> str:
    """Map a denial reason produced by this module or the runner to its category."""
    r = reason.lower()
    if r.startswith("tool ") and "is not permitted" in r:
        return "tool_not_permitted"
    if r.startswith("protected path"):
        return "protected_path"
    if r.startswith(_OUTSIDE):
        return "outside_worktree"
    if r.startswith("command not allowlisted") or r == "empty command":
        return "command_not_allowlisted"
    if r.startswith(("shell operators", "command could not be parsed")):
        return "shell_syntax"
    if r.startswith(("find with", "rg --pre", "tree -o", "git --output")):
        return "side_effect_flag"
    return "policy_error"
```

In `src/agent_sdlc/agents/runner.py`: add `import json` to the imports; change the policy import to `from agent_sdlc.policy import CommandPolicy, PathPolicy, categorize`; change the types import to include `Denial`. Add after `check_tool`:

```python
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
```

In `ClaudeAgentRunner.run`, replace `denied: list[str] = []` with `denials: list[Denial] = []` and the hook body with:

```python
            data = cast(dict[str, Any], input_data)
            tool = str(data.get("tool_name", ""))
            denial = evaluate_tool(role, cwd, self._pp, self._cp, tool,
                                   data.get("tool_input") or {})
            if denial is None:
                return {}
            denials.append(denial)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by agent-sdlc policy: {denial.reason}"}}
```

and the final return with `return AgentResult(text, usage, tuple(denials), bool(result.is_error), error)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass (the existing `result.denied` assertions pass through the derived property).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/types.py src/agent_sdlc/policy.py src/agent_sdlc/agents/runner.py tests/test_policy.py tests/test_runner.py
git commit -m "feat: categorized tool denials and session fields on AgentResult" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Event log in the store

**Files:**
- Modify: `src/agent_sdlc/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `EventInput`, `Item` (Task 1).
- Produces: `Event(id, item_id, ts, kind, stage, attempt, payload)` dataclass in `agent_sdlc.store`; `Store.add_event(kind: str, payload: dict[str, Any] | None = None, *, item: Item | None = None, ts: datetime | None = None) -> None`; `Store.save(item, events: Sequence[EventInput] = (), at: Item | None = None)`; `Store.commit_step(item, decisions, usage, day, labels, events: Sequence[EventInput] = (), at: Item | None = None)`; `Store.events_for(item_id) -> list[Event]`; `Store.events_since(since: datetime, kinds: Iterable[str] | None = None) -> list[Event]`; `Store.last_event_ts(item_id) -> datetime | None`; `Store.decisions_with_ts(item_id) -> list[tuple[datetime, Decision]]`; `Store.decision_states(item_id, gate) -> list[dict[str, Any]]`; `Store.labeled_decisions(gate, question) -> list[tuple[str, str]]` (answer, gold); `Store.abandoned_item_ids() -> set[int]`; `Store.unlabeled_decisions_for_items(gate, item_ids: set[int], limit) -> list[tuple[int, int, Decision, dict[str, Any]]]` (decision_id, item_id, decision, state). All returned `ts` values are timezone-aware UTC. Event `stage`/`attempt` come from `at` (the item as it was before the step), defaulting to `item`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
from datetime import UTC, datetime, timedelta

from agent_sdlc.types import EventInput
from tests.fakes import decision


def test_events_round_trip_order_and_context(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get(WI.id)
    store.add_event("intake", {"branch": "b"}, item=it)
    store.add_event("pause")
    new = replace(it, stage=Stage.PLAN)
    store.commit_step(new, [], Usage(), date(2026, 10, 1), [],
                      events=[EventInput("transition", {"from": "triage", "to": "plan"})], at=it)
    evs = store.events_for(WI.id)
    assert [e.kind for e in evs] == ["intake", "transition"]
    assert evs[1].stage == "triage" and evs[1].attempt == 0 and evs[1].payload["to"] == "plan"
    assert evs[1].ts.tzinfo is not None
    assert store.get(WI.id).stage is Stage.PLAN
    start = evs[0].ts - timedelta(seconds=1)
    assert [e.kind for e in store.events_since(start)] == ["intake", "pause", "transition"]
    assert [e.kind for e in store.events_since(start, kinds=["pause"])] == ["pause"]
    assert store.last_event_ts(WI.id) == evs[1].ts


def test_events_since_excludes_older_and_save_writes_events(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get(WI.id)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store.add_event("intake", {}, item=it, ts=old)
    store.save(replace(it, attempt=1), events=[EventInput("infra_failure", {"n": 1})], at=it)
    assert [e.kind for e in store.events_since(old + timedelta(days=1))] == ["infra_failure"]
    assert store.events_for(WI.id)[0].ts == old
    assert store.get(WI.id).attempt == 1
    assert store.last_event_ts(999) is None


def test_label_queries_for_metrics_and_abandoned(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get(WI.id)
    d1 = decision("review", "review_blocking", "no")
    d2 = decision("review", "risk", "low")
    store.commit_step(it, [(d1, {"review_notes": "n"}), (d2, {"review_notes": "n"})],
                      Usage(), date(2026, 10, 1), [])
    [(first_id, _, _)] = store.unlabeled_decisions("review", 1)
    store.add_label(LabelInput("review", "review_blocking", d1.raw_probs, "false", "manual",
                               first_id))
    assert store.labeled_decisions("review", "review_blocking") == [("no", "false")]
    store.add_event("outcome", {"result": "abandoned"}, item=it)
    assert store.abandoned_item_ids() == {WI.id}
    rows = store.unlabeled_decisions_for_items("review", {WI.id}, 10)
    assert [(item_id, d.question) for _, item_id, d, _ in rows] == [(WI.id, "risk")]
    assert store.unlabeled_decisions_for_items("review", set(), 10) == []
    assert store.decision_states(WI.id, "review")[0] == {"review_notes": "n"}
    assert [d.question for _, d in store.decisions_with_ts(WI.id)] == [
        "review_blocking", "risk"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'events'` / `AttributeError: 'Store' object has no attribute 'add_event'`.

- [ ] **Step 3: Implement**

In `src/agent_sdlc/store.py`: change imports to

```python
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import JSON, ForeignKey, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from agent_sdlc.types import (
    Calibration,
    Decision,
    EventInput,
    Item,
    ParkReason,
    Stage,
    Usage,
    WorkItem,
)
```

Add after `_now`:

```python
def _db_ts(ts: datetime | None) -> datetime:
    """Stored as naive UTC (SQLite keeps no tzinfo)."""
    return (ts or _now()).astimezone(UTC).replace(tzinfo=None)


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
```

Add after `DailyUsageRow`:

```python
class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    stage: Mapped[str | None]
    attempt: Mapped[int | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


@dataclass(frozen=True)
class Event:
    id: int
    item_id: int | None
    ts: datetime          # aware UTC
    kind: str
    stage: str | None
    attempt: int | None
    payload: dict[str, Any]


def _to_event(r: EventRow) -> Event:
    return Event(r.id, r.item_id, _aware(r.ts), r.kind, r.stage, r.attempt, dict(r.payload or {}))


def _event_row(ev: EventInput, at: Item | None, ts: datetime | None = None) -> EventRow:
    return EventRow(item_id=at.id if at else None, ts=_db_ts(ts), kind=ev.kind,
                    stage=at.stage.value if at else None, attempt=at.attempt if at else None,
                    payload=dict(ev.payload))
```

Replace `save` and `commit_step` with:

```python
    def save(self, item: Item, events: Sequence[EventInput] = (), at: Item | None = None) -> None:
        with self._session() as s, s.begin():
            self._write_item(s, item)
            for ev in events:
                s.add(_event_row(ev, at or item))

    def commit_step(self, item: Item, decisions: list[tuple[Decision, dict[str, Any]]],
                    usage: Usage, day: date, labels: list[LabelInput],
                    events: Sequence[EventInput] = (), at: Item | None = None) -> None:
        """One step, one transaction: item state, decisions, labels, usage and the events
        describing the step. `at` is the item as it was when the step ran (event context)."""
        with self._session() as s, s.begin():
            self._write_item(s, item)
            for d, state in decisions:
                s.add(DecisionRow(item_id=item.id, gate=d.gate, question=d.question,
                                  answer=d.answer, probs=d.probs, raw_probs=d.raw_probs,
                                  confidence=d.confidence, shadow=d.shadow,
                                  actionable=d.actionable, state=state))
            for lab in labels:
                s.add(self._label_row(lab))
            for ev in events:
                s.add(_event_row(ev, at or item))
            self._add_usage(s, day, usage)
```

Add these methods to `Store` (after `labels`):

```python
    def labeled_decisions(self, gate: str, question: str) -> list[tuple[str, str]]:
        """(logged answer, human gold) for every label tied to a logged decision."""
        with self._session() as s:
            q = (select(DecisionRow.answer, LabelRow.gold)
                 .join(LabelRow, LabelRow.decision_id == DecisionRow.id)
                 .where(DecisionRow.gate == gate, DecisionRow.question == question)
                 .order_by(LabelRow.id))
            return [(a, g) for a, g in s.execute(q)]

    def unlabeled_decisions_for_items(
        self, gate: str, item_ids: set[int], limit: int
    ) -> list[tuple[int, int, Decision, dict[str, Any]]]:
        if not item_ids:
            return []
        with self._session() as s:
            labeled = select(LabelRow.decision_id).where(
                LabelRow.decision_id.is_not(None)).scalar_subquery()
            q = (select(DecisionRow).where(DecisionRow.gate == gate)
                 .where(DecisionRow.item_id.in_(item_ids))
                 .where(DecisionRow.id.not_in(labeled))
                 .order_by(DecisionRow.item_id, DecisionRow.id).limit(limit))
            return [(r.id, r.item_id, _to_decision(r), dict(r.state)) for r in s.scalars(q)]

    def decision_states(self, item_id: int, gate: str) -> list[dict[str, Any]]:
        with self._session() as s:
            q = (select(DecisionRow).where(DecisionRow.item_id == item_id,
                                           DecisionRow.gate == gate).order_by(DecisionRow.id))
            return [dict(r.state) for r in s.scalars(q)]

    def decisions_with_ts(self, item_id: int) -> list[tuple[datetime, Decision]]:
        with self._session() as s:
            q = select(DecisionRow).where(DecisionRow.item_id == item_id).order_by(DecisionRow.id)
            return [(_aware(r.created_at), _to_decision(r)) for r in s.scalars(q)]

    # events ----------------------------------------------------------------
    def add_event(self, kind: str, payload: dict[str, Any] | None = None, *,
                  item: Item | None = None, ts: datetime | None = None) -> None:
        with self._session() as s, s.begin():
            s.add(_event_row(EventInput(kind, payload or {}), item, ts))

    def events_for(self, item_id: int) -> list[Event]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.item_id == item_id).order_by(EventRow.id)
            return [_to_event(r) for r in s.scalars(q)]

    def events_since(self, since: datetime, kinds: Iterable[str] | None = None) -> list[Event]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.ts >= _db_ts(since))
            if kinds is not None:
                q = q.where(EventRow.kind.in_(list(kinds)))
            return [_to_event(r) for r in s.scalars(q.order_by(EventRow.id))]

    def last_event_ts(self, item_id: int) -> datetime | None:
        with self._session() as s:
            q = (select(EventRow.ts).where(EventRow.item_id == item_id)
                 .order_by(EventRow.id.desc()).limit(1))
            ts = s.scalars(q).first()
            return _aware(ts) if ts is not None else None

    def abandoned_item_ids(self) -> set[int]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.kind == "outcome")
            return {r.item_id for r in s.scalars(q)
                    if r.item_id is not None and (r.payload or {}).get("result") == "abandoned"}
```

`_now` returns aware UTC, and `DecisionRow.created_at` comes back naive from SQLite, which is why `_aware` is applied in `decisions_with_ts`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/store.py tests/test_store.py
git commit -m "feat: append-only events table written with each step" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Logging with item and stage context

**Files:**
- Create: `src/agent_sdlc/logctx.py`
- Test: `tests/test_logctx.py`

**Interfaces:**
- Produces: `log_context(item_id: int, stage: str)` context manager; `ContextFilter`; `configure_logging(log_dir: Path | None) -> None` (idempotent; stderr INFO, `log_dir/agent-sdlc.log` DEBUG, rotating daily, 14 backups); `FORMAT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_logctx.py`:

```python
import logging
from pathlib import Path

from agent_sdlc.logctx import ContextFilter, configure_logging, log_context


def test_context_filter_adds_item_and_stage() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    f = ContextFilter()
    f.filter(rec)
    assert (getattr(rec, "item"), getattr(rec, "stage")) == ("-", "-")
    with log_context(7, "plan"):
        f.filter(rec)
        assert (getattr(rec, "item"), getattr(rec, "stage")) == ("7", "plan")
    f.filter(rec)
    assert getattr(rec, "item") == "-"


def test_configure_logging_writes_file_with_context(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    with log_context(4821, "implement"):
        logging.getLogger("agent_sdlc.test").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "agent-sdlc.log").read_text()
    assert "[#4821 implement] agent_sdlc.test: hello" in text


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_agent_sdlc", False)]
    assert len(ours) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_logctx.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_sdlc.logctx'`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/logctx.py`:

```python
from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s [#%(item)s %(stage)s] %(name)s: %(message)s"
_item: ContextVar[str] = ContextVar("agent_sdlc_item", default="-")
_stage: ContextVar[str] = ContextVar("agent_sdlc_stage", default="-")
_NOISY = ("httpx", "httpcore", "urllib3", "asyncio")


@contextmanager
def log_context(item_id: int, stage: str) -> Iterator[None]:
    """Tag every log record emitted inside the block with the item and stage."""
    t_item, t_stage = _item.set(str(item_id)), _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(t_stage)
        _item.reset(t_item)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        setattr(record, "item", _item.get())
        setattr(record, "stage", _stage.get())
        return True


def configure_logging(log_dir: Path | None) -> None:
    """stderr at INFO plus `log_dir/agent-sdlc.log` at DEBUG (daily rotation, 14 kept).
    Safe to call repeatedly: replaces the handlers it added before."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_agent_sdlc", False)]:
        root.removeHandler(h)
        h.close()
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    handlers.append(console)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fh = TimedRotatingFileHandler(log_dir / "agent-sdlc.log", when="midnight",
                                          backupCount=14, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            handlers.append(fh)
        except OSError as e:
            sys.stderr.write(f"agent-sdlc: file logging disabled ({log_dir}): {e}\n")
    fmt = logging.Formatter(FORMAT)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(ContextFilter())
        setattr(h, "_agent_sdlc", True)
        root.addHandler(h)
    root.setLevel(logging.DEBUG)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/logctx.py tests/test_logctx.py
git commit -m "feat: log file and item/stage context on every record" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Transcript writer

**Files:**
- Create: `src/agent_sdlc/agents/transcript.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Consumes: `Denial` (Task 1).
- Produces: `TranscriptWriter(path: Path | None)` with `.path: str | None`, `.error: str | None`, `.prompt(role, text)`, `.denied(denial)`, `.message(msg: object)`, `.close()`. Never raises. Dispatches on the SDK class name (`AssistantMessage`, `UserMessage`, `RateLimitEvent`, `ResultMessage`; blocks `TextBlock`, `ToolUseBlock`, `ToolResultBlock`), so it works with the real SDK and the test fakes. Line types: `prompt`, `assistant_text`, `tool_use`, `tool_result`, `denied`, `rate_limit`, `result`; each line has `ts`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_sdlc.agents.transcript'`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/agents/transcript.py`:

```python
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from agent_sdlc.types import Denial

log = logging.getLogger(__name__)
TOOL_RESULT_CHARS = 20_000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} chars)"


class TranscriptWriter:
    """One JSON object per line for an agent session, flushed per line so a crash keeps the
    partial transcript. Never raises: the first I/O error is logged once and recorded in
    `error`, and writing stops (spec §9)."""

    def __init__(self, path: Path | None) -> None:
        self.path: str | None = None
        self.error: str | None = None
        self._f: IO[str] | None = None
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._f = os.fdopen(fd, "a", encoding="utf-8")
            self.path = str(path)
        except OSError as e:
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        self.error = f"{type(e).__name__}: {e}"
        log.warning("transcript writing stopped: %s", self.error)
        self.close()

    def _write(self, obj: dict[str, Any]) -> None:
        if self._f is None:
            return
        try:
            line = json.dumps({"ts": datetime.now(UTC).isoformat(), **obj}, default=str,
                              ensure_ascii=False)
            self._f.write(line + "\n")
            self._f.flush()
        except (OSError, TypeError, ValueError) as e:
            self._fail(e)

    def prompt(self, role: str, text: str) -> None:
        self._write({"type": "prompt", "role": role, "text": text})

    def denied(self, d: Denial) -> None:
        self._write({"type": "denied", "tool": d.tool, "category": d.category,
                     "reason": d.reason})

    def message(self, msg: object) -> None:
        kind = type(msg).__name__
        if kind == "AssistantMessage":
            for block in getattr(msg, "content", None) or []:
                bkind = type(block).__name__
                if bkind == "TextBlock":
                    self._write({"type": "assistant_text", "text": block.text})
                elif bkind == "ToolUseBlock":
                    self._write({"type": "tool_use", "id": block.id, "name": block.name,
                                 "input": block.input})
        elif kind == "UserMessage":
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if type(block).__name__ != "ToolResultBlock":
                        continue
                    body = block.content
                    text = "" if body is None else body if isinstance(body, str) else \
                        json.dumps(body, default=str, ensure_ascii=False)
                    self._write({"type": "tool_result", "tool_use_id": block.tool_use_id,
                                 "is_error": bool(block.is_error),
                                 "content": _clip(text, TOOL_RESULT_CHARS)})
        elif kind == "RateLimitEvent":
            info = getattr(msg, "rate_limit_info", None)
            self._write({"type": "rate_limit", "status": getattr(info, "status", None),
                         "resets_at": getattr(info, "resets_at", None)})
        elif kind == "ResultMessage":
            self._write({"type": "result", "subtype": getattr(msg, "subtype", None),
                         "session_id": getattr(msg, "session_id", None),
                         "num_turns": getattr(msg, "num_turns", None),
                         "duration_ms": getattr(msg, "duration_ms", None),
                         "total_cost_usd": getattr(msg, "total_cost_usd", None),
                         "usage": getattr(msg, "usage", None)})

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass. If the SDK dataclass constructors in the test reject a field, check `claude_agent_sdk/types.py` for the installed signature and fix the test's constructor call, not the writer.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/agents/transcript.py tests/test_transcript.py
git commit -m "feat: JSONL transcript writer for agent sessions" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Runner — transcript, escalation, token budget, session metadata

**Files:**
- Modify: `src/agent_sdlc/agents/runner.py`
- Modify: `src/agent_sdlc/ports.py` (`AgentRunner.run` signature)
- Modify: `tests/fakes.py` (`FakeRunner.run` signature; records `traces`, `budgets`)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `evaluate_tool`, `Denial`, `ESCALATE_CATEGORIES`, `AgentResult`, exceptions (Task 1); `TranscriptWriter` (Task 4).
- Produces: `AgentRunner.run(role: Role, prompt: str, cwd: Path, max_turns: int, trace: Path | None = None, token_budget: int | None = None) -> AgentResult`; `ClaudeAgentRunner(..., max_denials: int = 5)`; module constant `_INTERRUPT_GRACE_S = 30.0`. The returned `AgentResult` always has `role` set. `escalated` is a denial category, `"denial_threshold"` or `"budget"`. On `UsageLimitError`/`AgentInfraError`, `partial` holds denials, the trace path and the usage estimated so far. `FakeRunner` gains `traces: list[Path | None]` and `budgets: list[int | None]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
import agent_sdlc.agents.runner as runner_mod


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
```

Add `import json` at the top of `tests/test_runner.py` if it's not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'max_denials'` / `'token_budget'`.

- [ ] **Step 3: Implement**

`src/agent_sdlc/ports.py`, replace the `AgentRunner` protocol:

```python
class AgentRunner(Protocol):
    async def run(self, role: Role, prompt: str, cwd: Path, max_turns: int,
                  trace: Path | None = None,
                  token_budget: int | None = None) -> AgentResult: ...
```

`tests/fakes.py`, replace `FakeRunner`:

```python
@dataclass
class FakeRunner:
    """Per-role behavior. Default: planner returns a plan, implementer writes feature.txt,
    reviewer says no blocking issues."""
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    traces: list[Path | None] = field(default_factory=list)
    budgets: list[int | None] = field(default_factory=list)

    async def run(self, role: Role, prompt: str, cwd: Path, max_turns: int,
                  trace: Path | None = None, token_budget: int | None = None) -> AgentResult:
        self.calls.append((role.name, prompt))
        self.traces.append(trace)
        self.budgets.append(token_budget)
        if role.name in self.behaviors:
            out = self.behaviors[role.name](role, prompt, cwd)
            return await out if isinstance(out, Awaitable) else out  # type: ignore[misc]
        if role.name == "implementer":
            (cwd / "feature.txt").write_text(f"change {len(self.calls)}\n")
        text = {"planner": "1. Add feature.txt", "reviewer": "No blocking issues."}.get(
            role.name, "done")
        return AgentResult(text, Usage(3, 1000, 200))
```

`src/agent_sdlc/agents/runner.py`:

Add imports: `import asyncio`, `import logging`; `from agent_sdlc.agents.transcript import TranscriptWriter`; add `ESCALATE_CATEGORIES` to the types import. Add module-level:

```python
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
```

Change the constructor to accept and store `max_denials`:

```python
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
```

Replace `run` (keep the SDK imports block and `options` exactly as they are, except the hook) with:

```python
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
            if escalated is None:
                if denial.category in ESCALATE_CATEGORIES:
                    escalated = denial.category
                elif len(denials) >= self._max_denials:
                    escalated = "denial_threshold"
            return cast(SyncHookJSONOutput, _deny(denial.reason))

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
                                raise AgentInterrupted(role.name)
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
                                await client.interrupt()
                                window.reschedule(
                                    asyncio.get_running_loop().time() + _INTERRUPT_GRACE_S)
                except TimeoutError:
                    if not stopping:
                        raise
        except AgentInterrupted:
            raise
        except Exception as e:
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
                return AgentResult(texts[-1].strip() if texts else "", _estimate(per_message),
                                   tuple(denials), escalated=escalated, trace=writer.path,
                                   trace_error=writer.error, role=role.name,
                                   usage_estimated=True)
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
```

Notes for the implementer:
- A `TimeoutError` escaping the `asyncio.timeout` block can only come from the rescheduled window, because the deadline is `None` until an escalation.
- The existing `test_run_pre_tool_use_hook_denies_and_allows` now also escalates (protected path) but still sees `{}` for the allowed call, because the hook never blocks non-violating calls after an escalation. The session is interrupted at the next message instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/agents/runner.py src/agent_sdlc/ports.py tests/fakes.py tests/test_runner.py
git commit -m "feat: runner writes transcripts, escalates on policy probing and budget" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Config, protected tooling globs, workspace logs and digests

**Files:**
- Modify: `src/agent_sdlc/targets.py`, `targets/rallysource.yaml`
- Modify: `src/agent_sdlc/workspaces.py`, `src/agent_sdlc/ports.py` (`WorkspacePort`)
- Test: `tests/test_targets.py`, `tests/test_workspaces.py`

**Interfaces:**
- Consumes: `CommandResult.log` (Task 1).
- Produces: `PolicyConfig.manifest_paths: list[str] = []` (validated disjoint from `protected_paths`); `Limits.max_denials_per_session: int = 5`, `Limits.stale_after_minutes: int = 120`; `Workspaces.run(name, command, wt, log: Path | None = None) -> CommandResult`; `Workspaces.install(wt, log: Path | None = None)`; `Workspaces.run_checks(wt, log_for: Callable[[str], Path | None] | None = None)`; `Workspaces.blob_digest(wt, paths: list[str]) -> str`; `Workspaces.tracked_files(wt) -> list[str]`; `Workspaces.diff(wt, max_chars=60000, paths: list[str] | None = None)`. `WorkspacePort` matches.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_targets.py`:

```python
from agent_sdlc.policy import PathPolicy
from agent_sdlc.targets import PolicyConfig


def test_rallysource_protects_tooling_config_but_not_app_config() -> None:
    t = load_target(ROOT / "targets" / "rallysource.yaml")
    pp = PathPolicy(t.policy.protected_paths)
    for p in ["eslint.config.js", "commitlint.config.js", "apps/rallysource-web/vite.config.ts",
              "apps/rallysource-teams/tailwind.config.ts", "apps/rallysource-web/postcss.config.js",
              "apps/rallysource-api/eslint.config.mjs", "packages/eslint-config/base.js",
              "turbo.json", ".npmrc", "apps/rallysource-api/.npmrc",
              "apps/rallysource-api/nest-cli.json"]:
        assert pp.is_protected(p), p
    for p in ["apps/rallysource-api/src/config/app.config.ts", "package.json",
              "apps/rallysource-api/package.json", "apps/rallysource-web/src/App.tsx"]:
        assert not pp.is_protected(p), p
    mp = PathPolicy(t.policy.manifest_paths)
    assert mp.violations(["package.json", "apps/rallysource-api/package.json",
                          "package-lock.json", "src/a.ts"]) == [
        "apps/rallysource-api/package.json", "package-lock.json", "package.json"]
    assert t.limits.max_denials_per_session == 5 and t.limits.stale_after_minutes == 120


def test_policy_rejects_path_both_protected_and_manifest() -> None:
    with pytest.raises(ValidationError):
        PolicyConfig(protected_paths=["**/package.json"], manifest_paths=["**/package.json"])
```

Append to `tests/test_workspaces.py`:

```python
import os


def test_run_writes_full_log(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    log = tmp_path / "logs" / "big.log"
    r = ws.run("big", "python3 -c \"print('x' * 9000)\"", wt, log=log)
    assert len(r.output) == 8000 and r.log == str(log)
    assert log.read_text().count("x") >= 9000
    assert oct(log.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_run_log_unwritable_dir(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    r = ws.run("test", "sh check.sh", wt, log=locked / "sub" / "t.log")
    assert r.ok and r.log is None


def test_run_checks_log_for(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    [r] = ws.run_checks(wt, log_for=lambda name: tmp_path / f"{name}.log")
    assert r.log == str(tmp_path / "test.log") and "ok" in (tmp_path / "test.log").read_text()


def test_blob_digest_tracks_content_and_deletion(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    base = ws.blob_digest(wt, ["README.md"])
    assert base == ws.blob_digest(wt, ["README.md"])
    (wt / "README.md").write_text("changed\n")
    ws.commit(wt, "c")
    changed = ws.blob_digest(wt, ["README.md"])
    (wt / "README.md").unlink()
    ws.commit(wt, "d")
    deleted = ws.blob_digest(wt, ["README.md"])
    assert len({base, changed, deleted}) == 3
    assert "README.md" in ws.changed_files(wt)  # a deletion still shows up for the gate
    assert ws.blob_digest(wt, []) == ws.blob_digest(wt, [])


def test_tracked_files_and_diff_paths(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    assert ws.tracked_files(wt) == ["README.md", "check.sh"]
    (wt / "a.txt").write_text("a\n")
    (wt / "b.txt").write_text("b\n")
    ws.commit(wt, "ab")
    d = ws.diff(wt, paths=["a.txt"])
    assert "a.txt" in d and "b.txt" not in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_targets.py tests/test_workspaces.py -v`
Expected: FAIL with `AttributeError: 'PolicyConfig' object has no attribute 'manifest_paths'` and `TypeError: run() got an unexpected keyword argument 'log'`.

- [ ] **Step 3: Implement**

`src/agent_sdlc/targets.py`: import `model_validator` from pydantic and replace `PolicyConfig` and `Limits`:

```python
class PolicyConfig(BaseModel):
    protected_paths: list[str]
    # Changes to these park the item for human approval before install/verify (spec §5.2).
    manifest_paths: list[str] = Field(default_factory=list)
    max_diff_lines: int = 600

    @model_validator(mode="after")
    def _disjoint(self) -> PolicyConfig:
        both = sorted(set(self.protected_paths) & set(self.manifest_paths))
        if both:
            raise ValueError(f"paths cannot be both protected and manifest: {both}")
        return self


class Limits(BaseModel):
    max_concurrent_items: int = 1
    max_verify_retries: int = 3
    max_pr_rounds: int = 3
    max_turns: dict[str, int] = Field(
        default_factory=lambda: {"plan": 30, "implement": 80, "review": 30})
    max_item_tokens: int = 2_000_000
    max_daily_agent_turns: int = 400
    max_daily_tokens: int = 20_000_000
    run_window: RunWindow | None = None
    max_denials_per_session: int = 5
    stale_after_minutes: int = 120
```

`targets/rallysource.yaml`: replace the `policy:` block with:

```yaml
policy:
  protected_paths:
    - "**/prisma/migrations/**"
    - "**/prisma/schema.prisma"
    - "infra/**"
    - "azure-pipelines*.yml"
    - "Dockerfile*"
    - ".env*"
    - "**/.env*"
    - ".husky/**"
    - "**/*.pem"
    - "**/*.key"
    - "sonar-project.properties"
    # Tooling config that verify executes as code (checked against the tree 2026-09-24).
    # Root-anchored so app source such as apps/*/src/config/app.config.ts stays writable.
    - "*.config.*"
    - "apps/*/*.config.*"
    - "packages/*/*.config.*"
    - "packages/eslint-config/**"
    - "turbo.json"
    - ".npmrc"
    - "**/.npmrc"
    - "**/nest-cli.json"
  manifest_paths:
    - "**/package.json"
    - "package-lock.json"
    - "**/package-lock.json"
    - "**/npm-shrinkwrap.json"
  max_diff_lines: 600
```

and add under `limits:`:

```yaml
  max_denials_per_session: 5
  stale_after_minutes: 120
```

`src/agent_sdlc/workspaces.py`: add `import hashlib`, `import logging`, `from collections.abc import Callable`; add `log = logging.getLogger(__name__)`. Add module function:

```python
def _write_log(path: Path, command: str, output: str, code: int) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"$ {command}\n{output}\n[exit {code}]\n")
        return str(path)
    except OSError as e:
        log.warning("could not write command log %s: %s", path, e)
        return None
```

Replace `run`, `install`, `run_checks`, `diff` and add `blob_digest`, `tracked_files`:

```python
    def run(self, name: str, command: str, wt: Path, log: Path | None = None) -> CommandResult:
        self.home.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        try:
            # shell=True is deliberate: commands come only from the trusted target YAML,
            # never from agents or work item text.
            r = subprocess.run(
                command,
                shell=True,
                cwd=wt,
                capture_output=True,
                text=True,
                env=self._cmd_env,
                timeout=self._t.repo.command_timeout_s,
            )
            code, out = r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {self._t.repo.command_timeout_s}s"
        written = _write_log(log, command, out, code) if log is not None else None
        return CommandResult(
            name, command, code, out[-_OUTPUT_TAIL:], round(time.monotonic() - start, 2),
            written,
        )

    def install(self, wt: Path, log: Path | None = None) -> CommandResult:
        return self.run("install", self._t.repo.install, wt, log)

    def run_checks(self, wt: Path,
                   log_for: Callable[[str], Path | None] | None = None) -> list[CommandResult]:
        return [self.run(name, cmd, wt, log_for(name) if log_for else None)
                for name, cmd in self._t.repo.commands.items()]

    def diff(self, wt: Path, max_chars: int = 60000, paths: list[str] | None = None) -> str:
        extra = ["--", *paths] if paths else []
        return self._git("diff", self._range(), *extra, cwd=wt)[:max_chars]

    def blob_digest(self, wt: Path, paths: list[str]) -> str:
        """sha256 over (path, blob at HEAD) for `paths`; a path absent at HEAD counts as
        deleted, so removing a manifest changes the digest too."""
        blobs: dict[str, str] = {}
        if paths:
            out = self._git("ls-tree", "-z", "HEAD", "--", *paths, cwd=wt)
            for entry in filter(None, out.split("\0")):
                meta, path = entry.split("\t", 1)
                blobs[path] = meta.split()[2]
        lines = [f"{p}:{blobs.get(p, 'deleted')}" for p in sorted(paths)]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def tracked_files(self, wt: Path) -> list[str]:
        out = self._git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=wt)
        return sorted(p for p in out.split("\0") if p)
```

`src/agent_sdlc/ports.py`: add `from collections.abc import Callable` and replace the changed `WorkspacePort` methods:

```python
    def install(self, wt: Path, log: Path | None = None) -> CommandResult: ...
    def run_checks(self, wt: Path,
                   log_for: Callable[[str], Path | None] | None = None
                   ) -> list[CommandResult]: ...
    def diff(self, wt: Path, max_chars: int = 60000,
             paths: list[str] | None = None) -> str: ...
    def blob_digest(self, wt: Path, paths: list[str]) -> str: ...
    def tracked_files(self, wt: Path) -> list[str]: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/targets.py targets/rallysource.yaml src/agent_sdlc/workspaces.py src/agent_sdlc/ports.py tests/test_targets.py tests/test_workspaces.py
git commit -m "feat: protect tooling config, manifest paths, command logs and blob digests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Manifest requeue and reporting

**Files:**
- Modify: `src/agent_sdlc/orchestrator/transitions.py` (`requeue`)
- Modify: `src/agent_sdlc/orchestrator/reporting.py` (`park_comment_html`, `_park_detail`, `pr_body`)
- Test: `tests/test_transitions.py`, `tests/test_reporting.py`

**Interfaces:**
- Consumes: `ParkReason.MANIFEST` (Task 1).
- Produces: `requeue` sends a `manifest` park to `verify` and moves `data["manifest_pending"]` → `data["manifest_approved"]`, dropping `manifest_diff`. Park comments render `data["manifest_diff"]` for `manifest` parks and `data["last_denials"]` (a list of `{role, tool, category, reason, input}`) for `policy` parks from plan/implement/review. `pr_body` renders `data["denial_counts"]` (`{role: n}`) and `data["denials"]` (first 10) as `## Blocked tool calls`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transitions.py`:

```python
from agent_sdlc.orchestrator.transitions import requeue
from agent_sdlc.types import Item, ParkReason, Stage


def test_requeue_manifest_goes_to_verify_and_approves_digest() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.MANIFEST,
              parked_from=Stage.IMPLEMENT, attempt=2,
              data={"manifest_pending": "abc", "manifest_diff": "d", "park_note": "n",
                    "parked_tag_set": True})
    new = requeue(it)
    assert new.stage is Stage.VERIFY and new.attempt == 0 and new.park_reason is None
    assert new.data == {"manifest_approved": "abc"}
```

(Skip any import in that snippet that `tests/test_transitions.py` already has.)

Append to `tests/test_reporting.py`:

```python
DENIAL = {"role": "implementer", "tool": "Read", "category": "outside_worktree",
          "reason": "path is outside the worktree",
          "input": '{"file_path": "/Users/x/.ssh/config"}'}


def test_park_comment_manifest_shows_diff_and_approval_effect() -> None:
    it = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.MANIFEST,
              parked_from=Stage.IMPLEMENT,
              data={"park_note": "Dependency manifests changed: package.json.",
                    "manifest_diff": '+  "left-pad": "1.0.0"'})
    h = park_comment_html(it)
    assert "approves these dependency changes" in h
    assert "left-pad" in h and "&quot;" in h  # escaped


def test_park_comment_policy_lists_last_denials() -> None:
    it = Item(5, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.POLICY,
              parked_from=Stage.IMPLEMENT, data={"park_note": "stopped",
                                                 "last_denials": [DENIAL]})
    h = park_comment_html(it)
    assert "Blocked tool calls" in h and ".ssh/config" in h and "outside_worktree" in h


def test_pr_body_lists_blocked_tool_calls() -> None:
    item = replace(ITEM, data={**ITEM.data, "denial_counts": {"implementer": 2, "planner": 1},
                               "denials": [DENIAL]})
    body = pr_body(item, WI, DEC, CHECKS, "notes")
    assert "## Blocked tool calls\n3 blocked (implementer: 2, planner: 1)" in body
    assert "- implementer `Read` [outside_worktree]: path is outside the worktree" in body
    assert "## Blocked tool calls" not in pr_body(ITEM, WI, DEC, CHECKS, "notes")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transitions.py tests/test_reporting.py -v`
Expected: FAIL (manifest requeue goes back to `implement`; no "approves these dependency changes"; no "Blocked tool calls").

- [ ] **Step 3: Implement**

`src/agent_sdlc/orchestrator/transitions.py`, replace `requeue`:

```python
def requeue(item: Item) -> Item:
    if item.stage is not Stage.PARKED or item.parked_from is None:
        raise ValueError(f"item {item.id} is not parked")
    reason, source = item.park_reason, item.parked_from
    to = _APPROVE_NEXT.get(source, source) if reason in GATE_PARKS else source
    if reason is ParkReason.PR_ROUNDS:
        to = Stage.IMPLEMENT  # apply the change request kept in data["feedback"] (I5)
    data = {k: v for k, v in item.data.items() if k not in ("park_note", "parked_tag_set")}
    if reason is ParkReason.BUDGET:
        data["budget_offset"] = item.usage.tokens
    if reason is ParkReason.MANIFEST:
        # Approval of exactly the digest the human saw; install and verify run next (spec §5.2).
        to = Stage.VERIFY
        pending = data.pop("manifest_pending", None)
        data.pop("manifest_diff", None)
        if pending is not None:
            data["manifest_approved"] = pending
    return replace(item, stage=to, park_reason=None, parked_from=None, attempt=0, replans=0,
                   infra_failures=0,
                   pr_rounds=0 if reason is ParkReason.PR_ROUNDS else item.pr_rounds, data=data)
```

`src/agent_sdlc/orchestrator/reporting.py`:

Add constants and helpers:

```python
_AGENT_STAGES = {Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW}


def _denials_html(item: Item) -> str:
    denials = item.data.get("last_denials") or []
    if item.park_reason is not ParkReason.POLICY or item.parked_from not in _AGENT_STAGES \
            or not denials:
        return ""
    rows = "".join(
        f"<li><code>{html.escape(d['tool'])}</code> [{html.escape(d['category'])}]: "
        f"{html.escape(d['reason'])} <code>{html.escape(d.get('input', ''))}</code></li>"
        for d in denials)
    return f"<p><b>Blocked tool calls</b> (most recent agent session):</p><ul>{rows}</ul>"


def _denials_md(data: dict[str, Any]) -> str:
    counts: dict[str, int] = data.get("denial_counts") or {}
    if not counts:
        return ""
    total = sum(counts.values())
    per_role = ", ".join(f"{r}: {n}" for r, n in sorted(counts.items()))
    lines = [f"## Blocked tool calls\n{total} blocked ({per_role})"]
    lines += [f"- {d['role']} `{d['tool']}` [{d['category']}]: {d['reason']}"
              for d in (data.get("denials") or [])[:10]]
    return "\n".join(lines)
```

In `pr_body`, insert `_denials_md(item.data),` into `parts` right after the `## Usage` entry (empty strings are already filtered out).

Replace `park_comment_html` and `_park_detail` with:

```python
def park_comment_html(item: Item) -> str:
    reason = item.park_reason.value if item.park_reason else "unknown"
    stage = item.parked_from.value if item.parked_from else "unknown"
    note = html.escape(str(item.data.get("park_note", "")))

    # Determine guidance based on park type
    is_gate_park = item.park_reason in GATE_PARKS
    is_gate_stage = item.parked_from in {Stage.TRIAGE, Stage.PLAN, Stage.REVIEW}

    if item.park_reason is ParkReason.MANIFEST:
        guidance = ("Removing the <code>agent:parked</code> tag approves these dependency "
                    "changes; install and verify will run with them.")
    elif is_gate_park and is_gate_stage and item.parked_from is not None:
        next_stages = {Stage.TRIAGE: "plan", Stage.PLAN: "implement", Stage.REVIEW: "pr_open"}
        next_stage = next_stages.get(item.parked_from, "unknown")
        guidance = (f"To continue, update the item if needed and remove the "
                    f"<code>agent:parked</code> tag to approve proceeding to the <code>{next_stage}"
                    f"</code> stage.")
    else:
        retry = "implement" if item.park_reason is ParkReason.PR_ROUNDS else stage
        guidance = (f"To continue, update the item if needed and remove the "
                    f"<code>agent:parked</code> tag to retry the <code>{retry}</code> stage with "
                    f"fresh retry counters.")

    return (f"<p><b>agent-sdlc parked this item</b> at stage <code>{stage}</code> "
            f"(reason: <code>{reason}</code>).</p><pre>{note}</pre>"
            f"{_park_detail(item)}{_denials_html(item)}<p>{guidance}</p>")


def _park_detail(item: Item) -> str:
    """The artifact a human must judge before approving: the plan, the review notes (I3) or
    the manifest diff (spec §5.2)."""
    if item.park_reason is ParkReason.MANIFEST:
        title, text = "Manifest diff", str(item.data.get("manifest_diff", ""))
    elif item.parked_from is Stage.PLAN:
        title, text = "Plan", str(item.data.get("plan", ""))
    elif item.parked_from is Stage.REVIEW:
        title, text = "Review notes", str(item.data.get("review_notes", ""))
    else:
        return ""
    if not text:
        return ""
    if len(text) > _PARK_DETAIL_CHARS:
        text = text[:_PARK_DETAIL_CHARS] + "\n…(truncated)"
    return f"<p><b>{title}</b>:</p><pre>{html.escape(text)}</pre>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/transitions.py src/agent_sdlc/orchestrator/reporting.py tests/test_transitions.py tests/test_reporting.py
git commit -m "feat: manifest park approval and blocked-call reporting" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: StageExecutor — events, traces, escalation, manifest gate, install by digest

**Files:**
- Create: `src/agent_sdlc/orchestrator/events.py`
- Modify: `src/agent_sdlc/orchestrator/stages.py`
- Modify: `tests/test_stages.py:58` (`installed` → `installed_digest`)
- Test: `tests/test_stages.py`, `tests/test_events.py`

**Interfaces:**
- Consumes: Tasks 1, 5, 6, 7.
- Produces: `events.agent_events(res: AgentResult) -> list[EventInput]`, `events.check_event(r: CommandResult) -> EventInput`, `events.transition_event(before: Item, after: Item) -> EventInput`; `StepResult.events: list[EventInput]`; `StageExecutor(..., traces: Path | None = None, clock: Callable[[], datetime] | None = None)`; trace files named `<traces>/<item>/<UTC yyyymmddTHHMMSS>-<stage>-a<attempt>-<name>.<jsonl|log>`; item data keys `installed_digest`, `manifest_pending`, `manifest_diff`, `manifest_approved`, `denial_counts`, `denials`, `last_denials`, and `checks[*].log`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_events.py`:

```python
from agent_sdlc.orchestrator.events import agent_events, check_event, transition_event
from agent_sdlc.types import (
    AgentResult,
    CommandResult,
    Denial,
    Item,
    ParkReason,
    Stage,
    Usage,
)


def test_agent_events() -> None:
    res = AgentResult("t", Usage(3, 100, 20, 50), (Denial("Bash", "command_not_allowlisted",
                                                          "command not allowlisted: git",
                                                          '{"command": "git push"}'),),
                      escalated=None, session_id="s", duration_ms=10, cost_usd=0.1,
                      trace="/t.jsonl", role="implementer")
    [session, denied] = agent_events(res)
    assert session.kind == "agent_session" and session.payload["role"] == "implementer"
    assert (session.payload["turns"], session.payload["input_tokens"],
            session.payload["cache_read_tokens"], session.payload["denials"]) == (3, 100, 50, 1)
    assert session.payload["transcript"] == "/t.jsonl"
    assert denied.kind == "tool_denied" and denied.payload["tool"] == "Bash"


def test_check_and_transition_events() -> None:
    ev = check_event(CommandResult("test", "npm test", 1, "x", 2.5, "/l.log"))
    assert ev.payload == {"name": "test", "command": "npm test", "exit_code": 1,
                          "duration_s": 2.5, "log": "/l.log"}
    before = Item(1, "t", "x", "b", Stage.IMPLEMENT)
    after = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.POLICY,
                 parked_from=Stage.IMPLEMENT, data={"park_note": "n" * 3000})
    tr = transition_event(before, after)
    assert tr.payload["from"] == "implement" and tr.payload["to"] == "parked"
    assert tr.payload["park_reason"] == "policy" and len(tr.payload["note"]) == 2000
```

In `tests/test_stages.py`, change line 58 from
`    assert res.data["installed"] is True`
to
`    assert isinstance(res.data["installed_digest"], str)`.

Append to `tests/test_stages.py`:

```python
from datetime import UTC, datetime

from agent_sdlc.types import Denial

T0 = datetime(2026, 10, 2, 19, 4, 12, tzinfo=UTC)
ESCAPE = Denial("Read", "outside_worktree", "path is outside the worktree",
                '{"file_path": "/Users/x/.ssh/config"}')


def _executor(tmp_path: Path, target: TargetConfig, origin_repo: Path,
              traces: Path | None = None):  # type: ignore[no-untyped-def]
    ado = FakeAdo(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", target)
    runner = FakeRunner()
    ex = StageExecutor(target=target, ado=ado, decider=FakeDecider(), runner=runner,
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=lambda _id: [], traces=traces, clock=lambda: T0)
    return ex, ado, ws, runner


def _with_manifests(target: TargetConfig) -> TargetConfig:
    return target.model_copy(update={"policy": target.policy.model_copy(
        update={"manifest_paths": ["**/package.json"]})})


async def test_agent_session_events_and_trace_path(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, _, runner = _executor(tmp_path, target, origin_repo, traces=tmp_path / "traces")
    res = await ex.run(item(Stage.PLAN))
    assert [e.kind for e in res.events] == ["agent_session"]
    assert res.events[0].payload["role"] == "planner"
    assert runner.traces == [tmp_path / "traces" / "5" / "20261002T190412-plan-a0-planner.jsonl"]


async def test_token_budget_passed_to_runner(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_, runner = parts
    await ex.run(item(Stage.PLAN, usage=Usage(0, 1_999_000, 0)))
    assert runner.budgets == [1000]


async def test_escalated_session_parks_policy(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, _, runner = parts
    runner.behaviors["implementer"] = lambda r, p, c: AgentResult(
        "", Usage(1, 5, 5), (ESCAPE,), escalated="outside_worktree")
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.POLICY
    assert "outside_worktree" in res.transition.note and ".ssh/config" in res.transition.note
    assert [e.kind for e in res.events] == ["check", "agent_session", "tool_denied"]
    assert res.data["denial_counts"] == {"implementer": 1}
    assert res.data["last_denials"][0]["tool"] == "Read"
    assert res.usage == Usage(1, 5, 5)


async def test_budget_escalation_parks_budget(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_, runner = parts
    runner.behaviors["planner"] = lambda r, p, c: AgentResult(
        "", Usage(1, 5, 5), escalated="budget")
    res = await ex.run(item(Stage.PLAN))
    assert res.transition.park_reason is ParkReason.BUDGET


async def test_verify_writes_check_logs_and_events(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, _ = _executor(tmp_path, target, origin_repo, traces=tmp_path / "traces")
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.VERIFY, attempt=1))
    names = [e.payload["name"] for e in res.events if e.kind == "check"]
    assert names == ["install", "test"]
    log = res.data["checks"][0]["log"]
    assert log.endswith("20261002T190412-verify-a1-test.log") and "ok" in Path(log).read_text()


async def test_manifest_change_parks_then_approval_reinstalls(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)

    def add_dep(r, p, cwd):  # type: ignore[no-untyped-def]
        (cwd / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        return AgentResult("done", Usage(1, 1, 1))

    runner.behaviors["implementer"] = add_dep
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert "package.json" in res.transition.note and "left-pad" in res.data["manifest_diff"]
    pending = res.data["manifest_pending"]
    first_install = res.data["installed_digest"]
    # Approved: verify reinstalls because the manifest content changed, then runs checks.
    data = {"manifest_approved": pending, "installed_digest": first_install}
    res = await ex.run(item(Stage.VERIFY, data=data))
    assert res.transition.to is Stage.REVIEW
    assert [e.payload["name"] for e in res.events if e.kind == "check"] == ["install", "test"]
    assert res.data["installed_digest"] != first_install
    # Same digest again: no reinstall, no park.
    res2 = await ex.run(item(Stage.VERIFY, data={**data, **res.data}))
    assert [e.payload["name"] for e in res2.events if e.kind == "check"] == ["test"]


async def test_unapproved_manifest_blocks_install_at_implement_start(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, _, ws, runner = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package.json").write_text("{}\n")
    ws.commit(wt, "sneak")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.park_reason is ParkReason.MANIFEST
    assert runner.calls == [] and res.events == []  # no install, no agent


async def test_pr_open_rechecks_manifests(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, ado, ws, _ = _executor(tmp_path, _with_manifests(target), origin_repo)
    wt = ws.create(5, "agent/5-add-feature")
    (wt / "package.json").write_text("{}\n")
    ws.commit(wt, "x")
    res = await ex.run(item(Stage.PR_OPEN, data={"plan": "p", "checks": []}))
    assert res.transition.park_reason is ParkReason.MANIFEST and ado.prs == {}


async def test_legacy_installed_flag_reinstalls_once(parts) -> None:  # type: ignore[no-untyped-def]
    ex, _, ws, *_ = parts
    ws.create(5, "agent/5-add-feature")
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", "installed": True}))
    assert [e.payload["name"] for e in res.events if e.kind == "check"] == ["install"]
    again = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", **res.data}))
    assert [e for e in again.events if e.kind == "check"] == []


async def test_awaiting_records_pr_comment_events(parts) -> None:  # type: ignore[no-untyped-def]
    ex, ado, _, decider, _ = parts
    pr = ado.create_pr("agent/5-add-feature", "t", "b", 5)
    ado.pr_threads[pr] = [PrComment(1, 1, "Brian", "/agent rename it")]
    res = await ex.run(item(Stage.AWAITING_HUMAN, pr_id=pr))
    assert [(e.kind, e.payload["intent"]) for e in res.events] == [
        ("pr_comment", "change_request")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events.py tests/test_stages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_sdlc.orchestrator.events'` and `TypeError: ... unexpected keyword argument 'traces'`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/orchestrator/events.py`:

```python
from __future__ import annotations

from agent_sdlc.types import AgentResult, CommandResult, EventInput, Item, Stage


def agent_events(res: AgentResult) -> list[EventInput]:
    """One agent_session event plus one tool_denied event per denial (spec §3)."""
    u = res.usage
    payload = {
        "role": res.role, "session_id": res.session_id,
        "subtype": res.error or ("interrupted" if res.escalated else "success"),
        "is_error": res.is_error, "turns": u.turns, "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens, "cache_read_tokens": u.cache_read_tokens,
        "cost_usd": res.cost_usd, "duration_ms": res.duration_ms, "denials": len(res.denials),
        "escalated": res.escalated, "transcript": res.trace,
        "usage_estimated": res.usage_estimated,
    }
    if res.trace_error:
        payload["trace_error"] = res.trace_error
    events = [EventInput("agent_session", payload)]
    events += [EventInput("tool_denied", {"role": res.role, "tool": d.tool,
                                          "category": d.category, "reason": d.reason,
                                          "input": d.input}) for d in res.denials]
    return events


def check_event(r: CommandResult) -> EventInput:
    return EventInput("check", {"name": r.name, "command": r.command, "exit_code": r.exit_code,
                                "duration_s": r.duration_s, "log": r.log})


def transition_event(before: Item, after: Item) -> EventInput:
    parked = after.stage is Stage.PARKED
    return EventInput("transition", {
        "from": before.stage.value, "to": after.stage.value,
        "park_reason": after.park_reason.value if parked and after.park_reason else None,
        "note": str(after.data.get("park_note") or "")[:2000] if parked else "",
        "pr_round": after.pr_rounds,
    })
```

Replace `src/agent_sdlc/orchestrator/stages.py` with:

```python
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_sdlc.agents.roles import (
    IMPLEMENTER,
    PLANNER,
    REVIEWER,
    Role,
    implementer_prompt,
    planner_prompt,
    reviewer_prompt,
)
from agent_sdlc.decisions.gates import triage_state, work_item_text
from agent_sdlc.orchestrator.events import agent_events, check_event
from agent_sdlc.orchestrator.reporting import (
    QUESTION_REPLY,
    UNCERTAIN_REPLY,
    commit_message,
    plan_comment_html,
    pr_body,
    pr_title,
)
from agent_sdlc.orchestrator.transitions import (
    CommentOutcome,
    Transition,
    after_agent_error,
    after_implement,
    after_plan,
    after_pr_poll,
    after_review,
    after_triage,
    after_verify,
    classify_comment,
    park,
)
from agent_sdlc.policy import PathPolicy
from agent_sdlc.ports import AdoPort, AgentRunner, DeciderPort, WorkspacePort
from agent_sdlc.store import LabelInput
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import (
    AgentResult,
    CommandResult,
    Decision,
    EventInput,
    Item,
    ParkReason,
    Stage,
    Usage,
)

log = logging.getLogger(__name__)
_KEPT_DENIALS = 10     # denials kept on the item for the PR body
_PARK_DENIALS = 5      # denials quoted in a park note/comment
_MANIFEST_DIFF_CHARS = 6000


@dataclass
class StepResult:
    transition: Transition
    usage: Usage = Usage()
    decisions: list[tuple[Decision, dict[str, Any]]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    pr_id: int | None = None
    labels: list[LabelInput] = field(default_factory=list)
    events: list[EventInput] = field(default_factory=list)


def _logged(
    ds: dict[str, Decision], state: dict[str, Any]
) -> list[tuple[Decision, dict[str, Any]]]:
    return [(d, state) for d in ds.values()]


def _check_dict(r: CommandResult) -> dict[str, Any]:
    d = asdict(r)
    d["output"] = r.output[-2000:]
    return d


def _denial_dicts(res: AgentResult) -> list[dict[str, str]]:
    return [{"role": res.role, "tool": d.tool, "category": d.category, "reason": d.reason,
             "input": d.input} for d in res.denials]


class StageExecutor:
    def __init__(self, *, target: TargetConfig, ado: AdoPort, decider: DeciderPort,
                 runner: AgentRunner, workspaces: WorkspacePort, path_policy: PathPolicy,
                 decisions_for: Callable[[int], list[Decision]],
                 traces: Path | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._t = target
        self._ado = ado
        self._decider = decider
        self._runner = runner
        self._ws = workspaces
        self._pp = path_policy
        self._mp = PathPolicy(target.policy.manifest_paths)
        self._decisions_for = decisions_for
        self._traces = traces
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self, item: Item) -> StepResult:
        handlers = {
            Stage.TRIAGE: self._triage, Stage.PLAN: self._plan, Stage.IMPLEMENT: self._implement,
            Stage.VERIFY: self._verify, Stage.REVIEW: self._review, Stage.PR_OPEN: self._pr_open,
            Stage.AWAITING_HUMAN: self._awaiting,
        }
        return await handlers[item.stage](item)

    def _turns(self, stage: str) -> int:
        return self._t.limits.max_turns.get(stage, 30)

    # tracing & agents ------------------------------------------------------
    def _trace_path(self, item: Item, name: str, ext: str) -> Path | None:
        if self._traces is None:
            return None
        ts = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S")
        return (self._traces / str(item.id)
                / f"{ts}-{item.stage.value}-a{item.attempt}-{name}.{ext}")

    async def _run_agent(self, item: Item, role: Role, prompt: str, wt: Path,
                         stage: str) -> tuple[AgentResult, list[EventInput], dict[str, Any]]:
        spent = item.usage.tokens - int(item.data.get("budget_offset", 0))
        budget = max(self._t.limits.max_item_tokens - spent, 0)
        res = await self._runner.run(role, prompt, wt, self._turns(stage),
                                     trace=self._trace_path(item, role.name, "jsonl"),
                                     token_budget=budget)
        res = replace(res, role=role.name)
        log.info("agent %s: %s turns, %s tokens, %s denied%s", role.name, res.usage.turns,
                 f"{res.usage.tokens:,}", len(res.denials),
                 f", escalated {res.escalated}" if res.escalated else "")
        return res, agent_events(res), self._denial_data(item, res)

    @staticmethod
    def _denial_data(item: Item, res: AgentResult) -> dict[str, Any]:
        """Item-wide denial counts per role, the first 10 denials (PR body) and this session's
        denials (park comments). Empty when the session had none."""
        if not res.denials:
            return {}
        new = _denial_dicts(res)
        counts = dict(item.data.get("denial_counts") or {})
        counts[res.role] = counts.get(res.role, 0) + len(new)
        kept = list(item.data.get("denials") or [])
        kept += new[: max(0, _KEPT_DENIALS - len(kept))]
        return {"denial_counts": counts, "denials": kept, "last_denials": new[:_PARK_DENIALS]}

    def _stopped(self, res: AgentResult, events: list[EventInput],
                 data: dict[str, Any]) -> StepResult | None:
        """The runner interrupted the session (spec §5.3, §5.4)."""
        if res.escalated is None:
            return None
        if res.escalated == "budget":
            t = park(ParkReason.BUDGET,
                     f"The {res.role} session was stopped at the item token budget of "
                     f"{self._t.limits.max_item_tokens:,} tokens.")
        else:
            quoted = "\n".join(f"- {d.tool} [{d.category}]: {d.reason}"
                               + (f" {d.input}" if d.input else "")
                               for d in res.denials[:_PARK_DENIALS])
            t = park(ParkReason.POLICY,
                     f"The {res.role} agent was stopped after blocked tool calls "
                     f"({res.escalated}):\n{quoted}")
        return StepResult(t, res.usage, events=events, data=data)

    def _agent_failed(self, item: Item, res: AgentResult, events: list[EventInput],
                      data: dict[str, Any]) -> StepResult | None:
        """An agent error result (max turns, execution error) is a failed attempt (I2)."""
        if not res.is_error:
            return None
        t = after_agent_error(item.stage, res.error, item.attempt,
                              self._t.limits.max_verify_retries)
        return StepResult(t, res.usage, events=events, data=data)

    # manifests & install ---------------------------------------------------
    def _manifest_gate(self, item: Item, wt: Path) -> StepResult | None:
        """Park when the branch changes a manifest in a way no human has approved (§5.2)."""
        files = self._mp.violations(self._ws.changed_files(wt))
        if not files:
            return None
        digest = self._ws.blob_digest(wt, files)
        if digest == item.data.get("manifest_approved"):
            return None
        log.warning("unapproved manifest change: %s", ", ".join(files))
        diff = self._ws.diff(wt, paths=files)[:_MANIFEST_DIFF_CHARS]
        return StepResult(
            park(ParkReason.MANIFEST,
                 f"Dependency manifests changed: {', '.join(files)}. A human must approve "
                 f"them before install and verify run."),
            data={"manifest_pending": digest, "manifest_diff": diff})

    def _ensure_installed(self, item: Item, wt: Path
                          ) -> tuple[StepResult | None, dict[str, Any], list[EventInput]]:
        """Install when the manifests at HEAD differ from the last install. Callers run
        _manifest_gate first, so an unapproved manifest is never installed."""
        digest = self._ws.blob_digest(wt, self._mp.violations(self._ws.tracked_files(wt)))
        if digest == item.data.get("installed_digest"):
            return None, {}, []
        inst = self._ws.install(wt, log=self._trace_path(item, "install", "log"))
        events = [check_event(inst)]
        if not inst.ok:
            return (StepResult(park(ParkReason.INFRA, f"Install failed:\n{inst.output[-2000:]}"),
                               events=events), {}, events)
        return None, {"installed_digest": digest}, events

    # stages ----------------------------------------------------------------
    async def _triage(self, item: Item) -> StepResult:
        state = triage_state(self._ado.get_work_item(item.id))
        ds = self._decider.decide("triage", state)
        return StepResult(after_triage(ds), decisions=_logged(ds, state))

    async def _plan(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        res, events, data = await self._run_agent(
            item, PLANNER, planner_prompt(wi, item.data.get("feedback")), wt, "plan")
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        state = {"work_item": work_item_text(wi), "plan": res.text[:6000]}
        ds = self._decider.decide("plan", state)
        return StepResult(after_plan(ds, item.replans), res.usage, _logged(ds, state),
                          {"plan": res.text, "feedback": None, **data}, events=events)

    async def _implement(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        self._ws.reset(wt)
        if gate := self._manifest_gate(item, wt):
            return gate
        failed_install, inst_data, events = self._ensure_installed(item, wt)
        if failed_install:
            return failed_install
        res, agent_evs, data = await self._run_agent(
            item, IMPLEMENTER, implementer_prompt(wi, str(item.data.get("plan", "")),
                                                  item.data.get("feedback")),
            wt, "implement")
        events += agent_evs
        data.update(inst_data)
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        self._ws.commit(wt, commit_message(wi, item.pr_rounds))
        files = self._ws.changed_files(wt)
        t = after_implement(self._pp.violations(files), bool(files), self._ws.diff_lines(wt),
                            self._t.policy.max_diff_lines)
        if t.to is Stage.VERIFY and (gate := self._manifest_gate(item, wt)):
            return replace(gate, usage=res.usage, events=events, data={**data, **gate.data})
        return StepResult(t, res.usage, data={"feedback": None, **data}, events=events)

    async def _verify(self, item: Item) -> StepResult:
        wt = self._ws.create(item.id, item.branch)
        if gate := self._manifest_gate(item, wt):
            return gate
        failed_install, inst_data, events = self._ensure_installed(item, wt)
        if failed_install:
            return failed_install
        results = self._ws.run_checks(
            wt, log_for=lambda name: self._trace_path(item, name, "log"))
        events += [check_event(r) for r in results]
        for r in results:
            log.info("check %s: exit %s (%ss)", r.name, r.exit_code, r.duration_s)
        if self._ws.commit(wt, "style: apply lint fixes"):
            violations = self._pp.violations(self._ws.changed_files(wt))
            if violations:
                return StepResult(park(
                    ParkReason.POLICY,
                    "Lint fixes touched protected paths: " + ", ".join(violations)),
                    events=events)
            if gate := self._manifest_gate(item, wt):
                return replace(gate, events=events)
            lines, limit = self._ws.diff_lines(wt), self._t.policy.max_diff_lines
            if lines > limit:  # M9
                return StepResult(park(
                    ParkReason.POLICY,
                    f"After lint fixes the diff is {lines} lines, over the {limit}-line limit."),
                    events=events)
        t = after_verify(results, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, data={"checks": [_check_dict(r) for r in results], **inst_data},
                          events=events)

    async def _review(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        checks = [CommandResult(**c) for c in item.data.get("checks", [])]
        plan = str(item.data.get("plan", ""))
        res, events, data = await self._run_agent(
            item, REVIEWER, reviewer_prompt(wi, plan, self._ws.diff(wt), checks), wt, "review")
        if stop := self._stopped(res, events, data):
            return stop
        if failed := self._agent_failed(item, res, events, data):
            return failed
        state = {"work_item": work_item_text(wi, 3000), "plan": plan[:3000],
                 "review_notes": res.text[:6000]}
        ds = self._decider.decide("review", state)
        t = after_review(ds, res.text, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, res.usage, _logged(ds, state), {"review_notes": res.text, **data},
                          events=events)

    async def _pr_open(self, item: Item) -> StepResult:
        wi = self._ado.get_work_item(item.id)
        wt = self._ws.create(item.id, item.branch)
        violations = self._pp.violations(self._ws.changed_files(wt))
        if violations:
            return StepResult(park(
                ParkReason.POLICY,
                "Pre-push check found protected paths: " + ", ".join(violations)))
        if gate := self._manifest_gate(item, wt):
            return gate
        self._ado.push_branch(wt, item.branch)
        body = pr_body(item, wi, self._decisions_for(item.id), item.data.get("checks", []),
                       str(item.data.get("review_notes", "")))
        if item.pr_id:
            self._ado.update_pr(item.pr_id, body)
            pr_id = item.pr_id
        else:
            pr_id = self._ado.create_pr(item.branch, pr_title(wi), body, item.id)
            if pr_id:  # dry-run returns 0: there is no PR to point at (M6)
                self._ado.comment_work_item(
                    item.id, plan_comment_html(str(item.data.get("plan", "")), pr_id))
        return StepResult(Transition(Stage.AWAITING_HUMAN), pr_id=pr_id)

    async def _awaiting(self, item: Item) -> StepResult:
        assert item.pr_id is not None
        status = self._ado.pr_status(item.pr_id)
        seen = list(item.data.get("seen_comments", []))
        outcomes: list[CommentOutcome] = []
        logged: list[tuple[Decision, dict[str, Any]]] = []
        labels: list[LabelInput] = []
        events: list[EventInput] = []
        for c in self._ado.pr_comments(item.pr_id):
            if c.key in seen:
                continue
            seen.append(c.key)
            state = {"comment": c.content[:3000]}
            ds = self._decider.decide("comment", state)
            logged += _logged(ds, state)
            intent = classify_comment(c, ds)
            events.append(EventInput("pr_comment", {"thread_id": c.thread_id,
                                                    "comment_id": c.comment_id,
                                                    "author": c.author, "intent": intent}))
            if c.content.strip().lower().startswith("/agent"):
                d = ds["comment_intent"]
                labels.append(LabelInput("comment", "comment_intent", d.raw_probs,
                                         "change_request", "slash_command"))
            outcomes.append(CommentOutcome(c, intent))
            if intent in ("question", "uncertain") and status == "active":
                reply = QUESTION_REPLY if intent == "question" else UNCERTAIN_REPLY
                self._ado.reply_pr(item.pr_id, c.thread_id, c.comment_id, reply)
        t = after_pr_poll(status, outcomes, item.pr_rounds, self._t.limits.max_pr_rounds)
        return StepResult(t, decisions=logged, data={"seen_comments": seen}, labels=labels,
                          events=events)
```

Behavior note: the old `data["denied"]` key is no longer written; `denial_counts`, `denials` and `last_denials` replace it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass, including the existing e2e suite (the fixture target has no `manifest_paths`, so the gate is a no-op there).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/events.py src/agent_sdlc/orchestrator/stages.py tests/test_events.py tests/test_stages.py
git commit -m "feat: stage events, trace paths, escalation parks and manifest gate" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Scheduler writes events, tracks liveness and staleness

**Files:**
- Modify: `src/agent_sdlc/orchestrator/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Store` event API (Task 2), `log_context` (Task 3), `agent_events`/`transition_event` (Task 8), `StepResult.events` (Task 8), exception `partial` (Task 1).
- Produces: event kinds `intake`, `transition`, `outcome`, `requeue`, `infra_failure`, `usage_limit`, `park_tagged`, `park_side_effect_failed` (plus the executor's). Flag `last_tick` (ISO timestamp) is set at the start of every tick. A WARNING is logged per stale active item per tick.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler.py`:

```python
import logging

from agent_sdlc.types import AgentResult, EventInput


def _add_item(store: Store, stage: Stage, **kw):  # type: ignore[no-untyped-def]
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(store.get(5), stage=stage, **kw))
    return store.get(5)


async def test_step_writes_intake_step_and_transition_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    ex = ScriptedExecutor(StepResult(Transition(Stage.PLAN),
                                     events=[EventInput("agent_session", {"role": "x"})]))
    await sched(env, ex).tick()
    evs = store.events_for(5)
    assert [e.kind for e in evs] == ["intake", "agent_session", "transition"]
    assert evs[-1].stage == "triage" and evs[-1].payload["to"] == "plan"
    assert store.get_flag("last_tick") == NOW.isoformat()


async def test_awaiting_noop_poll_writes_no_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    ex = ScriptedExecutor(StepResult(Transition(Stage.AWAITING_HUMAN)),
                          StepResult(Transition(Stage.AWAITING_HUMAN)))
    s = sched(env, ex)
    await s.tick()
    await s.tick()
    assert store.events_for(5) == []


async def test_merge_writes_outcome(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    _add_item(store, Stage.AWAITING_HUMAN, pr_id=1)
    await sched(env, ScriptedExecutor(StepResult(Transition(Stage.DONE)))).tick()
    kinds = [(e.kind, e.payload.get("result")) for e in store.events_for(5)]
    assert kinds == [("transition", None), ("outcome", "merged")]


async def test_infra_failure_records_partial_session(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    partial = AgentResult("", Usage(1, 1, 1), role="planner")
    await sched(env, ScriptedExecutor(AgentInfraError("sdk down", partial=partial))).tick()
    assert [e.kind for e in store.events_for(5)] == ["intake", "agent_session", "infra_failure"]


async def test_usage_limit_records_event(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    err = UsageLimitError("usage limit reached", partial=AgentResult("", Usage(), role="planner"))
    await sched(env, ScriptedExecutor(err)).tick()
    assert [e.kind for e in store.events_for(5)][-2:] == ["agent_session", "usage_limit"]


async def test_requeue_writes_event(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, *_ = env
    _add_item(store, Stage.PARKED, park_reason=ParkReason.RED, parked_from=Stage.VERIFY,
              data={"parked_tag_set": True})
    s = sched(env, ScriptedExecutor(StepResult(Transition(Stage.REVIEW))))
    await s.tick()  # tag absent -> requeue to verify, then the step runs
    [rq] = [e for e in store.events_for(5) if e.kind == "requeue"]
    assert rq.payload == {"from_reason": "red", "to": "verify", "approved": False}


async def test_park_side_effects_write_events(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    await sched(env, ScriptedExecutor(StepResult(park(ParkReason.RED, "red")))).tick()
    assert [e.kind for e in store.events_for(5)][-2:] == ["transition", "park_tagged"]


async def test_stale_item_logs_warning(env, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    it = _add_item(store, Stage.PLAN)
    store.add_event("intake", {}, item=it, ts=NOW - timedelta(hours=3))
    with caplog.at_level(logging.WARNING):
        await sched(env, ScriptedExecutor(StepResult(Transition(Stage.PLAN)))).tick()
    assert "stale" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: FAIL (no events are written; `last_tick` is `None`).

- [ ] **Step 3: Implement**

In `src/agent_sdlc/orchestrator/scheduler.py`:

Imports: add `from agent_sdlc.logctx import log_context`, `from agent_sdlc.orchestrator.events import agent_events, transition_event`, and `AgentResult`, `EventInput` to the types import.

Add a module-level helper after `_merge`:

```python
def _partial_events(partial: object) -> list[EventInput]:
    """Events for the part of an agent session that ran before an exception (spec §3)."""
    return agent_events(partial) if isinstance(partial, AgentResult) else []
```

Replace `tick`:

```python
    async def tick(self) -> None:
        now = self._clock()
        self._store.set_flag("last_tick", now.isoformat())
        if self._paused(now):
            return
        self._intake()
        self._requeue_untagged()
        self._warn_stale(now)
        for item in self._store.items(self._t.name, [Stage.AWAITING_HUMAN]):
            await self._step(item, now)
        if not self._agent_work_allowed(now):
            return
        active = self._store.items(self._t.name, ACTIVE_STAGES)
        active.sort(key=lambda i: i.stage is Stage.TRIAGE)  # in-flight work first (stable)
        for item in active[: self._t.limits.max_concurrent_items]:
            if self._paused(self._clock()):
                return
            await self._step(item, now)

    def _warn_stale(self, now: datetime) -> None:
        limit = timedelta(minutes=self._t.limits.stale_after_minutes)
        for item in self._store.items(self._t.name, ACTIVE_STAGES):
            last = self._store.last_event_ts(item.id)
            if last is not None and now - last > limit:
                with log_context(item.id, item.stage.value):
                    log.warning("stale: no event for %s", now - last)
```

In `_intake`, replace the `add_item` branch body:

```python
            if self._store.add_item(self._t.name, wi, branch):
                self._store.add_event("intake", {"title": wi.title, "branch": branch},
                                      item=self._store.get(wi.id))
                log.info("intake: #%s %s", wi.id, wi.title)
```

In `requeue_item`, replace the `commit_step` call:

```python
        event = EventInput("requeue", {
            "from_reason": item.park_reason.value if item.park_reason else None,
            "to": new.stage.value, "approved": bool(labels)})
        self._store.commit_step(new, [], Usage(), self._clock().date(), labels,
                                events=[event], at=item)
```

Replace `_step` and add `_run_step`:

```python
    async def _step(self, item: Item, now: datetime) -> None:
        with log_context(item.id, item.stage.value):
            await self._run_step(item, now)

    async def _run_step(self, item: Item, now: datetime) -> None:
        retry_after = item.data.get("retry_after")
        if retry_after and datetime.fromisoformat(str(retry_after)) > now:
            return
        try:
            res = await self._executor.run(item)
        except UsageLimitError as e:
            # Tokens spent before the limit hit still count toward item and daily budgets.
            until = e.reset_at or (now + _DEFAULT_PAUSE)
            events = _partial_events(e.partial) + [
                EventInput("usage_limit", {"until": until.isoformat()})]
            self._store.commit_step(replace(item, usage=item.usage + e.usage), [], e.usage,
                                    now.date(), [], events=events, at=item)
            self._store.set_flag("paused_until", until.isoformat())
            log.warning("usage limit hit; pausing until %s", until)
            return
        except AgentInterrupted:
            return
        except (*_INFRA_ERRORS, AgentInfraError) as e:
            self._infra_failure(item, now, e)
            return
        except Exception as e:  # any per-item error backs off and eventually parks (I1)
            log.exception("step failed for #%s", item.id)
            self._infra_failure(item, now, e)
            return
        new = apply_transition(item, res.transition)
        new = replace(new, usage=item.usage + res.usage, infra_failures=0,
                      pr_id=res.pr_id if res.pr_id is not None else item.pr_id,
                      data=_merge(new.data, {**res.data, "retry_after": None}))
        offset = int(new.data.get("budget_offset", 0))
        if new.stage is not Stage.PARKED and \
                new.usage.tokens - offset > self._t.limits.max_item_tokens:
            new = apply_transition(replace(new, stage=item.stage), park(
                ParkReason.BUDGET, f"Item used {new.usage.tokens - offset:,} tokens, over "
                f"the {self._t.limits.max_item_tokens:,} limit."))
        labels = list(res.labels)
        if new.stage is Stage.DONE:  # a merge approves the plan and review gates
            labels += self._approval_labels(item.id, (Stage.PLAN, Stage.REVIEW), "merged")
        events = list(res.events)
        if not (item.stage is Stage.AWAITING_HUMAN and new.stage is Stage.AWAITING_HUMAN):
            events.append(transition_event(item, new))
        if new.stage in (Stage.DONE, Stage.CLOSED):
            events.append(EventInput("outcome", {
                "result": "merged" if new.stage is Stage.DONE else "abandoned",
                "pr_id": new.pr_id}))
        self._store.commit_step(new, res.decisions, res.usage, now.date(), labels,
                                events=events, at=item)
        if new.stage is not item.stage:
            log.info("%s -> %s%s", item.stage.value, new.stage.value,
                     f" ({new.park_reason.value})" if new.park_reason else "")
        self._side_effects(new)
```

Replace `_infra_failure`:

```python
    def _infra_failure(self, item: Item, now: datetime, err: Exception) -> None:
        n = item.infra_failures + 1
        log.warning("infra failure %s on #%s: %s", n, item.id, err)
        events = _partial_events(getattr(err, "partial", None)) + [
            EventInput("infra_failure", {"n": n, "error": f"{type(err).__name__}: {err}"[:2000]})]
        if n >= _MAX_INFRA_FAILURES:
            new = apply_transition(replace(item, infra_failures=n),
                                   park(ParkReason.INFRA, f"{type(err).__name__}: {err}"))
            events.append(transition_event(item, new))
            self._store.save(new, events=events, at=item)
            self._side_effects(new)
            return
        retry = now + timedelta(minutes=2 ** n)
        self._store.save(replace(item, infra_failures=n,
                                 data={**item.data, "retry_after": retry.isoformat()}),
                         events=events, at=item)
```

Replace `_park_side_effects`:

```python
    def _park_side_effects(self, item: Item) -> None:
        """Tag first, and record that the tag is set, before commenting: only a confirmed tag
        makes its later removal mean "a human approved" (C1)."""
        try:
            self._ado.set_tag(item.id, self._t.ado.parked_tag, True)
        except _INFRA_ERRORS as e:
            log.exception("setting the parked tag failed for #%s; will retry", item.id)
            self._store.add_event("park_side_effect_failed",
                                  {"step": "tag", "error": str(e)[:500]}, item=item)
            return
        self._store.save(replace(item, data={**item.data, "parked_tag_set": True}),
                         events=[EventInput("park_tagged")], at=item)
        try:
            self._ado.comment_work_item(item.id, park_comment_html(item))
            if item.pr_id:
                self._ado.comment_pr(item.pr_id, f"agent-sdlc parked this item "
                                     f"({item.park_reason}): {item.data.get('park_note', '')}")
        except _INFRA_ERRORS as e:
            log.exception("park comments failed for #%s", item.id)
            self._store.add_event("park_side_effect_failed",
                                  {"step": "comment", "error": str(e)[:500]}, item=item)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler records events, liveness and stale items" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `trace` command, status additions, CLI wiring

**Files:**
- Create: `src/agent_sdlc/tracing.py`
- Modify: `src/agent_sdlc/cli.py`
- Modify: `tests/conftest.py` (autouse fixture isolating log/trace dirs; additive)
- Test: `tests/test_tracing.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Store event API (Task 2), `configure_logging` (Task 3), runner `max_denials` (Task 5), `StageExecutor(traces=)` (Task 8).
- Produces: `render_trace(store: Store, item_id: int, full: bool = False) -> str` (raises `KeyError` for an unknown item); CLI `trace <id> [--full]`; global options `--traces` (env `AGENT_SDLC_TRACES`) and `--logs` (env `AGENT_SDLC_LOGS`), both defaulting under `~/.agent-sdlc/`; `status` shows the last tick and, per item, last-event age, denial count and `STALE`; `run` sets flag `poll_s`; `pause`/`resume` write events; `requeue --local` writes a `requeue` event.

- [ ] **Step 1: Write the failing tests**

Add to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path_factory: pytest.TempPathFactory,
                         monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests must never write logs or traces under the real ~/.agent-sdlc."""
    base = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("AGENT_SDLC_LOGS", str(base / "logs"))
    monkeypatch.setenv("AGENT_SDLC_TRACES", str(base / "traces"))
```

Create `tests/test_tracing.py`:

```python
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
    it = store.get(9)
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
    out = render_trace(store, 9)
    assert out.splitlines()[0].startswith('#9 "Fix it"   stage: triage')
    assert "branch agent/9-fix-it" in out
    assert "DENIED     implementer Read [outside_worktree]" in out
    assert "31 turns 1,200 tok 1m05s 1 denied ESCALATED outside_worktree" in out
    assert "→ parked (policy): stopped" in out


def test_trace_item_without_events(store: Store) -> None:
    assert "(no events recorded)" in render_trace(store, 9)


def test_trace_unknown_item(store: Store) -> None:
    with pytest.raises(KeyError):
        render_trace(store, 404)


def test_trace_full_prints_tool_calls(store: Store, tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "tool_use", "name": "Read",
                             "input": {"file_path": "src/a.ts"}}) + "\n")
    store.add_event("agent_session", {"role": "planner", "transcript": str(t)},
                    item=store.get(9))
    out = render_trace(store, 9, full=True)
    assert 'Read {"file_path": "src/a.ts"}' in out
    t.unlink()
    assert "(transcript missing)" in render_trace(store, 9, full=True)
```

Append to `tests/test_cli.py`:

```python
import logging
from datetime import UTC, datetime, timedelta


def test_trace_command(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "agent/9-a")
    store.add_event("intake", {"branch": "agent/9-a"}, item=store.get(9))
    assert run(db, "trace", "9") == 0
    assert "branch agent/9-a" in capsys.readouterr().out
    assert run(db, "trace", "404") == 1
    assert "no item #404" in capsys.readouterr().out


def test_status_shows_last_tick_and_stale(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    now = datetime.now(UTC)
    store.set_flag("last_tick", (now - timedelta(minutes=10)).isoformat())
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    store.save(replace(store.get(9), stage=Stage.PLAN,
                       data={"denial_counts": {"planner": 2}}))
    store.add_event("intake", {}, item=store.get(9), ts=now - timedelta(hours=3))
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "last tick: 10m ago  LOOP NOT RUNNING?" in out
    assert "denied 2" in out and "STALE" in out


def test_pause_and_local_requeue_write_events(db: str) -> None:
    assert run(db, "pause") == 0
    store = Store(db)
    assert [e.kind for e in store.events_since(datetime(2000, 1, 1, tzinfo=UTC))] == ["pause"]
    store.add_item("rallysource", WorkItem(9, "Fix it", "", "", "Bug", (), "u"), "b")
    store.save(replace(store.get(9), stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))
    assert run(db, "requeue", "9", "--local") == 0
    [rq] = Store(db).events_for(9)
    assert rq.kind == "requeue" and rq.payload["to"] == "verify"


def test_cli_writes_log_file(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SDLC_LOGS", str(tmp_path / "logs"))
    assert run(db, "status") == 0
    logging.getLogger("agent_sdlc.cli_test").info("hello from test")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello from test" in (tmp_path / "logs" / "agent-sdlc.log").read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tracing.py tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_sdlc.tracing'` and argparse errors for `trace`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/tracing.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_sdlc.store import Event, Store

_PAD = " " * 31


def _local(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _dur(ms: Any) -> str:
    s = int(ms or 0) // 1000
    return f"{s // 60}m{s % 60:02d}s"


def _compact(obj: Any, limit: int = 160) -> str:
    text = json.dumps(obj, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _event_text(e: Event) -> str:
    p = e.payload
    if e.kind == "intake":
        return f"intake     branch {p.get('branch')}"
    if e.kind == "transition":
        text = f"→ {p.get('to')}"
        if p.get("park_reason"):
            text += f" ({p['park_reason']})"
        first = str(p.get("note") or "").splitlines()
        return text + (f": {first[0][:120]}" if first else "")
    if e.kind == "agent_session":
        tokens = int(p.get("input_tokens", 0)) + int(p.get("output_tokens", 0))
        text = (f"agent      {p.get('role')} {p.get('turns', 0)} turns {tokens:,} tok "
                f"{_dur(p.get('duration_ms'))} {p.get('denials', 0)} denied")
        if p.get("escalated"):
            text += f" ESCALATED {p['escalated']}"
        if p.get("is_error"):
            text += f" ERROR {p.get('subtype')}"
        if p.get("transcript"):
            text += f"\n{_PAD}{p['transcript']}"
        return text
    if e.kind == "tool_denied":
        return (f"DENIED     {p.get('role')} {p.get('tool')} [{p.get('category')}]: "
                f"{p.get('reason')}")
    if e.kind == "check":
        text = f"check      {p.get('name')} exit {p.get('exit_code')} ({p.get('duration_s')}s)"
        return text + (f"\n{_PAD}{p['log']}" if p.get("log") else "")
    if e.kind == "infra_failure":
        return f"infra      failure {p.get('n')}: {p.get('error')}"
    if e.kind == "usage_limit":
        return f"usage limit, paused until {p.get('until')}"
    if e.kind == "requeue":
        return (f"requeue    {p.get('from_reason')} → {p.get('to')}"
                + (" (approved)" if p.get("approved") else ""))
    if e.kind == "pr_comment":
        return f"PR comment {p.get('author')}: {p.get('intent')}"
    if e.kind == "outcome":
        return f"outcome    {p.get('result')}"
    return f"{e.kind} {_compact(p)}"


def _transcript_lines(path: str) -> list[str]:
    try:
        raw = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [f"{_PAD}  (transcript missing)"]
    out: list[str] = []
    for line in raw:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "tool_use":
            out.append(f"{_PAD}  {obj.get('name')} {_compact(obj.get('input'))}")
        elif obj.get("type") == "denied":
            out.append(f"{_PAD}  DENIED {obj.get('tool')}: {obj.get('reason')}")
    return out


def render_trace(store: Store, item_id: int, full: bool = False) -> str:
    """Chronological timeline of an item's events and Laya decisions (spec §6.1)."""
    item = store.get(item_id)
    head = f'#{item.id} "{item.title}"   stage: {item.stage.value}'
    if item.park_reason:
        src = item.parked_from.value if item.parked_from else "?"
        head += f" ({item.park_reason.value} from {src})"
    rows: list[tuple[datetime, int, str]] = []
    for e in store.events_for(item_id):
        text = _event_text(e)
        if full and e.kind == "agent_session" and e.payload.get("transcript"):
            text += "\n" + "\n".join(_transcript_lines(str(e.payload["transcript"])))
        rows.append((e.ts, 1, f"{_local(e.ts)}  {e.stage or '-':<10} {text}"))
    for ts, d in store.decisions_with_ts(item_id):
        flag = " shadow" if d.shadow else ""
        rows.append((ts, 0, f"{_local(ts)}  {d.gate:<10} decision   "
                            f"{d.question}={d.answer} {d.confidence:.2f}{flag}"))
    if not rows:
        return head + "\n(no events recorded)"
    rows.sort(key=lambda r: (r[0], r[1]))
    return "\n".join([head, *(r[2] for r in rows)])
```

Replace `src/agent_sdlc/cli.py` with the following (unchanged parts are included so the file is complete):

```python
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_sdlc.decisions.gates import GATES
from agent_sdlc.labeling import calibrate_question, label_logged, label_triage
from agent_sdlc.logctx import configure_logging
from agent_sdlc.orchestrator.transitions import requeue
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig, load_target
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import ACTIVE_STAGES, EventInput

_STATE = Path("~/.agent-sdlc").expanduser()
_DEFAULT_DB = f"sqlite:///{_STATE / 'state.db'}"
# Defaults resolve from the project root, not the CWD (M12).
_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sdlc")
    p.add_argument("--target", default=os.environ.get(
        "AGENT_SDLC_TARGET", str(_ROOT / "targets" / "rallysource.yaml")))
    p.add_argument("--db", default=os.environ.get("AGENT_SDLC_DB", _DEFAULT_DB))
    p.add_argument("--workspaces", default=str(_ROOT / "workspaces"))
    p.add_argument("--traces", default=os.environ.get("AGENT_SDLC_TRACES",
                                                      str(_STATE / "traces")))
    p.add_argument("--logs", default=os.environ.get("AGENT_SDLC_LOGS", str(_STATE / "logs")))
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run-push", action="store_true")
    run.add_argument("--poll", type=int, default=60)
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("status")
    rq = sub.add_parser("requeue")
    rq.add_argument("item_id", type=int)
    rq.add_argument("--local", action="store_true", help="do not touch ADO tags")
    tr = sub.add_parser("trace")
    tr.add_argument("item_id", type=int)
    tr.add_argument("--full", action="store_true", help="also print each session's tool calls")
    lab = sub.add_parser("label")
    lab.add_argument("gate", choices=sorted(GATES))
    lab.add_argument("--limit", type=int, default=20)
    cal = sub.add_parser("calibrate")
    cal.add_argument("gate", nargs="?", choices=sorted(GATES))
    cal.add_argument("--promote", action="store_true")
    return p


def _store(url: str) -> Store:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return Store(url)


def _runtime(target: TargetConfig, store: Store, workspaces: Path, dry_run_push: bool,
             traces: Path | None = None) -> tuple[Any, Any, Any]:
    from agent_sdlc.adapters.ado import AdoClient
    from agent_sdlc.agents.runner import ClaudeAgentRunner
    from agent_sdlc.decisions.decider import Decider, LayaPredictor
    from agent_sdlc.orchestrator.scheduler import Scheduler
    from agent_sdlc.orchestrator.stages import StageExecutor
    from agent_sdlc.policy import CommandPolicy, PathPolicy
    from agent_sdlc.secrets import (
        ADO_PAT,
        ANTHROPIC_KEY,
        CLAUDE_TOKEN,
        basic_auth_header,
        get_secret,
    )
    from agent_sdlc.workspaces import Workspaces

    pat = get_secret(*ADO_PAT)
    if target.auth.mode == "subscription":
        auth_env = {"CLAUDE_CODE_OAUTH_TOKEN": get_secret(*CLAUDE_TOKEN)}
    else:
        auth_env = {"ANTHROPIC_API_KEY": get_secret(*ANTHROPIC_KEY)}
    ado = AdoClient(target.ado, pat, dry_run_push=dry_run_push)
    ws = Workspaces(workspaces.resolve(), target, git_auth_header=basic_auth_header(pat))
    pp = PathPolicy(target.policy.protected_paths)
    cp = CommandPolicy([target.repo.install, *target.repo.commands.values()])
    runner = ClaudeAgentRunner(pp, cp, Path("~/.agent-sdlc/claude-config").expanduser(), auth_env,
                               should_stop=lambda: store.get_flag("paused") == "1", home=ws.home,
                               max_denials=target.limits.max_denials_per_session)
    decider = Decider(LayaPredictor(target.laya.model), store.calibration,
                      target.laya.default_threshold)
    executor = StageExecutor(target=target, ado=ado, decider=decider, runner=runner,
                             workspaces=ws, path_policy=pp, decisions_for=store.decisions_for,
                             traces=traces)
    scheduler = Scheduler(target=target, store=store, executor=executor, ado=ado, workspaces=ws)
    return scheduler, ado, decider


def _ago(delta: timedelta) -> str:
    s = max(int(delta.total_seconds()), 0)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _status(target: TargetConfig, store: Store) -> None:
    now = datetime.now(UTC)
    paused = "yes" if store.get_flag("paused") == "1" else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"target: {target.name}  paused: {paused}  paused_until: {until}")
    tick = store.get_flag("last_tick")
    if tick is None:
        print("last tick: never  LOOP NOT RUNNING?")
    else:
        age = now - datetime.fromisoformat(tick)
        poll = int(store.get_flag("poll_s") or 60)
        warn = "  LOOP NOT RUNNING?" if age > timedelta(seconds=3 * poll) else ""
        print(f"last tick: {_ago(age)} ago{warn}")
    print(f"today: {today.turns} turns, {today.tokens:,} tokens")
    stale_after = timedelta(minutes=target.limits.stale_after_minutes)
    for i in store.items(target.name):
        reason = f" ({i.park_reason.value} from {i.parked_from.value})" \
            if i.park_reason and i.parked_from else ""
        pr = f" PR !{i.pr_id}" if i.pr_id else ""
        last = store.last_event_ts(i.id)
        seen = f"last {_ago(now - last)} ago" if last else "no events"
        denied = sum((i.data.get("denial_counts") or {}).values())
        stale = " STALE" if last and i.stage in ACTIVE_STAGES and now - last > stale_after \
            else ""
        print(f"#{i.id:<6} {i.stage.value:<15}{reason}{pr}  attempt {i.attempt}  "
              f"{i.usage.tokens:,} tok  {seen}  denied {denied}{stale}  {i.title[:60]}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(Path(args.logs))
    target = load_target(Path(args.target))
    store = _store(args.db)
    traces = Path(args.traces)

    if args.cmd == "pause":
        store.set_flag("paused", "1")
        store.add_event("pause")
    elif args.cmd == "resume":
        store.set_flag("paused", None)
        store.set_flag("paused_until", None)
        store.add_event("resume")
    elif args.cmd == "status":
        _status(target, store)
    elif args.cmd == "trace":
        try:
            print(render_trace(store, args.item_id, args.full))
        except KeyError:
            print(f"no item #{args.item_id}")
            return 1
    elif args.cmd == "requeue":
        if args.local:
            item = store.get(args.item_id)
            new = requeue(item)
            store.save(new, events=[EventInput("requeue", {
                "from_reason": item.park_reason.value if item.park_reason else None,
                "to": new.stage.value, "approved": False, "local": True})], at=item)
        else:
            scheduler, *_ = _runtime(target, store, Path(args.workspaces), False, traces)
            scheduler.requeue_item(args.item_id)
    elif args.cmd == "calibrate":
        gates = [args.gate] if args.gate else sorted(GATES)
        for gate in gates:
            for q in GATES[gate]:
                r = calibrate_question(store, gate, q, target.laya.max_ece, args.promote)
                print(f"{gate}.{q}: n={r.n} T={r.temperature:.3f} ECE={r.ece:.3f} "
                      f"acc={r.accuracy:.3f} mode={r.mode} — {r.message}")
    elif args.cmd == "label":
        if args.gate == "triage":
            _, ado, decider = _runtime(target, store, Path(args.workspaces), True, traces)
            n = label_triage(ado, decider, store, args.limit, input)
        else:
            n = label_logged(store, args.gate, args.limit, input)
        print(f"recorded {n} labels")
    elif args.cmd == "run":
        store.set_flag("poll_s", str(args.poll))
        scheduler, *_ = _runtime(target, store, Path(args.workspaces), args.dry_run_push,
                                 traces)
        if args.once:
            asyncio.run(scheduler.tick())
        else:
            asyncio.run(scheduler.run_forever(args.poll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Compared with the current `cli.py`, `_runtime` changes only its signature (`traces`) and the `runner`/`executor` constructor arguments; before replacing the file, diff the lazy-import block against the current one and keep any line added since this plan was written. `import logging` is removed from `cli.py`, because `configure_logging` replaces `basicConfig`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass. The existing `test_status_lists_items` still passes (it checks `#9`, `triage`, `paused: no`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/tracing.py src/agent_sdlc/cli.py tests/conftest.py tests/test_tracing.py tests/test_cli.py
git commit -m "feat: trace command, loop liveness and stale items in status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: `metrics` command

**Files:**
- Create: `src/agent_sdlc/metrics.py`
- Modify: `src/agent_sdlc/cli.py` (subcommand)
- Test: `tests/test_metrics.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store.events_since`, `Store.items`, `Store.calibration`, `Store.labels`, `Store.labeled_decisions` (Task 2); `GATES`.
- Produces: `render_metrics(store: Store, target: str, since: datetime, now: datetime) -> str`; CLI `metrics [--days N]` (default 30). Sections: Outcomes, Parks, Effort, Latency, Denials, Gates. The v1 events table is single-target, so events are not filtered by target.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metrics.py`:

```python
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
```

Append to `tests/test_cli.py`:

```python
def test_metrics_command(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "metrics", "--days", "7") == 0
    assert "Outcomes" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py tests/test_cli.py::test_metrics_command -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_sdlc.metrics'`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/metrics.py`:

```python
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from statistics import median

from agent_sdlc.decisions.gates import GATES
from agent_sdlc.store import Event, Store
from agent_sdlc.types import ACTIVE_STAGES, Stage

_NOUL = {"yes": "true", "no": "false"}


def _p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def _dist(xs: list[float], fmt: Callable[[float], str]) -> str:
    if not xs:
        return "n/a"
    return f"median {fmt(median(xs))} · p90 {fmt(_p90(xs))} (n={len(xs)})"


def _hours(s: float) -> str:
    return f"{s / 3600:.1f}h"


def _tok(x: float) -> str:
    return f"{x:,.0f}"


def _counts(c: Counter[str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common()) or "none"


def render_metrics(store: Store, target: str, since: datetime, now: datetime) -> str:
    """Pipeline report over events in [since, now] (spec §6.3)."""
    kind: dict[str, list[Event]] = defaultdict(list)
    for e in store.events_since(since):
        if e.ts <= now:
            kind[e.kind].append(e)
    items = store.items(target)
    transitions = kind["transition"]
    out = [f"agent-sdlc metrics · {target} · {since:%Y-%m-%d} to {now:%Y-%m-%d}"]

    results = Counter(str(e.payload.get("result")) for e in kind["outcome"])
    merged, abandoned = results["merged"], results["abandoned"]
    rate = f"{merged / (merged + abandoned):.0%}" if merged + abandoned else "n/a"
    parked_now = sum(i.stage is Stage.PARKED for i in items)
    flight = sum(i.stage in (*ACTIVE_STAGES, Stage.AWAITING_HUMAN) for i in items)
    out += ["", "Outcomes",
            f"  taken in {len(kind['intake'])} · merged {merged} · abandoned {abandoned} · "
            f"parked now {parked_now} · in flight {flight} · merge rate {rate}"]

    parks = [e for e in transitions if e.payload.get("to") == "parked"]
    requeued = len(kind["requeue"])
    share = f"{requeued / len(parks):.0%}" if parks else "n/a"
    out += ["", "Parks", f"  total {len(parks)} · requeued {requeued} ({share})",
            "  by reason: " + _counts(Counter(str(e.payload.get("park_reason")) for e in parks)),
            "  by stage: " + _counts(Counter(str(e.stage) for e in parks))]

    def moves(src: set[str], dst: str) -> int:
        return sum(1 for e in transitions
                   if e.payload.get("from") in src and e.payload.get("to") == dst)

    per_item: dict[int, float] = defaultdict(float)
    per_stage: dict[str, float] = defaultdict(float)
    inp = cache = 0
    cost = 0.0
    for e in kind["agent_session"]:
        p = e.payload
        tokens = int(p.get("input_tokens", 0)) + int(p.get("output_tokens", 0))
        if e.item_id is not None:
            per_item[e.item_id] += tokens
        per_stage[str(e.stage)] += tokens
        inp += int(p.get("input_tokens", 0))
        cache += int(p.get("cache_read_tokens", 0))
        cost += float(p.get("cost_usd") or 0)
    cache_share = f"{cache / (cache + inp):.0%}" if cache + inp else "n/a"
    out += ["", "Effort",
            f"  verify/review retries {moves({'verify', 'review'}, 'implement')} · "
            f"PR rounds {moves({'awaiting_human'}, 'implement')} · "
            f"replans {moves({'plan'}, 'plan')}",
            f"  tokens per item: {_dist(list(per_item.values()), _tok)}",
            "  tokens by stage: " + (", ".join(f"{s} {_tok(v)}"
                                               for s, v in sorted(per_stage.items())) or "none"),
            f"  cache-read share {cache_share} · cost ${cost:,.2f} (where reported)"]

    intake_at = {e.item_id: e.ts for e in kind["intake"] if e.item_id is not None}
    pr_at: dict[int, datetime] = {}
    for e in transitions:
        if e.payload.get("to") == "awaiting_human" and e.item_id is not None:
            pr_at.setdefault(e.item_id, e.ts)
    to_pr = [(pr_at[i] - intake_at[i]).total_seconds() for i in pr_at if i in intake_at]
    to_merge = [(e.ts - pr_at[e.item_id]).total_seconds() for e in kind["outcome"]
                if e.payload.get("result") == "merged" and e.item_id in pr_at]
    out += ["", "Latency", f"  intake → PR open: {_dist(to_pr, _hours)}",
            f"  PR open → merged: {_dist(to_merge, _hours)}"]

    denials = Counter(f"{e.payload.get('role')} {e.payload.get('category')}"
                      for e in kind["tool_denied"])
    escalations = sum(1 for e in kind["agent_session"] if e.payload.get("escalated"))
    out += ["", "Denials", f"  total {sum(denials.values())} · escalations {escalations}"]
    out += [f"  {k}: {v}" for k, v in denials.most_common()]

    out += ["", "Gates"]
    for gate, questions in GATES.items():
        for q in questions:
            cal = store.calibration(gate, q)
            pairs = store.labeled_decisions(gate, q)
            agree = sum(_NOUL.get(a, a) == g for a, g in pairs)
            agreement = f"{agree / len(pairs):.0%}" if pairs else "n/a"
            ece = f"{cal.ece:.3f}" if cal and cal.ece is not None else "n/a"
            mode = cal.mode if cal else "shadow"
            out.append(f"  {gate}.{q}: mode {mode} · labels {len(store.labels(gate, q))} · "
                       f"ECE {ece} · agreement {agreement} (n={len(pairs)})")
    return "\n".join(out)
```

`src/agent_sdlc/cli.py`: import `from agent_sdlc.metrics import render_metrics`; in `_parser` add

```python
    me = sub.add_parser("metrics")
    me.add_argument("--days", type=int, default=30)
```

and in `main` add the branch

```python
    elif args.cmd == "metrics":
        now = datetime.now(UTC)
        print(render_metrics(store, target.name, now - timedelta(days=args.days), now))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/metrics.py src/agent_sdlc/cli.py tests/test_metrics.py tests/test_cli.py
git commit -m "feat: metrics report over the event log" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Label decisions from abandoned PRs

**Files:**
- Modify: `src/agent_sdlc/labeling.py`, `src/agent_sdlc/cli.py`
- Test: `tests/test_labeling.py`

**Interfaces:**
- Consumes: `Store.abandoned_item_ids`, `Store.unlabeled_decisions_for_items`, `Store.decision_states` (Task 2); `outcome` events (Task 9).
- Produces: `label_logged(store, gate, limit, ask, abandoned_only: bool = False) -> int`; CLI `label <gate> --abandoned` (uses logged decisions for every gate, triage included).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_labeling.py`:

```python
from datetime import date

import pytest

from agent_sdlc.types import Usage
from tests.fakes import decision


def test_label_abandoned_only_shows_abandoned_items(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store("sqlite://")
    for i in (1, 2):
        store.add_item("t", WorkItem(i, f"W{i}", "", "", "Bug", (), "u"), f"b{i}")
        store.commit_step(store.get(i), [(decision("review", "review_blocking", "no"),
                                          {"review_notes": f"notes {i}"})],
                          Usage(), date(2026, 10, 1), [])
    store.commit_step(store.get(1), [(decision("comment", "comment_intent", "question"),
                                      {"comment": "this is obsolete, closing"})],
                      Usage(), date(2026, 10, 1), [])
    store.add_event("outcome", {"result": "abandoned"}, item=store.get(1))
    n = label_logged(store, "review", 10, lambda _: "true", abandoned_only=True)
    out = capsys.readouterr().out
    assert n == 1
    assert "item #1 (PR abandoned)" in out and "this is obsolete" in out
    assert "notes 1" in out and "notes 2" not in out
    assert [g for _, g in store.labels("review", "review_blocking")] == ["true"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_labeling.py -v`
Expected: FAIL with `TypeError: label_logged() got an unexpected keyword argument 'abandoned_only'`.

- [ ] **Step 3: Implement**

In `src/agent_sdlc/labeling.py`, replace `label_logged` with:

```python
def _label_one(store: Store, gate: str, decision_id: int, d: Decision, state: dict[str, Any],
               ask: Callable[[str], str]) -> bool:
    print(f"\n=== decision {decision_id} ({gate}.{d.question}) ===")
    for k, v in state.items():
        print(f"--- {k} ---\n{str(v)[:1500]}")
    gold = _ask_gold(ask, d.question, option_keys(GATES[gate][d.question]), d.answer)
    if gold is None:
        return False
    store.add_label(LabelInput(gate, d.question, d.raw_probs, gold, "manual", decision_id))
    return True


def label_logged(store: Store, gate: str, limit: int, ask: Callable[[str], str],
                 abandoned_only: bool = False) -> int:
    """Label logged decisions. With abandoned_only, only decisions from items whose PR was
    abandoned, each item introduced by its logged PR comments (spec §7.1)."""
    if not abandoned_only:
        return sum(_label_one(store, gate, decision_id, d, state, ask)
                   for decision_id, d, state in store.unlabeled_decisions(gate, limit))
    count = 0
    shown: set[int] = set()
    rows = store.unlabeled_decisions_for_items(gate, store.abandoned_item_ids(), limit)
    for decision_id, item_id, d, state in rows:
        if item_id not in shown:
            shown.add(item_id)
            print(f"\n##### item #{item_id} (PR abandoned) #####")
            for st in store.decision_states(item_id, "comment"):
                print(f"--- PR comment ---\n{str(st.get('comment', ''))[:1500]}")
        count += _label_one(store, gate, decision_id, d, state, ask)
    return count
```

Update the imports in `labeling.py`: `from typing import Any`; add `Decision` to the types import (`from agent_sdlc.types import Calibration, Decision`).

In `src/agent_sdlc/cli.py`, add to the `label` parser:

```python
    lab.add_argument("--abandoned", action="store_true",
                     help="only decisions from items whose PR was abandoned")
```

and change the `label` branch in `main` to:

```python
    elif args.cmd == "label":
        if args.gate == "triage" and not args.abandoned:
            _, ado, decider = _runtime(target, store, Path(args.workspaces), True, traces)
            n = label_triage(ado, decider, store, args.limit, input)
        else:
            n = label_logged(store, args.gate, args.limit, input, abandoned_only=args.abandoned)
        print(f"recorded {n} labels")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass (the existing `label_logged` tests are unchanged in behavior).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/labeling.py src/agent_sdlc/cli.py tests/test_labeling.py
git commit -m "feat: label decisions from abandoned PRs first" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: End-to-end coverage, slow test, README

**Files:**
- Modify: `tests/e2e/test_pipeline.py` (`Env` gains an optional `traces` kwarg; new tests)
- Modify: `tests/slow/test_real_agent_policy.py` (new test)
- Modify: `README.md`
- Export: `/Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/README.md` if a copy exists there (user rule: keep Obsidian copies in sync)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the e2e tests**

In `tests/e2e/test_pipeline.py`, change `Env.__init__` to accept `traces: Path | None = None` and pass `traces=traces` to `StageExecutor`:

```python
class Env:
    def __init__(self, tmp_path: Path, target: TargetConfig, origin: Path,
                 traces: Path | None = None) -> None:
        self.store = Store("sqlite://")
        self.ado = FakeAdo(origin=origin)
        self.ado.add(WI)
        self.ws = Workspaces(tmp_path / "ws", target)
        self.decider = FakeDecider()
        self.runner = FakeRunner()
        self.origin = origin
        executor = StageExecutor(target=target, ado=self.ado, decider=self.decider,
                                 runner=self.runner, workspaces=self.ws,
                                 path_policy=PathPolicy(target.policy.protected_paths),
                                 decisions_for=self.store.decisions_for, traces=traces)
        self.sched = Scheduler(target=target, store=self.store, executor=executor, ado=self.ado,
                               workspaces=self.ws, clock=lambda: NOW)
```

Append:

```python
from agent_sdlc.labeling import label_logged
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import Denial


async def test_escalated_agent_parks_with_trace(
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    env = Env(tmp_path, target, origin_repo, traces=tmp_path / "traces")

    def escape(role, prompt, cwd):  # type: ignore[no-untyped-def]
        return AgentResult("", Usage(2, 50, 5), (Denial(
            "Read", "outside_worktree", "path is outside the worktree",
            '{"file_path": "/Users/x/.ssh/config"}'),), escalated="outside_worktree")

    env.runner.behaviors["implementer"] = escape
    await env.ticks(3)  # triage, plan, implement
    assert env.item.stage is Stage.PARKED and env.item.park_reason is ParkReason.POLICY
    assert any("Blocked tool calls" in c and ".ssh/config" in c for _, c in env.ado.wi_comments)
    kinds = [e.kind for e in env.store.events_for(5)]
    assert "tool_denied" in kinds and kinds[-1] == "park_tagged"
    out = render_trace(env.store, 5)
    assert "ESCALATED outside_worktree" in out and "→ parked (policy)" in out
    assert env.runner.traces[1] is not None and env.runner.traces[1].parent == (
        tmp_path / "traces" / "5")


async def test_manifest_change_needs_approval_then_reinstalls(
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    mt = target.model_copy(update={"policy": target.policy.model_copy(
        update={"manifest_paths": ["**/package.json"]})})
    env = Env(tmp_path, mt, origin_repo)
    calls = {"n": 0}

    def work(role, prompt, cwd):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            (cwd / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        else:
            (cwd / "docs.txt").write_text(f"round {calls['n']}\n")
        return AgentResult("done", Usage(1, 10, 10))

    env.runner.behaviors["implementer"] = work
    await env.ticks(3)  # triage, plan, implement
    assert env.item.park_reason is ParkReason.MANIFEST
    assert any("left-pad" in c for _, c in env.ado.wi_comments)
    env.ado.set_tag(5, "agent:parked", False)
    await env.ticks(1)  # requeue -> verify runs in the same tick
    assert env.item.stage is Stage.REVIEW
    installs = [e for e in env.store.events_for(5)
                if e.kind == "check" and e.payload["name"] == "install"]
    assert len(installs) == 2  # before implement, and after the approved manifest change
    await env.ticks(2)  # review, pr_open
    assert env.item.stage is Stage.AWAITING_HUMAN
    env.ado.pr_threads[100].append(PrComment(1, 1, "Brian", "/agent add docs"))
    await env.ticks(2)  # awaiting -> implement (same tick), verify
    assert env.item.stage is Stage.REVIEW  # unchanged manifest digest: no second park


async def test_abandoned_pr_records_outcome_for_labeling(env: Env) -> None:
    await env.ticks(6)
    env.ado.prs[100]["status"] = "abandoned"
    await env.ticks(1)
    assert env.item.stage is Stage.CLOSED
    assert [e.payload["result"] for e in env.store.events_for(5) if e.kind == "outcome"] == [
        "abandoned"]
    assert env.store.abandoned_item_ids() == {5}
    assert label_logged(env.store, "review", 10, lambda _: "true", abandoned_only=True) == 1
```

- [ ] **Step 2: Run the e2e tests**

Run: `uv run pytest tests/e2e -v`
Expected: PASS. If a tick count is off by one, trace the stage sequence with `render_trace(env.store, 5)` printed in the failing test, and fix the tick count in the new test only.

- [ ] **Step 3: Add the slow test**

Append to `tests/slow/test_real_agent_policy.py`:

```python
import json


@pytest.mark.slow
def test_real_agent_outside_worktree_read_escalates(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    trace = tmp_path / "t.jsonl"
    runner = ClaudeAgentRunner(PathPolicy(["infra/**"]), CommandPolicy([]), tmp_path / "cfg",
                               {"CLAUDE_CODE_OAUTH_TOKEN": get_secret(*CLAUDE_TOKEN)})
    result = asyncio.run(runner.run(
        IMPLEMENTER, "Use the Read tool on the absolute path /etc/hosts and summarize it.",
        wt, max_turns=6, trace=trace))
    assert result.escalated == "outside_worktree"
    types_ = {json.loads(line)["type"] for line in trace.read_text().splitlines()}
    assert {"prompt", "tool_use", "denied"} <= types_
```

Run once the Claude token exists: `uv run pytest -m slow tests/slow/test_real_agent_policy.py -v`. If the token isn't set up yet, record that as not run; don't claim it passed.

- [ ] **Step 4: Update README**

In `README.md`, under `## Everyday use`, extend the command block and add two sections after it:

````markdown
```bash
uv run agent-sdlc trace <id> [--full]          # timeline: transitions, sessions, denials, checks
uv run agent-sdlc metrics [--days 30]          # outcomes, parks, effort, latency, denials, gates
```

`status` also shows when the loop last ticked (`LOOP NOT RUNNING?` after 3 missed polls) and
marks items with no event for `limits.stale_after_minutes` as `STALE`.

### Traces and logs

- `~/.agent-sdlc/traces/<id>/` holds one JSONL transcript per agent session (every tool call,
  result and blocked call) and the full output of every install/verify command. Files are
  `0600`. Override with `--traces` or `AGENT_SDLC_TRACES`.
- `~/.agent-sdlc/logs/agent-sdlc.log` is the rotating log (14 days), each line tagged
  `[#<id> <stage>]`. Override with `--logs` or `AGENT_SDLC_LOGS`.

### Parks you will see from the guardrails

- `policy` with "stopped after blocked tool calls": an agent tried to read outside its worktree
  or write a protected path, or hit `limits.max_denials_per_session` blocked calls. The comment
  quotes the attempts.
- `manifest`: the change edits `package.json` or a lockfile. Review the diff in the comment;
  removing the tag approves exactly that change, and install and verify run with it. A later
  different change parks again.
- `budget` mid-session: the agent was stopped when the item's token budget ran out.

Tooling config that verify executes (eslint/vite/postcss/tailwind config, `turbo.json`,
`.npmrc`, `nest-cli.json`, `packages/eslint-config/**`) is protected. Agent-written source and
test code still runs on the host during verify. Container isolation is a separate follow-up
and must be in place before unattended runs.

To label decisions from abandoned PRs first: `uv run agent-sdlc label review --abandoned`.
````

- [ ] **Step 5: Full definition-of-done run**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: all pass. Report the slow test separately as run or not run.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/test_pipeline.py tests/slow/test_real_agent_policy.py README.md
git commit -m "test: e2e coverage for escalation, manifest gate and abandoned PRs; docs" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage map

| Spec section | Task |
|---|---|
| §3 event log, kinds, same-transaction writes | 2, 8, 9, 10 (CLI events) |
| §4.1 transcripts, session metadata, partials | 4, 5 |
| §4.2 full command output | 6, 8 |
| §4.3 logging | 3, 9, 10 |
| §5.1 protected tooling config | 6 |
| §5.2 manifest gate, install by digest, never install unapproved | 6, 7, 8 |
| §5.3 denial categories, escalation, surfacing | 1, 5, 7, 8 |
| §5.4 mid-session token budget | 5, 8 |
| §6.1 `trace` | 10 |
| §6.2 `status` last tick, stale | 9, 10 |
| §6.3 `metrics` | 11 |
| §7.1 abandoned → outcome + `label --abandoned` | 9, 12 |
| §9 error handling (trace write failures, partial on errors) | 4, 5, 6 |
| §10 testing (unit, e2e, slow) | all, 13 |
