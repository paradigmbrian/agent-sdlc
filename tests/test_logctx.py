import logging
from pathlib import Path

from agent_sdlc.logctx import ContextFilter, configure_logging, log_context


def test_context_filter_adds_item_and_stage() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    f = ContextFilter()
    f.filter(rec)
    assert (rec.item, rec.stage) == ("-", "-")  # type: ignore[attr-defined]
    with log_context(7, "plan"):
        f.filter(rec)
        assert (rec.item, rec.stage) == ("7", "plan")  # type: ignore[attr-defined]
    f.filter(rec)
    assert rec.item == "-"  # type: ignore[attr-defined]


def test_configure_logging_writes_file_with_context(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    with log_context(4821, "implement"):
        logging.getLogger("agent_sdlc.test").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "agent-sdlc.log").read_text()
    assert "[#4821 implement] agent_sdlc.test: hello" in text


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_agent_sdlc", False)]
    assert len(ours) == 2
