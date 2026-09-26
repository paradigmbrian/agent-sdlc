from __future__ import annotations

import html
from typing import Any

from agent_sdlc.types import GATE_PARKS, Decision, Item, ParkReason, Stage, WorkItem

MAX_PR_DESCRIPTION = 4000
_TRUNCATED = "\n\n…(truncated; the full plan is in the work item comments)"
_PREFIX = {"Bug": "fix", "Task": "chore"}
_PARK_DETAIL_CHARS = 6000
_AGENT_STAGES = {Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW}

QUESTION_REPLY = ("Thanks — I only act on change requests automatically. If you want a code "
                  "change, reply starting with `/agent` and describe it.")
UNCERTAIN_REPLY = ("I couldn't tell whether this asks for a code change. To request one, reply "
                   "starting with `/agent`.")


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


def pr_title(wi: WorkItem, ref: str) -> str:
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title} ({ref})"[:400]


def commit_message(wi: WorkItem, round_: int, ref: str) -> str:
    suffix = f" (revision {round_})" if round_ else ""
    return f"{_PREFIX.get(wi.work_item_type, 'feat')}: {wi.title}{suffix}\n\n{ref}"


def _decision_line(d: Decision) -> str:
    flag = " · shadow" if d.shadow else ""
    return f"| {d.gate}.{d.question} | {d.answer} | {d.confidence:.2f}{flag} |"


def pr_body(item: Item, wi: WorkItem, ref: str, decisions: list[Decision],
            checks: list[dict[str, Any]], review_notes: str) -> str:
    u = item.usage
    check_lines = "\n".join(
        f"- {'✅' if c['exit_code'] == 0 else '❌'} `{c['command']}` ({c['duration_s']}s)"
        for c in checks)
    latest: dict[str, Decision] = {}
    for d in decisions:
        latest[f"{d.gate}.{d.question}"] = d
    note = item.data.get("note") or ""
    parts = [
        f"Automated change for {ref} by agent-sdlc. **Human review required before merge.**",
        f"> {note}" if note else "",
        "## Checks\n" + (check_lines or "(none)"),
        "## Laya decisions\n| gate | answer | confidence |\n|---|---|---|\n"
        + "\n".join(_decision_line(d) for d in latest.values()),
        f"## Usage\n{u.turns} turns · {u.tokens:,} tokens "
        f"(+{u.cache_read_tokens:,} cache-read tokens) · verify retries {item.attempt} · "
        f"PR rounds {item.pr_rounds}",
        _denials_md(item.data),
        "## Review notes\n" + (review_notes or "(none)"),
        "## Plan\n" + str(item.data.get("plan", "")),
    ]
    body = "\n\n".join(p for p in parts if p)
    if len(body) > MAX_PR_DESCRIPTION:
        body = body[: MAX_PR_DESCRIPTION - len(_TRUNCATED)] + _TRUNCATED
    return body


def park_comment_html(item: Item, *, label_word: str = "tag",
                      parked_label: str = "agent:parked") -> str:
    reason = item.park_reason.value if item.park_reason else "unknown"
    stage = item.parked_from.value if item.parked_from else "unknown"
    note = html.escape(str(item.data.get("park_note", "")))
    lab = f"<code>{html.escape(parked_label)}</code> {label_word}"

    # Determine guidance based on park type
    is_gate_park = item.park_reason in GATE_PARKS
    is_gate_stage = item.parked_from in {Stage.TRIAGE, Stage.PLAN, Stage.REVIEW}

    if item.park_reason is ParkReason.MANIFEST:
        resume = html.escape(str(item.data.get("manifest_resume", Stage.VERIFY.value)))
        guidance = (f"Removing the {lab} approves these dependency "
                    f"changes; install and {resume} will run with them.")
    elif is_gate_park and is_gate_stage and item.parked_from is not None:
        next_stages = {Stage.TRIAGE: "plan", Stage.PLAN: "implement", Stage.REVIEW: "pr_open"}
        next_stage = next_stages.get(item.parked_from, "unknown")
        guidance = (f"To continue, update the item if needed and remove the {lab} to approve "
                    f"proceeding to the <code>{next_stage}</code> stage.")
    else:
        retry = "implement" if item.park_reason is ParkReason.PR_ROUNDS else stage
        guidance = (f"To continue, update the item if needed and remove the {lab} to retry the "
                    f"<code>{retry}</code> stage with fresh retry counters.")

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


def plan_comment_html(plan: str, pr_ref: str) -> str:
    return (f"<p><b>agent-sdlc plan</b> for PR {html.escape(pr_ref)}:</p>"
            f"<pre>{html.escape(plan)}</pre>")
