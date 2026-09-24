import math
import random

from agent_sdlc.decisions.calibration import accuracy, apply_temperature, ece, fit_temperature


def test_temperature_one_is_identity() -> None:
    p = {"a": 0.7, "b": 0.2, "c": 0.1}
    assert apply_temperature(p, 1.0) == p


def test_temperature_above_one_flattens() -> None:
    p = apply_temperature({"a": 0.9, "b": 0.1}, 3.0)
    assert 0.5 < p["a"] < 0.9
    assert math.isclose(sum(p.values()), 1.0)


def test_temperature_handles_zero_probability() -> None:
    p = apply_temperature({"a": 1.0, "b": 0.0}, 2.0)
    assert p["a"] > 0.99 and p["b"] >= 0.0


def test_fit_returns_one_when_too_few_pairs() -> None:
    assert fit_temperature([({"a": 0.9, "b": 0.1}, "a")] * 10) == 1.0


def test_fit_detects_overconfidence() -> None:
    rng = random.Random(0)
    pairs = [({"true": 0.99, "false": 0.01}, "true" if rng.random() < 0.7 else "false")
             for _ in range(200)]
    t = fit_temperature(pairs)
    assert t > 1.5
    assert ece([(apply_temperature(p, t), g) for p, g in pairs]) < ece(pairs)


def test_ece_and_accuracy() -> None:
    pairs = [({"a": 1.0, "b": 0.0}, "a"), ({"a": 0.0, "b": 1.0}, "b")]
    assert ece(pairs) == 0.0
    assert accuracy(pairs) == 1.0
    assert accuracy([({"a": 0.6, "b": 0.4}, "b")]) == 0.0
