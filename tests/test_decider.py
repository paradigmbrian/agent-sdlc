import json
import math
from pathlib import Path
from typing import Any

import pytest

from agent_sdlc.decisions.decider import Decider, interpret, normalize_answer
from agent_sdlc.decisions.gates import GATES, option_keys
from agent_sdlc.types import Calibration

FIXTURE = Path(__file__).parent / "fixtures" / "laya_triage_sample.json"
NOUL = {"type": "noul", "instructions": "x"}
CHOICE = GATES["triage"]["kind"]
SCORE = GATES["triage"]["clarity"]
ACTIVE = Calibration(threshold=0.8, mode="active")


def test_option_keys() -> None:
    assert option_keys(SCORE) == ["unclear", "partly clear", "clear"]
    assert option_keys(NOUL) == ["false", "true"]
    assert option_keys(CHOICE) == ["bug", "feature", "chore", "question"]


def test_normalize_noul() -> None:
    assert normalize_answer(NOUL, {"noul": 0.8}) == pytest.approx({"false": 0.2, "true": 0.8})


def test_normalize_choice_with_distribution() -> None:
    raw = {"choice": "bug", "confidence": 0.7,
           "probabilities": {"bug": 0.7, "feature": 0.2, "chore": 0.1, "question": 0.0}}
    assert normalize_answer(CHOICE, raw)["bug"] == pytest.approx(0.7)


def test_normalize_choice_without_distribution_spreads_rest() -> None:
    p = normalize_answer(CHOICE, {"choice": "feature", "confidence": 0.7})
    assert p["feature"] == pytest.approx(0.7) and p["bug"] == pytest.approx(0.1)


def test_normalize_score_list_distribution() -> None:
    p = normalize_answer(SCORE, {"score": 1.8, "distribution": [0.1, 0.2, 0.7]})
    assert p == pytest.approx({"unclear": 0.1, "partly clear": 0.2, "clear": 0.7})


def test_normalize_score_without_distribution_rounds_expected_level() -> None:
    p = normalize_answer(SCORE, {"score": 1.6, "confidence": 0.9})
    assert max(p, key=lambda k: p[k]) == "clear"


def test_normalize_real_fixture() -> None:
    sample = json.loads(FIXTURE.read_text())
    for qid, qdef in GATES["triage"].items():
        p = normalize_answer(qdef, sample["response"]["answers"][qid])
        assert set(p) == set(option_keys(qdef))
        assert math.isclose(sum(p.values()), 1.0, rel_tol=1e-6)


@pytest.mark.parametrize("p,answer,actionable", [
    (0.9, "yes", True), (0.1, "no", True), (0.5, "unknown", False), (0.79, "unknown", False),
])
def test_interpret_noul_bands(p: float, answer: str, actionable: bool) -> None:
    d = interpret("g", "q", NOUL, {"false": 1 - p, "true": p}, ACTIVE)
    assert (d.answer, d.actionable) == (answer, actionable)


def test_interpret_shadow_is_never_actionable() -> None:
    d = interpret("g", "q", NOUL, {"false": 0.0, "true": 1.0}, Calibration(mode="shadow"))
    assert d.answer == "yes" and d.shadow and not d.actionable


def test_interpret_choice_below_threshold() -> None:
    probs = {"bug": 0.6, "feature": 0.4, "chore": 0.0, "question": 0.0}
    d = interpret("g", "kind", CHOICE, probs, ACTIVE)
    assert d.answer == "bug" and not d.actionable


def test_interpret_applies_temperature() -> None:
    d = interpret("g", "q", NOUL, {"false": 0.05, "true": 0.95},
                  Calibration(temperature=5.0, threshold=0.8, mode="active"))
    assert d.answer == "unknown"
    assert d.raw_probs == {"false": 0.05, "true": 0.95}


class FakePredictor:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def predict(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, questions))
        return {"answers": self.answers}


def test_decider_uses_stored_calibration_per_question() -> None:
    pred = FakePredictor({"plan_addresses_item": {"noul": 0.95}, "plan_scope_ok": {"noul": 0.95}})
    cals = {("plan", "plan_addresses_item"): ACTIVE}
    decider = Decider(pred, lambda g, q: cals.get((g, q)))
    out = decider.decide("plan", {"plan": "x"})
    assert out["plan_addresses_item"].actionable is True
    assert out["plan_scope_ok"].shadow is True and out["plan_scope_ok"].actionable is False
    assert pred.calls[0][1] is GATES["plan"]


# --- final review fix wave ---------------------------------------------------------------


def test_t4_digit_keyed_distribution_maps_by_int_order() -> None:
    raw = {"score": 2, "distribution": {"2": 0.7, "1": 0.2, "0": 0.1}}
    assert normalize_answer(SCORE, raw) == pytest.approx(
        {"unclear": 0.1, "partly clear": 0.2, "clear": 0.7})
    raw10 = {"score": 0, "distribution": {"10": 0.0, "2": 0.1, "1": 0.1, "0": 0.8}}
    four = {"type": "score", "criteria": ["a", "b", "c", "d"]}
    assert normalize_answer(four, raw10) == pytest.approx({"a": 0.8, "b": 0.1, "c": 0.1, "d": 0.0})


def test_locked_decider_delegates() -> None:
    from agent_sdlc.decisions.decider import LockedDecider
    from tests.fakes import FakeDecider
    inner = FakeDecider()
    out = LockedDecider(inner).decide("plan", {"plan": "p"})
    assert set(out) == {"plan_addresses_item", "plan_scope_ok"}
    assert inner.calls == [("plan", {"plan": "p"})]
