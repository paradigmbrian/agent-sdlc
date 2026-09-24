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
