from __future__ import annotations

import html
from typing import Any

from laya_sdlc.types import GATE_PARKS, Decision, Item, Stage, WorkItem

MAX_PR_DESCRIPTION = 4000
_TRUNCATED = "\n\n…(truncated; the full plan is in the work item comments)"
_PREFIX = {"Bug": "fix", "Task": "chore"}

QUESTION_REPLY = ("Thanks — I only act on change requests automatically. If you want a code "
                  "change, reply starting with `/laya` and describe it.")
UNCERTAIN_REPLY = ("I couldn't tell whether this asks for a code change. To request one, reply "
                   "starting with `/laya`.")


def pr_title(wi: WorkItem) -> str:
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title} (AB#{wi.id})"[:400]


def commit_message(wi: WorkItem, round_: int) -> str:
    suffix = f" (revision {round_})" if round_ else ""
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title}{suffix}\n\nAB#{wi.id}"


def _decision_line(d: Decision) -> str:
    flag = " · shadow" if d.shadow else ""
    return f"| {d.gate}.{d.question} | {d.answer} | {d.confidence:.2f}{flag} |"


def pr_body(item: Item, wi: WorkItem, decisions: list[Decision], checks: list[dict[str, Any]],
            review_notes: str) -> str:
    u = item.usage
    check_lines = "\n".join(
        f"- {'✅' if c['exit_code'] == 0 else '❌'} `{c['command']}` ({c['duration_s']}s)"
        for c in checks)
    latest: dict[str, Decision] = {}
    for d in decisions:
        latest[f"{d.gate}.{d.question}"] = d
    note = item.data.get("note") or ""
    parts = [
        f"Automated change for AB#{wi.id} by laya-sdlc. **Human review required before merge.**",
        f"> {note}" if note else "",
        "## Checks\n" + (check_lines or "(none)"),
        "## Laya decisions\n| gate | answer | confidence |\n|---|---|---|\n"
        + "\n".join(_decision_line(d) for d in latest.values()),
        f"## Usage\n{u.turns} turns · {u.tokens:,} tokens · verify retries {item.attempt} · "
        f"PR rounds {item.pr_rounds}",
        "## Review notes\n" + (review_notes or "(none)"),
        "## Plan\n" + str(item.data.get("plan", "")),
    ]
    body = "\n\n".join(p for p in parts if p)
    if len(body) > MAX_PR_DESCRIPTION:
        body = body[: MAX_PR_DESCRIPTION - len(_TRUNCATED)] + _TRUNCATED
    return body


def park_comment_html(item: Item) -> str:
    reason = item.park_reason.value if item.park_reason else "unknown"
    stage = item.parked_from.value if item.parked_from else "unknown"
    note = html.escape(str(item.data.get("park_note", "")))

    # Determine guidance based on park type
    is_gate_park = item.park_reason in GATE_PARKS
    is_gate_stage = item.parked_from in {Stage.TRIAGE, Stage.PLAN, Stage.REVIEW}

    if is_gate_park and is_gate_stage and item.parked_from is not None:
        next_stages = {Stage.TRIAGE: "plan", Stage.PLAN: "implement", Stage.REVIEW: "pr_open"}
        next_stage = next_stages.get(item.parked_from, "unknown")
        guidance = (f"To continue, update the item if needed and remove the "
                    f"<code>laya:parked</code> tag to approve proceeding to the <code>{next_stage}"
                    f"</code> stage.")
    else:
        guidance = (f"To continue, update the item if needed and remove the "
                    f"<code>laya:parked</code> tag to retry the <code>{stage}</code> stage with "
                    f"fresh retry counters.")

    return (f"<p><b>laya-sdlc parked this item</b> at stage <code>{stage}</code> "
            f"(reason: <code>{reason}</code>).</p><pre>{note}</pre>"
            f"<p>{guidance}</p>")


def plan_comment_html(plan: str, pr_id: int) -> str:
    return f"<p><b>laya-sdlc plan</b> for PR !{pr_id}:</p><pre>{html.escape(plan)}</pre>"
