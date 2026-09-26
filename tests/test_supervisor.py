import threading

from agent_sdlc.orchestrator.supervisor import Supervisor
from agent_sdlc.targets import TargetConfig

BASE = {"repo": {"install": "true", "commands": {"test": "true"}},
        "policy": {"protected_paths": ["infra/**"]},
        "forge": {"kind": "ado", "org": "o", "project": "p", "repo": "r"}}
A = TargetConfig.model_validate({**BASE, "name": "a"})
B = TargetConfig.model_validate({**BASE, "name": "b"})


class FakeScheduler:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier
        self.threads: list[int] = []
        self.ticks = 0

    async def tick(self) -> None:
        self.threads.append(threading.get_ident())
        if self.barrier is not None:
            self.barrier.wait(timeout=5)     # both targets must be inside tick at once
        self.ticks += 1

    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None:
        await self.tick()
        raise RuntimeError("crash")          # the supervisor restarts it


def test_run_once_ticks_targets_in_parallel_threads() -> None:
    barrier = threading.Barrier(2)
    scheds = {"a": FakeScheduler(barrier), "b": FakeScheduler(barrier)}
    ok = Supervisor([A, B], lambda t: scheds[t.name]).run_once()  # type: ignore[arg-type,return-value]
    assert scheds["a"].ticks == 1 and scheds["b"].ticks == 1
    assert scheds["a"].threads != scheds["b"].threads
    assert ok is True


def test_run_once_build_failure_does_not_block_other_targets() -> None:
    ok = FakeScheduler()

    def build(t: TargetConfig) -> FakeScheduler:
        if t.name == "a":
            raise ValueError("set forge.app_id for target a")
        return ok

    Supervisor([A, B], build).run_once()  # type: ignore[arg-type]
    assert ok.ticks == 1


def test_m4_run_once_reports_failure_when_a_target_fails_to_build() -> None:
    ok = FakeScheduler()

    def build(t: TargetConfig) -> FakeScheduler:
        if t.name == "a":
            raise ValueError("set forge.app_id for target a")
        return ok

    assert Supervisor([A, B], build).run_once() is False  # type: ignore[arg-type]
    assert ok.ticks == 1


class FailingTickScheduler:
    async def tick(self) -> None:
        raise RuntimeError("boom")

    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None:
        raise NotImplementedError


def test_m4_run_once_reports_failure_when_a_target_tick_raises() -> None:
    ok = FakeScheduler()

    def build(t: TargetConfig) -> object:
        return FailingTickScheduler() if t.name == "a" else ok

    assert Supervisor([A, B], build).run_once() is False  # type: ignore[arg-type]
    assert ok.ticks == 1


def test_m4_run_once_is_true_when_every_target_succeeds() -> None:
    scheds = {"a": FakeScheduler(), "b": FakeScheduler()}
    assert Supervisor([A, B], lambda t: scheds[t.name]).run_once() is True  # type: ignore[arg-type,return-value]


def test_run_forever_restarts_a_crashed_target() -> None:
    builds: list[str] = []
    sup: Supervisor

    def build(t: TargetConfig) -> FakeScheduler:
        builds.append(t.name)
        if len(builds) >= 3:
            sup.stop()
        return FakeScheduler()

    sup = Supervisor([A], build, restart_s=0.01)  # type: ignore[arg-type]
    sup.run_forever(poll_s=60)
    assert builds[:3] == ["a", "a", "a"]
