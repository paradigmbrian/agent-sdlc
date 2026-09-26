import random

import pytest

from agent_sdlc.labeling import MIN_LABELS, calibrate_question, label_logged, label_triage
from agent_sdlc.store import LabelInput, Store
from agent_sdlc.types import WorkItem
from tests.fakes import FakeDecider, FakeForge


def _seed(store: Store, n: int, acc: float) -> None:
    rng = random.Random(1)
    for _ in range(n):
        store.add_label(LabelInput("plan", "plan_scope_ok", {"false": 0.02, "true": 0.98},
                                   "true" if rng.random() < acc else "false", "t"))


def test_calibrate_requires_minimum_labels() -> None:
    store = Store("sqlite://")
    _seed(store, MIN_LABELS - 1, 0.9)
    r = calibrate_question(store, "plan", "plan_scope_ok", 0.10, promote=True)
    assert r.mode == "shadow" and "need" in r.message
    assert store.calibration("plan", "plan_scope_ok") is None


def test_calibrate_fits_and_promotes_only_when_ece_ok() -> None:
    store = Store("sqlite://")
    _seed(store, 200, 0.97)
    r = calibrate_question(store, "plan", "plan_scope_ok", 0.10, promote=True)
    assert r.n == 200 and r.ece <= 0.10 and r.mode == "active"
    assert store.calibration("plan", "plan_scope_ok").mode == "active"


def test_calibrate_refuses_promotion_on_bad_ece() -> None:
    store = Store("sqlite://")
    _seed(store, 200, 0.5)
    r = calibrate_question(store, "plan", "plan_scope_ok", 0.0001, promote=True)
    assert r.mode == "shadow" and "ECE" in r.message


def test_label_triage_records_answers() -> None:
    store, forge, decider = Store("sqlite://"), FakeForge(), FakeDecider()
    forge.add(WorkItem(1, "t", "d", "a", "Bug", (), "u"))
    answers = iter(["bug", "clear", "", "nonsense"])
    n = label_triage(forge, decider, store, 5, lambda _prompt: next(answers))
    assert n == 2
    assert [g for _, g in store.labels("triage", "kind")] == ["bug"]
    assert store.labels("triage", "size") == []


def test_label_logged_marks_decisions() -> None:
    from datetime import date

    from agent_sdlc.types import Usage
    from tests.fakes import decision
    store = Store("sqlite://")
    store.add_item("t", WorkItem(1, "t", "d", "a", "Bug", (), "u"), "b")
    store.commit_step(store.get_by_ref("t", 1),
                      [(decision("review", "review_blocking", "no"), {"n": "x"})],
                      Usage(), date(2026, 9, 23), [])
    assert label_logged(store, "review", 10, lambda _p: "true") == 1
    assert store.labels("review", "review_blocking")[0][1] == "true"
    assert label_logged(store, "review", 10, lambda _p: "true") == 0


def test_m3_label_logged_fills_target_from_the_decisions_item() -> None:
    from datetime import date

    from agent_sdlc.types import Usage
    from tests.fakes import decision
    store = Store("sqlite://")
    store.add_item("triathlon", WorkItem(1, "t", "d", "a", "Bug", (), "u"), "b")
    store.commit_step(store.get_by_ref("triathlon", 1),
                      [(decision("review", "review_blocking", "no"), {"n": "x"})],
                      Usage(), date(2026, 9, 23), [])
    assert label_logged(store, "review", 10, lambda _p: "true") == 1
    assert store.label_targets("review", "review_blocking") == ["triathlon"]


def test_label_abandoned_only_shows_abandoned_items(capsys: pytest.CaptureFixture[str]) -> None:
    from datetime import date

    from agent_sdlc.types import Usage
    from tests.fakes import decision
    store = Store("sqlite://")
    for i in (1, 2):
        store.add_item("t", WorkItem(i, f"W{i}", "", "", "Bug", (), "u"), f"b{i}")
        store.commit_step(store.get_by_ref("t", i), [(decision("review", "review_blocking", "no"),
                                                       {"review_notes": f"notes {i}"})],
                          Usage(), date(2026, 10, 1), [])
    store.commit_step(store.get_by_ref("t", 1), [(decision("comment", "comment_intent", "question"),
                                                   {"comment": "this is obsolete, closing"})],
                      Usage(), date(2026, 10, 1), [])
    store.add_event("outcome", {"result": "abandoned"}, item=store.get_by_ref("t", 1))
    n = label_logged(store, "review", 10, lambda _: "true", abandoned_only=True)
    out = capsys.readouterr().out
    assert n == 1
    assert "t#1 (PR abandoned)" in out and "this is obsolete" in out
    assert "notes 1" in out and "notes 2" not in out
    assert [g for _, g in store.labels("review", "review_blocking")] == ["true"]
