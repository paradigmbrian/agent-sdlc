from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from laya_sdlc.decisions.calibration import apply_temperature
from laya_sdlc.decisions.gates import GATES, option_keys
from laya_sdlc.types import Calibration, Decision

_DIST_KEYS = ("probabilities", "distribution", "probs")


class Predictor(Protocol):
    def predict(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]: ...


class LayaPredictor:
    def __init__(self, model: str = "auto") -> None:
        from laya import Router  # heavy import: model weights load here

        self._router = Router(preload=True)
        self._model = None if model == "auto" else model

    def predict(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        if self._model:
            result: dict[str, Any] = self._router.predict(state, questions, model=self._model)
        else:
            result = self._router.predict(state, questions)
        return result


def normalize_answer(qdef: dict[str, Any], raw: dict[str, Any]) -> dict[str, float]:
    """Turn one Laya answer into a distribution over option_keys(qdef)."""
    keys = option_keys(qdef)
    if qdef["type"] == "noul":
        p = min(max(float(raw["noul"]), 0.0), 1.0)
        return {"false": 1.0 - p, "true": p}
    dist = next((raw[k] for k in _DIST_KEYS if k in raw), None)
    probs: dict[str, float]
    if isinstance(dist, dict) and set(keys) <= {str(k) for k in dist}:
        probs = {k: float(dist[k]) for k in keys}
    elif isinstance(dist, dict | list) and len(dist) == len(keys):
        values = list(dist.values()) if isinstance(dist, dict) else list(dist)
        probs = dict(zip(keys, (float(v) for v in values), strict=True))
    else:
        conf = float(raw.get("confidence", 1.0))
        if qdef["type"] == "choice":
            top = str(raw["choice"])
        else:
            top = keys[min(max(round(float(raw["score"])), 0), len(keys) - 1)]
        rest = (1.0 - conf) / (len(keys) - 1) if len(keys) > 1 else 0.0
        probs = {k: (conf if k == top else rest) for k in keys}
    total = sum(probs.values()) or 1.0
    return {k: v / total for k, v in probs.items()}


def interpret(gate: str, question: str, qdef: dict[str, Any], raw_probs: dict[str, float],
              cal: Calibration) -> Decision:
    probs = apply_temperature(raw_probs, cal.temperature)
    shadow = cal.mode != "active"
    if qdef["type"] == "noul":
        p = probs["true"]
        answer = "yes" if p >= cal.threshold else "no" if p <= 1 - cal.threshold else "unknown"
        confidence = max(p, 1 - p)
        actionable = not shadow and answer != "unknown"
    else:
        answer = max(probs, key=lambda k: probs[k])
        confidence = probs[answer]
        actionable = not shadow and confidence >= cal.threshold
    return Decision(gate, question, answer, probs, dict(raw_probs), confidence, shadow, actionable)


class Decider:
    def __init__(self, predictor: Predictor,
                 calibrations: Callable[[str, str], Calibration | None],
                 default_threshold: float = 0.8) -> None:
        self._predictor = predictor
        self._calibrations = calibrations
        self._default = Calibration(threshold=default_threshold)

    def decide(self, gate: str, state: dict[str, Any]) -> dict[str, Decision]:
        questions = GATES[gate]
        answers = self._predictor.predict(state, questions)["answers"]
        out: dict[str, Decision] = {}
        for qid, qdef in questions.items():
            cal = self._calibrations(gate, qid) or self._default
            out[qid] = interpret(gate, qid, qdef, normalize_answer(qdef, answers[qid]), cal)
        return out
