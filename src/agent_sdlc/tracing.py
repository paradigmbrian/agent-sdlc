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
    head = f'{item.target}#{item.external_id} "{item.title}"   stage: {item.stage.value}'
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
