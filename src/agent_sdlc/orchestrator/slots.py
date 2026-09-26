from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from agent_sdlc.types import AgentInterrupted


class SessionSlots:
    """Caps concurrent agent sessions across all targets (spec §5.2)."""

    def __init__(self, n: int, poll_s: float = 5.0) -> None:
        self._sem = threading.BoundedSemaphore(n)
        self._poll_s = poll_s

    @contextmanager
    def hold(self, should_stop: Callable[[], bool]) -> Iterator[None]:
        while not self._sem.acquire(timeout=self._poll_s):
            if should_stop():
                raise AgentInterrupted("paused while waiting for an agent session slot")
        try:
            yield
        finally:
            self._sem.release()
