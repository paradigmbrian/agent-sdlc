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
