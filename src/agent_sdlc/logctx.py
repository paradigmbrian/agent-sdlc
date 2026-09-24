from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s [#%(item)s %(stage)s] %(name)s: %(message)s"
_item: ContextVar[str] = ContextVar("agent_sdlc_item", default="-")
_stage: ContextVar[str] = ContextVar("agent_sdlc_stage", default="-")
_NOISY = ("httpx", "httpcore", "urllib3", "asyncio")


@contextmanager
def log_context(item_id: int, stage: str) -> Iterator[None]:
    """Tag every log record emitted inside the block with the item and stage."""
    t_item, t_stage = _item.set(str(item_id)), _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(t_stage)
        _item.reset(t_item)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.item = _item.get()
        record.stage = _stage.get()
        return True


def configure_logging(log_dir: Path | None) -> None:
    """stderr at INFO plus `log_dir/agent-sdlc.log` at DEBUG (daily rotation, 14 kept).
    Safe to call repeatedly: replaces the handlers it added before."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_agent_sdlc", False)]:
        root.removeHandler(h)
        h.close()
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    handlers.append(console)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fh = TimedRotatingFileHandler(log_dir / "agent-sdlc.log", when="midnight",
                                          backupCount=14, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            handlers.append(fh)
        except OSError as e:
            sys.stderr.write(f"agent-sdlc: file logging disabled ({log_dir}): {e}\n")
    fmt = logging.Formatter(FORMAT)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(ContextFilter())
        h._agent_sdlc = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(logging.DEBUG)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
