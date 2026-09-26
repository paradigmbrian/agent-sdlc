import threading
import time

import pytest

from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.types import AgentInterrupted


def test_second_holder_waits_for_the_first() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    order: list[str] = []
    entered = threading.Event()

    def first() -> None:
        with slots.hold(lambda: False):
            entered.set()
            time.sleep(0.1)
            order.append("first done")

    t = threading.Thread(target=first)
    t.start()
    entered.wait()
    with slots.hold(lambda: False):
        order.append("second in")
    t.join()
    assert order == ["first done", "second in"]


def test_stop_while_waiting_raises_interrupted() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    with slots.hold(lambda: False), pytest.raises(AgentInterrupted):
        with slots.hold(lambda: True):
            pass


def test_slot_released_after_exception() -> None:
    slots = SessionSlots(1, poll_s=0.01)
    with pytest.raises(RuntimeError), slots.hold(lambda: False):
        raise RuntimeError("boom")
    with slots.hold(lambda: True):   # acquires immediately: the slot was released
        pass
