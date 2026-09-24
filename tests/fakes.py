from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_sdlc.agents.roles import Role
from agent_sdlc.decisions.gates import GATES, option_keys
from agent_sdlc.types import AgentResult, Decision, PrComment, Usage, WorkItem


def decision(gate: str, q: str, answer: str, actionable: bool = True) -> Decision:
    keys = option_keys(GATES[gate][q])
    probs = {k: 0.0 for k in keys}
    probs["true" if answer == "yes" else "false" if answer == "no" else answer] = 1.0
    return Decision(gate, q, answer, probs, probs, 0.95, not actionable, actionable)


GOOD: dict[str, dict[str, str]] = {
    "triage": {"kind": "bug", "clarity": "clear", "touches_protected": "no", "size": "small"},
    "plan": {"plan_addresses_item": "yes", "plan_scope_ok": "yes"},
    "review": {"review_blocking": "no", "risk": "low"},
    "comment": {"comment_intent": "change_request"},
}


@dataclass
class FakeDecider:
    answers: dict[str, dict[str, str]] = field(default_factory=lambda: {
        g: dict(a) for g, a in GOOD.items()})
    shadow: set[str] = field(default_factory=set)   # gates in shadow mode
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def decide(self, gate: str, state: dict[str, Any]) -> dict[str, Decision]:
        self.calls.append((gate, state))
        return {q: decision(gate, q, a, actionable=gate not in self.shadow)
                for q, a in self.answers[gate].items()}


Behavior = Callable[[Role, str, Path], Awaitable[AgentResult] | AgentResult]


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


@dataclass
class FakeAdo:
    origin: Path | None = None                         # local bare repo to push into
    items: dict[int, WorkItem] = field(default_factory=dict)
    tags: dict[int, set[str]] = field(default_factory=dict)
    wi_comments: list[tuple[int, str]] = field(default_factory=list)
    prs: dict[int, dict[str, Any]] = field(default_factory=dict)
    pr_threads: dict[int, list[PrComment]] = field(default_factory=dict)
    replies: list[tuple[int, int, str]] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)

    def add(self, wi: WorkItem) -> None:
        self.items[wi.id] = wi
        self.tags[wi.id] = set(wi.tags)

    def list_intake(self) -> list[WorkItem]:
        return [wi for i, wi in self.items.items() if "agent" in self.tags[i]]

    def list_closed(self, limit: int) -> list[WorkItem]:
        return list(self.items.values())[:limit]

    def get_work_item(self, id: int) -> WorkItem:
        return self.items[id]

    def comment_work_item(self, id: int, html_text: str) -> None:
        self.wi_comments.append((id, html_text))

    def set_tag(self, id: int, tag: str, present: bool) -> None:
        (self.tags[id].add if present else self.tags[id].discard)(tag)

    def has_tag(self, id: int, tag: str) -> bool:
        return tag in self.tags[id]

    def push_branch(self, worktree: Path, branch: str) -> None:
        assert branch.startswith("agent/")
        if self.origin is not None:
            subprocess.run(["git", "push", str(self.origin), f"HEAD:refs/heads/{branch}"],
                           cwd=worktree, check=True, capture_output=True)

    def create_pr(self, branch: str, title: str, body: str, work_item_id: int) -> int:
        pr_id = 100 + len(self.prs)
        self.prs[pr_id] = {"branch": branch, "title": title, "body": body, "status": "active",
                           "work_item": work_item_id, "updates": 0}
        self.pr_threads[pr_id] = []
        return pr_id

    def update_pr(self, pr_id: int, body: str) -> None:
        self.prs[pr_id]["body"] = body
        self.prs[pr_id]["updates"] += 1

    def pr_status(self, pr_id: int) -> str:
        return str(self.prs[pr_id]["status"])

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        return list(self.pr_threads[pr_id])

    def reply_pr(self, pr_id: int, thread_id: int, parent_comment_id: int, text: str) -> None:
        self.replies.append((pr_id, thread_id, text))

    def comment_pr(self, pr_id: int, text: str) -> None:
        self.replies.append((pr_id, 0, text))

    def delete_branch(self, branch: str) -> None:
        self.deleted_branches.append(branch)
