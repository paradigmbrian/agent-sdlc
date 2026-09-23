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
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.turns + other.turns,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {"turns": self.turns, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Usage:
        d = d or {}
        return cls(int(d.get("turns", 0)), int(d.get("input_tokens", 0)),
                   int(d.get("output_tokens", 0)))


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


@dataclass(frozen=True)
class AgentResult:
    text: str
    usage: Usage
    denied: tuple[str, ...] = ()
    is_error: bool = False


class UsageLimitError(Exception):
    """The model provider refused work because a usage/rate limit window is exhausted."""

    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class AgentInterrupted(Exception):
    """The kill switch interrupted an agent session; the stage should be retried later."""
