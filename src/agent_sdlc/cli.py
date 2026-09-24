from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_sdlc.decisions.gates import GATES
from agent_sdlc.labeling import calibrate_question, label_logged, label_triage
from agent_sdlc.logctx import configure_logging
from agent_sdlc.metrics import render_metrics
from agent_sdlc.orchestrator.transitions import requeue
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig, load_target
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import ACTIVE_STAGES, EventInput

_STATE = Path("~/.agent-sdlc").expanduser()
_DEFAULT_DB = f"sqlite:///{_STATE / 'state.db'}"
# Defaults resolve from the project root, not the CWD (M12).
_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sdlc")
    p.add_argument("--target", default=os.environ.get(
        "AGENT_SDLC_TARGET", str(_ROOT / "targets" / "rallysource.yaml")))
    p.add_argument("--db", default=os.environ.get("AGENT_SDLC_DB", _DEFAULT_DB))
    p.add_argument("--workspaces", default=str(_ROOT / "workspaces"))
    p.add_argument("--traces", default=os.environ.get("AGENT_SDLC_TRACES",
                                                      str(_STATE / "traces")))
    p.add_argument("--logs", default=os.environ.get("AGENT_SDLC_LOGS", str(_STATE / "logs")))
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run-push", action="store_true")
    run.add_argument("--poll", type=int, default=60)
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("status")
    rq = sub.add_parser("requeue")
    rq.add_argument("item_id", type=int)
    rq.add_argument("--local", action="store_true", help="do not touch ADO tags")
    tr = sub.add_parser("trace")
    tr.add_argument("item_id", type=int)
    tr.add_argument("--full", action="store_true", help="also print each session's tool calls")
    lab = sub.add_parser("label")
    lab.add_argument("gate", choices=sorted(GATES))
    lab.add_argument("--limit", type=int, default=20)
    cal = sub.add_parser("calibrate")
    cal.add_argument("gate", nargs="?", choices=sorted(GATES))
    cal.add_argument("--promote", action="store_true")
    me = sub.add_parser("metrics")
    me.add_argument("--days", type=int, default=30)
    return p


def _store(url: str) -> Store:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return Store(url)


def _runtime(target: TargetConfig, store: Store, workspaces: Path, dry_run_push: bool,
             traces: Path | None = None) -> tuple[Any, Any, Any]:
    from agent_sdlc.adapters.ado import AdoClient
    from agent_sdlc.agents.runner import ClaudeAgentRunner
    from agent_sdlc.decisions.decider import Decider, LayaPredictor
    from agent_sdlc.orchestrator.scheduler import Scheduler
    from agent_sdlc.orchestrator.stages import StageExecutor
    from agent_sdlc.policy import CommandPolicy, PathPolicy
    from agent_sdlc.secrets import (
        ADO_PAT,
        ANTHROPIC_KEY,
        CLAUDE_TOKEN,
        basic_auth_header,
        get_secret,
    )
    from agent_sdlc.workspaces import Workspaces

    pat = get_secret(*ADO_PAT)
    if target.auth.mode == "subscription":
        auth_env = {"CLAUDE_CODE_OAUTH_TOKEN": get_secret(*CLAUDE_TOKEN)}
    else:
        auth_env = {"ANTHROPIC_API_KEY": get_secret(*ANTHROPIC_KEY)}
    ado = AdoClient(target.ado, pat, dry_run_push=dry_run_push)
    ws = Workspaces(workspaces.resolve(), target, git_auth_header=basic_auth_header(pat))
    pp = PathPolicy(target.policy.protected_paths)
    cp = CommandPolicy([target.repo.install, *target.repo.commands.values()])
    runner = ClaudeAgentRunner(pp, cp, Path("~/.agent-sdlc/claude-config").expanduser(), auth_env,
                               should_stop=lambda: store.get_flag("paused") == "1", home=ws.home,
                               max_denials=target.limits.max_denials_per_session)
    decider = Decider(LayaPredictor(target.laya.model), store.calibration,
                      target.laya.default_threshold)
    executor = StageExecutor(target=target, ado=ado, decider=decider, runner=runner,
                             workspaces=ws, path_policy=pp, decisions_for=store.decisions_for,
                             traces=traces)
    scheduler = Scheduler(target=target, store=store, executor=executor, ado=ado, workspaces=ws)
    return scheduler, ado, decider


def _ago(delta: timedelta) -> str:
    s = max(int(delta.total_seconds()), 0)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _status(target: TargetConfig, store: Store) -> None:
    now = datetime.now(UTC)
    paused = "yes" if store.get_flag("paused") == "1" else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"target: {target.name}  paused: {paused}  paused_until: {until}")
    tick = store.get_flag("last_tick")
    if tick is None:
        print("last tick: never  LOOP NOT RUNNING?")
    else:
        age = now - datetime.fromisoformat(tick)
        poll = int(store.get_flag("poll_s") or 60)
        warn = "  LOOP NOT RUNNING?" if age > timedelta(seconds=3 * poll) else ""
        print(f"last tick: {_ago(age)} ago{warn}")
    print(f"today: {today.turns} turns, {today.tokens:,} tokens")
    stale_after = timedelta(minutes=target.limits.stale_after_minutes)
    for i in store.items(target.name):
        reason = f" ({i.park_reason.value} from {i.parked_from.value})" \
            if i.park_reason and i.parked_from else ""
        pr = f" PR !{i.pr_id}" if i.pr_id else ""
        last = store.last_event_ts(i.id)
        seen = f"last {_ago(now - last)} ago" if last else "no events"
        denied = sum((i.data.get("denial_counts") or {}).values())
        stale = " STALE" if last and i.stage in ACTIVE_STAGES and now - last > stale_after \
            else ""
        print(f"#{i.id:<6} {i.stage.value:<15}{reason}{pr}  attempt {i.attempt}  "
              f"{i.usage.tokens:,} tok  {seen}  denied {denied}{stale}  {i.title[:60]}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(Path(args.logs))
    target = load_target(Path(args.target))
    store = _store(args.db)
    traces = Path(args.traces)

    if args.cmd == "pause":
        store.set_flag("paused", "1")
        store.add_event("pause")
    elif args.cmd == "resume":
        store.set_flag("paused", None)
        store.set_flag("paused_until", None)
        store.add_event("resume")
    elif args.cmd == "status":
        _status(target, store)
    elif args.cmd == "trace":
        try:
            print(render_trace(store, args.item_id, args.full))
        except KeyError:
            print(f"no item #{args.item_id}")
            return 1
    elif args.cmd == "requeue":
        if args.local:
            item = store.get(args.item_id)
            new = requeue(item)
            store.save(new, events=[EventInput("requeue", {
                "from_reason": item.park_reason.value if item.park_reason else None,
                "to": new.stage.value, "approved": False, "local": True})], at=item)
        else:
            scheduler, *_ = _runtime(target, store, Path(args.workspaces), False, traces)
            scheduler.requeue_item(args.item_id)
    elif args.cmd == "calibrate":
        gates = [args.gate] if args.gate else sorted(GATES)
        for gate in gates:
            for q in GATES[gate]:
                r = calibrate_question(store, gate, q, target.laya.max_ece, args.promote)
                print(f"{gate}.{q}: n={r.n} T={r.temperature:.3f} ECE={r.ece:.3f} "
                      f"acc={r.accuracy:.3f} mode={r.mode} — {r.message}")
    elif args.cmd == "label":
        if args.gate == "triage":
            _, ado, decider = _runtime(target, store, Path(args.workspaces), True, traces)
            n = label_triage(ado, decider, store, args.limit, input)
        else:
            n = label_logged(store, args.gate, args.limit, input)
        print(f"recorded {n} labels")
    elif args.cmd == "metrics":
        now = datetime.now(UTC)
        print(render_metrics(store, target.name, now - timedelta(days=args.days), now))
    elif args.cmd == "run":
        store.set_flag("poll_s", str(args.poll))
        scheduler, *_ = _runtime(target, store, Path(args.workspaces), args.dry_run_push,
                                 traces)
        if args.once:
            asyncio.run(scheduler.tick())
        else:
            asyncio.run(scheduler.run_forever(args.poll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
