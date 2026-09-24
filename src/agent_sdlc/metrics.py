from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from statistics import median

from agent_sdlc.decisions.gates import GATES
from agent_sdlc.store import Event, Store
from agent_sdlc.types import ACTIVE_STAGES, Stage

_NOUL = {"yes": "true", "no": "false"}


def _p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def _dist(xs: list[float], fmt: Callable[[float], str]) -> str:
    if not xs:
        return "n/a"
    return f"median {fmt(median(xs))} · p90 {fmt(_p90(xs))} (n={len(xs)})"


def _hours(s: float) -> str:
    return f"{s / 3600:.1f}h"


def _tok(x: float) -> str:
    return f"{x:,.0f}"


def _counts(c: Counter[str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common()) or "none"


def render_metrics(store: Store, target: str, since: datetime, now: datetime) -> str:
    """Pipeline report over events in [since, now] (spec §6.3)."""
    kind: dict[str, list[Event]] = defaultdict(list)
    for e in store.events_since(since):
        if e.ts <= now:
            kind[e.kind].append(e)
    items = store.items(target)
    transitions = kind["transition"]
    out = [f"agent-sdlc metrics · {target} · {since:%Y-%m-%d} to {now:%Y-%m-%d}"]

    results = Counter(str(e.payload.get("result")) for e in kind["outcome"])
    merged, abandoned = results["merged"], results["abandoned"]
    rate = f"{merged / (merged + abandoned):.0%}" if merged + abandoned else "n/a"
    parked_now = sum(i.stage is Stage.PARKED for i in items)
    flight = sum(i.stage in (*ACTIVE_STAGES, Stage.AWAITING_HUMAN) for i in items)
    out += ["", "Outcomes",
            f"  taken in {len(kind['intake'])} · merged {merged} · abandoned {abandoned} · "
            f"parked now {parked_now} · in flight {flight} · merge rate {rate}"]

    parks = [e for e in transitions if e.payload.get("to") == "parked"]
    requeued = len(kind["requeue"])
    share = f"{requeued / len(parks):.0%}" if parks else "n/a"
    out += ["", "Parks", f"  total {len(parks)} · requeued {requeued} ({share})",
            "  by reason: " + _counts(Counter(str(e.payload.get("park_reason")) for e in parks)),
            "  by stage: " + _counts(Counter(str(e.stage) for e in parks))]

    def moves(src: set[str], dst: str) -> int:
        return sum(1 for e in transitions
                   if e.payload.get("from") in src and e.payload.get("to") == dst)

    per_item: dict[int, float] = defaultdict(float)
    per_stage: dict[str, float] = defaultdict(float)
    inp = cache = 0
    cost = 0.0
    for e in kind["agent_session"]:
        p = e.payload
        tokens = int(p.get("input_tokens", 0)) + int(p.get("output_tokens", 0))
        if e.item_id is not None:
            per_item[e.item_id] += tokens
        per_stage[str(e.stage)] += tokens
        inp += int(p.get("input_tokens", 0))
        cache += int(p.get("cache_read_tokens", 0))
        cost += float(p.get("cost_usd") or 0)
    cache_share = f"{cache / (cache + inp):.0%}" if cache + inp else "n/a"
    out += ["", "Effort",
            f"  verify/review retries {moves({'verify', 'review'}, 'implement')} · "
            f"PR rounds {moves({'awaiting_human'}, 'implement')} · "
            f"replans {moves({'plan'}, 'plan')}",
            f"  tokens per item: {_dist(list(per_item.values()), _tok)}",
            "  tokens by stage: " + (", ".join(f"{s} {_tok(v)}"
                                               for s, v in sorted(per_stage.items())) or "none"),
            f"  cache-read share {cache_share} · cost ${cost:,.2f} (where reported)"]

    intake_at = {e.item_id: e.ts for e in kind["intake"] if e.item_id is not None}
    pr_at: dict[int, datetime] = {}
    for e in transitions:
        if e.payload.get("to") == "awaiting_human" and e.item_id is not None:
            pr_at.setdefault(e.item_id, e.ts)
    to_pr = [(pr_at[i] - intake_at[i]).total_seconds() for i in pr_at if i in intake_at]
    to_merge = [(e.ts - pr_at[e.item_id]).total_seconds() for e in kind["outcome"]
                if e.payload.get("result") == "merged" and e.item_id in pr_at]
    out += ["", "Latency", f"  intake → PR open: {_dist(to_pr, _hours)}",
            f"  PR open → merged: {_dist(to_merge, _hours)}"]

    denials = Counter(f"{e.payload.get('role')} {e.payload.get('category')}"
                      for e in kind["tool_denied"])
    escalations = sum(1 for e in kind["agent_session"] if e.payload.get("escalated"))
    out += ["", "Denials", f"  total {sum(denials.values())} · escalations {escalations}"]
    out += [f"  {k}: {v}" for k, v in denials.most_common()]

    out += ["", "Gates"]
    for gate, questions in GATES.items():
        for q in questions:
            cal = store.calibration(gate, q)
            pairs = store.labeled_decisions(gate, q)
            agree = sum(_NOUL.get(a, a) == g for a, g in pairs)
            agreement = f"{agree / len(pairs):.0%}" if pairs else "n/a"
            ece = f"{cal.ece:.3f}" if cal and cal.ece is not None else "n/a"
            mode = cal.mode if cal else "shadow"
            out.append(f"  {gate}.{q}: mode {mode} · labels {len(store.labels(gate, q))} · "
                       f"ECE {ece} · agreement {agreement} (n={len(pairs)})")
    return "\n".join(out)
