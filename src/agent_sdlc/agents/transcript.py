from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from agent_sdlc.fsutil import ensure_private_dir
from agent_sdlc.types import Denial

log = logging.getLogger(__name__)
TOOL_RESULT_CHARS = 20_000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} chars)"


class TranscriptWriter:
    """One JSON object per line for an agent session, flushed per line so a crash keeps the
    partial transcript. Never raises: the first I/O error is logged once and recorded in
    `error`, and writing stops (spec §9)."""

    def __init__(self, path: Path | None) -> None:
        self.path: str | None = None
        self.error: str | None = None
        self._f: IO[str] | None = None
        if path is None:
            return
        try:
            ensure_private_dir(path.parent)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._f = os.fdopen(fd, "a", encoding="utf-8")
            self.path = str(path)
        except OSError as e:
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        self.error = f"{type(e).__name__}: {e}"
        log.warning("transcript writing stopped: %s", self.error)
        self.close()

    def _write(self, obj: dict[str, Any]) -> None:
        if self._f is None:
            return
        try:
            line = json.dumps({"ts": datetime.now(UTC).isoformat(), **obj}, default=str,
                              ensure_ascii=False)
            self._f.write(line + "\n")
            self._f.flush()
        except (OSError, TypeError, ValueError) as e:
            self._fail(e)

    def prompt(self, role: str, text: str) -> None:
        self._write({"type": "prompt", "role": role, "text": text})

    def denied(self, d: Denial) -> None:
        self._write({"type": "denied", "tool": d.tool, "category": d.category,
                     "reason": d.reason})

    def message(self, msg: object) -> None:
        kind = type(msg).__name__
        if kind == "AssistantMessage":
            for block in getattr(msg, "content", None) or []:
                bkind = type(block).__name__
                if bkind == "TextBlock":
                    self._write({"type": "assistant_text", "text": block.text})
                elif bkind == "ToolUseBlock":
                    self._write({"type": "tool_use", "id": block.id, "name": block.name,
                                 "input": block.input})
        elif kind == "UserMessage":
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if type(block).__name__ != "ToolResultBlock":
                        continue
                    body = block.content
                    text = "" if body is None else body if isinstance(body, str) else \
                        json.dumps(body, default=str, ensure_ascii=False)
                    self._write({"type": "tool_result", "tool_use_id": block.tool_use_id,
                                 "is_error": bool(block.is_error),
                                 "content": _clip(text, TOOL_RESULT_CHARS)})
        elif kind == "RateLimitEvent":
            info = getattr(msg, "rate_limit_info", None)
            self._write({"type": "rate_limit", "status": getattr(info, "status", None),
                         "resets_at": getattr(info, "resets_at", None)})
        elif kind == "ResultMessage":
            self._write({"type": "result", "subtype": getattr(msg, "subtype", None),
                         "session_id": getattr(msg, "session_id", None),
                         "num_turns": getattr(msg, "num_turns", None),
                         "duration_ms": getattr(msg, "duration_ms", None),
                         "total_cost_usd": getattr(msg, "total_cost_usd", None),
                         "usage": getattr(msg, "usage", None)})

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
