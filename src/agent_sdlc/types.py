from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal


class Stage(StrEnum):
    TRIAGE = "triage"
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW = "review"
    PR_OPEN = "pr_open"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"
    CLOSED = "closed"
    PARKED = "parked"


# Stages that do work (and may use agents); AWAITING_HUMAN only polls.
ACTIVE_STAGES = (
    Stage.TRIAGE, Stage.PLAN, Stage.IMPLEMENT, Stage.VERIFY, Stage.REVIEW, Stage.PR_OPEN,
)


class ParkReason(StrEnum):
    NEEDS_HUMAN = "needs_human"
    PLAN_REJECTED = "plan_rejected"
    POLICY = "policy"
    RED = "red"
    PR_ROUNDS = "pr_rounds"
    BUDGET = "budget"
    INFRA = "infra"
    AGENT_ERROR = "agent_error"  # an agent session failed; not a gate park, requeue retries
    MANIFEST = "manifest"  # dependency manifests changed; a human approves them before install


# Parks caused by a Laya gate; a human re-queue means "approved, proceed past the gate".
GATE_PARKS = frozenset({ParkReason.NEEDS_HUMAN, ParkReason.PLAN_REJECTED})


@dataclass(frozen=True)
class WorkItem:
    id: int
    title: str
    description: str
    acceptance_criteria: str
    work_item_type: str
    tags: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class Usage:
    turns: int = 0
    input_tokens: int = 0         # uncached input + cache creation
    output_tokens: int = 0
    cache_read_tokens: int = 0    # reported separately; not counted against budgets

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.turns + other.turns,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def tokens(self) -> int:
        """Budgeted tokens: input (incl. cache creation) + output, excluding cache reads."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {"turns": self.turns, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cache_read_tokens": self.cache_read_tokens}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Usage:
        d = d or {}
        return cls(int(d.get("turns", 0)), int(d.get("input_tokens", 0)),
                   int(d.get("output_tokens", 0)), int(d.get("cache_read_tokens", 0)))


@dataclass(frozen=True)
class Decision:
    gate: str
    question: str
    answer: str                    # option key; for noul: "yes" | "no" | "unknown"
    probs: dict[str, float]        # after temperature
    raw_probs: dict[str, float]    # as normalized from Laya, before temperature (for labels)
    confidence: float
    shadow: bool
    actionable: bool               # active gate, confident, and not "unknown"


@dataclass(frozen=True)
class Calibration:
    temperature: float = 1.0
    threshold: float = 0.8
    mode: Literal["shadow", "active"] = "shadow"
    ece: float | None = None
    n: int = 0


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


@dataclass(frozen=True)
class PrComment:
    thread_id: int
    comment_id: int
    author: str
    content: str

    @property
    def key(self) -> str:
        return f"{self.thread_id}:{self.comment_id}"


@dataclass(frozen=True)
class Item:
    id: int
    target: str
    title: str
    branch: str
    stage: Stage
    park_reason: ParkReason | None = None
    parked_from: Stage | None = None
    attempt: int = 0
    replans: int = 0
    pr_rounds: int = 0
    infra_failures: int = 0
    pr_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    usage: Usage = Usage()


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

    def __init__(self, message: str, partial: AgentResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class AgentInfraError(Exception):
    """The agent SDK/CLI failed (connection, process or missing result); retried with backoff."""

    def __init__(self, message: str, partial: AgentResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial
