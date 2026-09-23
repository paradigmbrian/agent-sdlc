from __future__ import annotations

from typing import Any

from laya_sdlc.types import WorkItem

GATES: dict[str, dict[str, dict[str, Any]]] = {
    "triage": {
        "kind": {
            "type": "choice",
            "instructions": "What kind of work does this work item describe?",
            "criteria": {
                "bug": "something is broken or behaves incorrectly",
                "feature": "a new capability or a change in behavior",
                "chore": "refactor, dependency, tooling, docs or cleanup with no behavior change",
                "question": "a question, discussion or request for information, not a change",
            },
        },
        "clarity": {
            "type": "score",
            "instructions": "How clearly does the work item specify what done looks like?",
            "criteria": [
                "unclear: missing what should change or why",
                "partly clear: goal known but key details or acceptance criteria missing",
                "clear: goal, scope and acceptance criteria are specific",
            ],
        },
        "touches_protected": {
            "type": "noul",
            "instructions": ("Does this work require changing database migrations or schema, "
                             "infrastructure, CI/CD pipelines, Dockerfiles, or secrets or "
                             "environment configuration?"),
        },
        "size": {
            "type": "choice",
            "instructions": "How large is the change?",
            "criteria": {
                "small": "a few files, under a day of work",
                "medium": "several files in one area, one to three days",
                "large": "many areas, multiple days, or needs design work first",
            },
        },
    },
    "plan": {
        "plan_addresses_item": {
            "type": "noul",
            "instructions": "Does the plan fully address what the work item asks for?",
        },
        "plan_scope_ok": {
            "type": "noul",
            "instructions": ("Does the plan stay within the work item's scope, without unrelated "
                             "changes and without touching migrations, infrastructure, pipelines "
                             "or secrets?"),
        },
    },
    "review": {
        "review_blocking": {
            "type": "noul",
            "instructions": ("Do the review notes report a blocking problem (bug, missing "
                             "requirement, failing behavior or security issue) that must be "
                             "fixed before merge?"),
        },
        "risk": {
            "type": "score",
            "instructions": "How risky is merging this change?",
            "criteria": ["low", "medium", "high"],
        },
    },
    "comment": {
        "comment_intent": {
            "type": "choice",
            "instructions": "What does this pull request comment ask for?",
            "criteria": {
                "change_request": "asks for the code to be changed",
                "question": "asks a question without requesting a change",
                "approval": "approves or praises the change",
                "noise": "automated, status or irrelevant text",
            },
        },
    },
}


def option_keys(qdef: dict[str, Any]) -> list[str]:
    kind = qdef["type"]
    if kind == "choice":
        return list(qdef["criteria"].keys())
    if kind == "score":
        return [str(c).split(":")[0].strip() for c in qdef["criteria"]]
    return ["false", "true"]


def work_item_text(wi: WorkItem, limit: int = 6000) -> str:
    text = (f"Type: {wi.work_item_type}\nTitle: {wi.title}\n\nDescription:\n{wi.description}"
            f"\n\nAcceptance criteria:\n{wi.acceptance_criteria or '(none)'}")
    return text[:limit]


def triage_state(wi: WorkItem) -> dict[str, str]:
    return {
        "type": wi.work_item_type,
        "title": wi.title,
        "description": wi.description[:4000],
        "acceptance_criteria": wi.acceptance_criteria[:2000],
    }
