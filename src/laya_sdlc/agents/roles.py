from __future__ import annotations

from dataclasses import dataclass

from laya_sdlc.decisions.gates import work_item_text
from laya_sdlc.types import CommandResult, WorkItem

_READ = ("Read", "Glob", "Grep", "Bash")

_COMMON = """You are working inside a git worktree of the target repository. Rules enforced by
the harness (violations are blocked): you may only touch files inside the worktree; you must not
edit database migrations or schema, infrastructure, CI/CD pipelines, Dockerfiles, env files or
secrets; Bash is restricted to the project's test/lint/typecheck/build commands and read-only
commands (ls, cat, grep, rg, find, git status/diff/log/show). Do not commit or push; the
harness does that. Follow the repository's existing conventions."""

PLANNER_PROMPT = _COMMON + """

Role: planner. Read the relevant code and write an implementation plan for the work item.
Output ONLY the plan in markdown with these sections: Summary, Files to change (exact paths),
Steps (numbered, concrete), Tests to add or update (exact test names and what they assert),
Out of scope. Keep it within the work item's scope."""

IMPLEMENTER_PROMPT = _COMMON + """

Role: implementer. Implement the plan using test-driven development: write or update the failing
test first, run it, implement, run it again. Run the relevant test command before finishing.
Finish with a short summary of what changed and which tests you ran."""

REVIEWER_PROMPT = _COMMON + """

Role: reviewer. Review the diff against the work item and plan. Report BLOCKING issues (bugs,
missing requirements, failing or missing tests, security problems, scope creep) separately from
NON-BLOCKING suggestions. If there are no blocking issues, say exactly "No blocking issues."."""


@dataclass(frozen=True)
class Role:
    name: str
    system_prompt: str
    tools: tuple[str, ...]
    stage: str


PLANNER = Role("planner", PLANNER_PROMPT, _READ, "plan")
IMPLEMENTER = Role("implementer", IMPLEMENTER_PROMPT, _READ + ("Edit", "Write"), "implement")
REVIEWER = Role("reviewer", REVIEWER_PROMPT, _READ, "review")


def _feedback(feedback: str | None) -> str:
    return f"\n\n## Feedback from the previous attempt\n{feedback}" if feedback else ""


def planner_prompt(wi: WorkItem, feedback: str | None) -> str:
    return f"## Work item #{wi.id}\n{work_item_text(wi)}{_feedback(feedback)}"


def implementer_prompt(wi: WorkItem, plan: str, feedback: str | None) -> str:
    return (f"## Work item #{wi.id}\n{work_item_text(wi)}\n\n## Plan\n{plan}"
            f"{_feedback(feedback)}")


def reviewer_prompt(wi: WorkItem, plan: str, diff: str, checks: list[CommandResult]) -> str:
    check_text = "\n".join(
        f"- {c.name} (exit {c.exit_code}):\n```\n{c.output[-2000:]}\n```" for c in checks)
    return (f"## Work item #{wi.id}\n{work_item_text(wi)}\n\n## Plan\n{plan}\n\n"
            f"## Check results\n{check_text}\n\n## Diff\n```diff\n{diff}\n```")
