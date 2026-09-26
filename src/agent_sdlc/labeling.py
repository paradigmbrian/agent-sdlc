from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agent_sdlc.decisions.calibration import accuracy, apply_temperature, ece, fit_temperature
from agent_sdlc.decisions.gates import GATES, option_keys, triage_state, work_item_text
from agent_sdlc.ports import DeciderPort, ForgePort
from agent_sdlc.store import LabelInput, Store
from agent_sdlc.types import Calibration, Decision

MIN_LABELS = 30


@dataclass(frozen=True)
class CalibrationReport:
    gate: str
    question: str
    n: int
    temperature: float
    ece: float
    accuracy: float
    mode: str
    message: str


def calibrate_question(store: Store, gate: str, question: str, max_ece: float, promote: bool,
                       seed: int = 0) -> CalibrationReport:
    pairs = store.labels(gate, question)
    current = store.calibration(gate, question) or Calibration()
    if len(pairs) < MIN_LABELS:
        return CalibrationReport(gate, question, len(pairs), current.temperature, 0.0, 0.0,
                                 current.mode, f"need {MIN_LABELS} labels, have {len(pairs)}")
    rng = random.Random(seed)
    shuffled = pairs[:]
    rng.shuffle(shuffled)
    cut = int(len(shuffled) * 0.7)
    train, test = shuffled[:cut], shuffled[cut:]
    t = fit_temperature(train, min_pairs=20)
    scored = [(apply_temperature(p, t), g) for p, g in test]
    e, acc = ece(scored), accuracy(scored)
    mode = current.mode
    message = "fitted"
    if promote:
        if e <= max_ece:
            mode, message = "active", "fitted and promoted to active"
        else:
            mode, message = "shadow", f"ECE {e:.3f} > {max_ece}; kept in shadow"
    store.set_calibration(gate, question, replace(current, temperature=t, ece=e, n=len(pairs),
                                                  mode="active" if mode == "active" else "shadow"))
    return CalibrationReport(gate, question, len(pairs), t, e, acc, mode, message)


def _ask_gold(ask: Callable[[str], str], question: str, keys: list[str], hint: str) -> str | None:
    answer = ask(f"{question} {keys} [laya: {hint}] (enter = skip): ").strip()
    return answer if answer in keys else None


def label_triage(forge: ForgePort, decider: DeciderPort, store: Store, limit: int,
                 ask: Callable[[str], str], target: str | None = None) -> int:
    count = 0
    for wi in forge.list_closed(limit):
        print(f"\n=== #{wi.id} ===\n{work_item_text(wi, 2000)}")
        ds = decider.decide("triage", triage_state(wi))
        for q, d in ds.items():
            gold = _ask_gold(ask, q, option_keys(GATES["triage"][q]), d.answer)
            if gold is not None:
                store.add_label(LabelInput("triage", q, d.raw_probs, gold, "manual",
                                           target=target))
                count += 1
    return count


def _label_one(store: Store, gate: str, decision_id: int, d: Decision, state: dict[str, Any],
               ask: Callable[[str], str]) -> bool:
    print(f"\n=== decision {decision_id} ({gate}.{d.question}) ===")
    for k, v in state.items():
        print(f"--- {k} ---\n{str(v)[:1500]}")
    gold = _ask_gold(ask, d.question, option_keys(GATES[gate][d.question]), d.answer)
    if gold is None:
        return False
    store.add_label(LabelInput(gate, d.question, d.raw_probs, gold, "manual", decision_id))
    return True


def label_logged(store: Store, gate: str, limit: int, ask: Callable[[str], str],
                 abandoned_only: bool = False) -> int:
    """Label logged decisions. With abandoned_only, only decisions from items whose PR was
    abandoned, each item introduced by its logged PR comments (spec §7.1)."""
    if not abandoned_only:
        return sum(_label_one(store, gate, decision_id, d, state, ask)
                   for decision_id, d, state in store.unlabeled_decisions(gate, limit))
    count = 0
    shown: set[int] = set()
    rows = store.unlabeled_decisions_for_items(gate, store.abandoned_item_ids(), limit)
    for decision_id, item_id, d, state in rows:
        if item_id not in shown:
            shown.add(item_id)
            it = store.get(item_id)
            print(f"\n##### {it.target}#{it.external_id} (PR abandoned) #####")
            for st in store.decision_states(item_id, "comment"):
                print(f"--- PR comment ---\n{str(st.get('comment', ''))[:1500]}")
        count += _label_one(store, gate, decision_id, d, state, ask)
    return count
