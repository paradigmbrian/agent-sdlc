from __future__ import annotations

import math
from collections.abc import Sequence

Pair = tuple[dict[str, float], str]


def apply_temperature(probs: dict[str, float], t: float) -> dict[str, float]:
    """softmax(log p / t): equivalent to re-tempering the logits, since log p = z - const."""
    if t == 1.0:
        return dict(probs)
    logs = {k: math.log(max(p, 1e-12)) / t for k, p in probs.items()}
    top = max(logs.values())
    ex = {k: math.exp(v - top) for k, v in logs.items()}
    total = sum(ex.values())
    return {k: v / total for k, v in ex.items()}


def fit_temperature(pairs: Sequence[Pair], lo: float = 0.2, hi: float = 10.0, steps: int = 160,
                    min_pairs: int = 25) -> float:
    """Grid-search the temperature minimizing NLL (same method as Laya's benchmark scripts)."""
    if len(pairs) < min_pairs:
        return 1.0
    best_t, best = 1.0, float("inf")
    for i in range(steps):
        t = lo * (hi / lo) ** (i / (steps - 1))
        nll = -sum(math.log(max(apply_temperature(p, t).get(g, 0.0), 1e-12)) for p, g in pairs)
        if nll < best:
            best, best_t = nll, t
    return round(best_t, 4)


def _top(p: dict[str, float]) -> tuple[str, float]:
    k = max(p, key=lambda key: p[key])
    return k, p[k]


def accuracy(pairs: Sequence[Pair]) -> float:
    return sum(_top(p)[0] == g for p, g in pairs) / len(pairs) if pairs else 0.0


def ece(pairs: Sequence[Pair], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for p, g in pairs:
        k, conf = _top(p)
        buckets[min(int(conf * bins), bins - 1)].append((conf, k == g))
    total = 0.0
    for b in buckets:
        if b:
            conf = sum(c for c, _ in b) / len(b)
            acc = sum(ok for _, ok in b) / len(b)
            total += len(b) / len(pairs) * abs(conf - acc)
    return total
