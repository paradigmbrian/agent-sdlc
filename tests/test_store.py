from dataclasses import replace
from datetime import date

import pytest

from laya_sdlc.store import LabelInput, Store
from laya_sdlc.types import Calibration, Decision, ParkReason, Stage, Usage, WorkItem

WI = WorkItem(1, "Fix login", "desc", "ac", "Bug", ("laya",), "https://x/1")


def _decision(q: str = "clarity", answer: str = "clear") -> Decision:
    return Decision("triage", q, answer, {"clear": 0.9, "unclear": 0.1},
                    {"clear": 0.95, "unclear": 0.05}, 0.9, True, False)


@pytest.fixture
def store() -> Store:
    return Store("sqlite://")


def test_add_item_dedupes_forever(store: Store) -> None:
    assert store.add_item("t", WI, "laya/1-fix-login") is True
    item = store.get(1)
    assert item.stage is Stage.TRIAGE and item.branch == "laya/1-fix-login"
    store.save(replace(item, stage=Stage.DONE))
    assert store.add_item("t", WI, "laya/1-fix-login") is False


def test_save_roundtrip(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = replace(store.get(1), stage=Stage.PARKED, park_reason=ParkReason.RED,
                   parked_from=Stage.VERIFY, attempt=3, pr_id=7,
                   data={"plan": "p"}, usage=Usage(2, 10, 5))
    store.save(item)
    assert store.get(1) == item


def test_items_filters_by_stage_and_target(store: Store) -> None:
    store.add_item("t", WI, "b")
    store.add_item("t", replace(WI, id=2), "b2")
    store.add_item("other", replace(WI, id=3), "b3")
    store.save(replace(store.get(2), stage=Stage.PLAN))
    assert [i.id for i in store.items("t")] == [1, 2]
    assert [i.id for i in store.items("t", [Stage.PLAN])] == [2]


def test_commit_step_writes_everything(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = replace(store.get(1), stage=Stage.PLAN)
    d = _decision()
    store.commit_step(item, [(d, {"title": "Fix login"})], Usage(1, 100, 50), date(2026, 9, 23),
                      [LabelInput("triage", "clarity", d.raw_probs, "clear", "test")])
    assert store.get(1).stage is Stage.PLAN
    assert store.decisions_for(1, "triage") == [d]
    assert store.daily_usage(date(2026, 9, 23)) == Usage(1, 100, 50)
    assert store.labels("triage", "clarity") == [(d.raw_probs, "clear")]


def test_unlabeled_decisions_excludes_labeled(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = store.get(1)
    store.commit_step(item, [(_decision(), {"s": 1}), (_decision("size", "small"), {"s": 2})],
                      Usage(), date(2026, 9, 23), [])
    rows = store.unlabeled_decisions("triage", 10)
    assert [d.question for _, d, _ in rows] == ["clarity", "size"]
    store.add_label(LabelInput("triage", "clarity", {}, "clear", "manual", decision_id=rows[0][0]))
    assert [d.question for _, d, _ in store.unlabeled_decisions("triage", 10)] == ["size"]


def test_daily_usage_accumulates(store: Store) -> None:
    day = date(2026, 9, 23)
    store.add_daily_usage(day, Usage(1, 10, 1))
    store.add_daily_usage(day, Usage(2, 20, 2))
    assert store.daily_usage(day) == Usage(3, 30, 3)
    assert store.daily_usage(date(2026, 9, 24)) == Usage()


def test_flags(store: Store) -> None:
    assert store.get_flag("paused") is None
    store.set_flag("paused", "1")
    assert store.get_flag("paused") == "1"
    store.set_flag("paused", None)
    assert store.get_flag("paused") is None


def test_calibration_roundtrip(store: Store) -> None:
    assert store.calibration("triage", "clarity") is None
    cal = Calibration(temperature=1.7, threshold=0.85, mode="active", ece=0.06, n=40)
    store.set_calibration("triage", "clarity", cal)
    assert store.calibration("triage", "clarity") == cal


# --- final review fix wave ---------------------------------------------------------------


def test_i4_usage_cache_reads_roundtrip_and_old_dicts_load() -> None:
    from laya_sdlc.types import Usage as U
    u = U(1, 2, 3, cache_read_tokens=4)
    assert U.from_dict(u.to_dict()) == u and u.tokens == 5
    assert U.from_dict({"turns": 1, "input_tokens": 2, "output_tokens": 3}) == U(1, 2, 3, 0)
    assert (u + u).cache_read_tokens == 8
