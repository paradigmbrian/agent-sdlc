from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agent_sdlc.store import LabelInput, Store
from agent_sdlc.types import Calibration, Decision, EventInput, ParkReason, Stage, Usage, WorkItem
from tests.fakes import decision

WI = WorkItem(1, "Fix login", "desc", "ac", "Bug", ("agent",), "https://x/1")


def _decision(q: str = "clarity", answer: str = "clear") -> Decision:
    return Decision("triage", q, answer, {"clear": 0.9, "unclear": 0.1},
                    {"clear": 0.95, "unclear": 0.05}, 0.9, True, False)


@pytest.fixture
def store() -> Store:
    return Store("sqlite://")


def test_add_item_dedupes_forever(store: Store) -> None:
    assert store.add_item("t", WI, "agent/1-fix-login") is not None
    item = store.get_by_ref("t", 1)
    assert item.stage is Stage.TRIAGE and item.branch == "agent/1-fix-login"
    store.save(replace(item, stage=Stage.DONE))
    assert store.add_item("t", WI, "agent/1-fix-login") is None


def test_save_roundtrip(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = replace(store.get_by_ref("t", 1), stage=Stage.PARKED, park_reason=ParkReason.RED,
                   parked_from=Stage.VERIFY, attempt=3, pr_id=7,
                   data={"plan": "p"}, usage=Usage(2, 10, 5))
    store.save(item)
    assert store.get(item.id) == item


def test_items_filters_by_stage_and_target(store: Store) -> None:
    store.add_item("t", WI, "b")
    store.add_item("t", replace(WI, id=2), "b2")
    store.add_item("other", replace(WI, id=3), "b3")
    store.save(replace(store.get_by_ref("t", 2), stage=Stage.PLAN))
    assert [i.id for i in store.items("t")] == [1, 2]
    assert [i.id for i in store.items("t", [Stage.PLAN])] == [2]


def test_commit_step_writes_everything(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = replace(store.get_by_ref("t", 1), stage=Stage.PLAN)
    d = _decision()
    store.commit_step(item, [(d, {"title": "Fix login"})], Usage(1, 100, 50), date(2026, 9, 23),
                      [LabelInput("triage", "clarity", d.raw_probs, "clear", "test")])
    assert store.get_by_ref("t", 1).stage is Stage.PLAN
    assert store.decisions_for(1, "triage") == [d]
    assert store.daily_usage(date(2026, 9, 23)) == Usage(1, 100, 50)
    assert store.labels("triage", "clarity") == [(d.raw_probs, "clear")]


def test_unlabeled_decisions_excludes_labeled(store: Store) -> None:
    store.add_item("t", WI, "b")
    item = store.get_by_ref("t", 1)
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
    from agent_sdlc.types import Usage as U
    u = U(1, 2, 3, cache_read_tokens=4)
    assert U.from_dict(u.to_dict()) == u and u.tokens == 5
    assert U.from_dict({"turns": 1, "input_tokens": 2, "output_tokens": 3}) == U(1, 2, 3, 0)
    assert (u + u).cache_read_tokens == 8


def test_events_round_trip_order_and_context(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get_by_ref("t", WI.id)
    store.add_event("intake", {"branch": "b"}, item=it)
    store.add_event("pause")
    new = replace(it, stage=Stage.PLAN)
    store.commit_step(new, [], Usage(), date(2026, 10, 1), [],
                      events=[EventInput("transition", {"from": "triage", "to": "plan"})], at=it)
    evs = store.events_for(WI.id)
    assert [e.kind for e in evs] == ["intake", "transition"]
    assert evs[1].stage == "triage" and evs[1].attempt == 0 and evs[1].payload["to"] == "plan"
    assert evs[1].ts.tzinfo is not None
    assert store.get_by_ref("t", WI.id).stage is Stage.PLAN
    start = evs[0].ts - timedelta(seconds=1)
    assert [e.kind for e in store.events_since(start)] == ["intake", "pause", "transition"]
    assert [e.kind for e in store.events_since(start, kinds=["pause"])] == ["pause"]
    assert store.last_event_ts(WI.id) == evs[1].ts


def test_events_since_excludes_older_and_save_writes_events(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get_by_ref("t", WI.id)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store.add_event("intake", {}, item=it, ts=old)
    store.save(replace(it, attempt=1), events=[EventInput("infra_failure", {"n": 1})], at=it)
    assert [e.kind for e in store.events_since(old + timedelta(days=1))] == ["infra_failure"]
    assert store.events_for(WI.id)[0].ts == old
    assert store.get_by_ref("t", WI.id).attempt == 1
    assert store.last_event_ts(999) is None


def test_label_queries_for_metrics_and_abandoned(store: Store) -> None:
    store.add_item("t", WI, "b")
    it = store.get_by_ref("t", WI.id)
    d1 = decision("review", "review_blocking", "no")
    d2 = decision("review", "risk", "low")
    store.commit_step(it, [(d1, {"review_notes": "n"}), (d2, {"review_notes": "n"})],
                      Usage(), date(2026, 10, 1), [])
    [(first_id, _, _)] = store.unlabeled_decisions("review", 1)
    store.add_label(LabelInput("review", "review_blocking", d1.raw_probs, "false", "manual",
                               first_id))
    assert store.labeled_decisions("review", "review_blocking") == [("no", "false")]
    store.add_event("outcome", {"result": "abandoned"}, item=it)
    assert store.abandoned_item_ids() == {WI.id}
    rows = store.unlabeled_decisions_for_items("review", {WI.id}, 10)
    assert [(item_id, d.question) for _, item_id, d, _ in rows] == [(WI.id, "risk")]
    assert store.unlabeled_decisions_for_items("review", set(), 10) == []
    assert store.decision_states(WI.id, "review")[0] == {"review_notes": "n"}
    assert [d.question for _, d in store.decisions_with_ts(WI.id)] == [
        "review_blocking", "risk"]


def test_same_external_id_under_two_targets(store: Store) -> None:
    a = store.add_item("rallysource", WI, "agent/1-a")
    b = store.add_item("triathlon", WI, "agent/1-b")
    assert a is not None and b is not None and a.id != b.id
    assert (a.external_id, b.external_id) == (1, 1)
    assert store.add_item("triathlon", WI, "agent/1-b") is None
    assert store.get_by_ref("triathlon", 1).branch == "agent/1-b"
    assert {i.target for i in store.find_external(1)} == {"rallysource", "triathlon"}
    assert [i.target for i in store.find_external(1, ["triathlon"])] == ["triathlon"]
    with pytest.raises(KeyError):
        store.get_by_ref("nope", 1)


def test_labels_record_target(store: Store) -> None:
    store.add_label(LabelInput("triage", "kind", {"bug": 1.0}, "bug", "manual", target="tri"))
    assert store.labels("triage", "kind") == [({"bug": 1.0}, "bug")]
    assert store.label_targets("triage", "kind") == ["tri"]


def test_file_db_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    s = Store(f"sqlite:///{tmp_path / 'x.db'}")
    with s._engine.connect() as c:  # noqa: SLF001 - pragma check
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
