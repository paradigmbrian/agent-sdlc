from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from agent_sdlc.targets import TargetConfig

log = logging.getLogger(__name__)


class Runnable(Protocol):
    async def tick(self) -> None: ...
    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None: ...


class Supervisor:
    """One thread and event loop per target; a crashed or unbuildable target is retried after
    `restart_s` without affecting the others (spec §5.1)."""

    def __init__(self, targets: list[TargetConfig],
                 build: Callable[[TargetConfig], Runnable], *, restart_s: float = 60.0) -> None:
        self._targets = targets
        self._build = build
        self._restart_s = restart_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _once(self, t: TargetConfig) -> None:
        try:
            asyncio.run(self._build(t).tick())
        except Exception:
            log.exception("target %s failed this tick", t.name)

    def _serve(self, t: TargetConfig, poll_s: int) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._build(t).run_forever(poll_s, self._stop))
            except Exception:
                log.exception("target %s stopped; restarting in %ss", t.name, self._restart_s)
            self._stop.wait(self._restart_s)

    def _join(self, threads: list[threading.Thread]) -> None:
        try:
            while any(th.is_alive() for th in threads):
                for th in threads:
                    th.join(0.5)
        except KeyboardInterrupt:
            self._stop.set()
            raise

    def run_once(self) -> None:
        threads = [threading.Thread(target=self._once, args=(t,), name=f"target-{t.name}",
                                    daemon=True) for t in self._targets]
        for th in threads:
            th.start()
        self._join(threads)

    def run_forever(self, poll_s: int) -> None:
        threads = [threading.Thread(target=self._serve, args=(t, poll_s),
                                    name=f"target-{t.name}", daemon=True)
                   for t in self._targets]
        for th in threads:
            th.start()
        self._join(threads)
