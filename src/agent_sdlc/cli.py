from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_sdlc.config import GlobalConfig, load_config
from agent_sdlc.decisions.gates import GATES
from agent_sdlc.labeling import calibrate_question, label_logged, label_triage
from agent_sdlc.logctx import configure_logging
from agent_sdlc.metrics import render_metrics
from agent_sdlc.orchestrator.scheduler import in_flight, stop_requested
from agent_sdlc.orchestrator.transitions import requeue
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig, load_target
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import ACTIVE_STAGES, EventInput

_STATE = Path("~/.agent-sdlc").expanduser()
_DEFAULT_DB = f"sqlite:///{_STATE / 'agent-sdlc-v2.db'}"
# Defaults resolve from the project root, not the CWD (M12).
_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sdlc")
    p.add_argument("--config", default=os.environ.get(
        "AGENT_SDLC_CONFIG", str(_ROOT / "agent-sdlc.yaml")))
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
    pa = sub.add_parser("pause")
    pa.add_argument("--target-name")
    rs = sub.add_parser("resume")
    rs.add_argument("--target-name")
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
    lab.add_argument("--abandoned", action="store_true",
                     help="only decisions from items whose PR was abandoned")
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


def _global_config(path: str) -> GlobalConfig:
    p = Path(path)
    return load_config(p).config if p.exists() else GlobalConfig()


def _runtime(cfg: GlobalConfig, target: TargetConfig, store: Store, workspaces: Path,
             dry_run_push: bool, traces: Path | None = None) -> tuple[Any, Any, Any]:
    from agent_sdlc.adapters.ado import AdoForge
    from agent_sdlc.agents.runner import ClaudeAgentRunner
    from agent_sdlc.decisions.decider import Decider, LayaPredictor
    from agent_sdlc.orchestrator.scheduler import Scheduler
    from agent_sdlc.orchestrator.stages import StageExecutor
    from agent_sdlc.policy import CommandPolicy, PathPolicy
    from agent_sdlc.secrets import ADO_PAT, ANTHROPIC_KEY, CLAUDE_TOKEN, get_secret
    from agent_sdlc.targets import AdoForgeConfig
    from agent_sdlc.workspaces import Workspaces

    pat = get_secret(*ADO_PAT)
    if cfg.auth.mode == "subscription":
        auth_env = {"CLAUDE_CODE_OAUTH_TOKEN": get_secret(*CLAUDE_TOKEN)}
    else:
        auth_env = {"ANTHROPIC_API_KEY": get_secret(*ANTHROPIC_KEY)}
    assert isinstance(target.forge, AdoForgeConfig)
    forge = AdoForge(target.forge, pat, intake=target.intake,
                     base_branch=target.repo.base_branch,
                     branch_prefix=target.repo.branch_prefix, dry_run_push=dry_run_push)
    ws = Workspaces(workspaces.resolve(), target, git_auth=forge.git_auth_header)
    pp = PathPolicy(target.policy.protected_paths)
    cp = CommandPolicy([*target.repo.install, *target.repo.commands.values()])
    runner = ClaudeAgentRunner(pp, cp, Path("~/.agent-sdlc/claude-config").expanduser(), auth_env,
                               should_stop=lambda: stop_requested(store, target.name),
                               home=ws.home, max_denials=target.limits.max_denials_per_session)
    decider = Decider(LayaPredictor(cfg.laya.model), store.calibration,
                      cfg.laya.default_threshold)
    executor = StageExecutor(target=target, forge=forge, decider=decider, runner=runner,
                             workspaces=ws, path_policy=pp, decisions_for=store.decisions_for,
                             traces=traces)
    scheduler = Scheduler(target=target, store=store, executor=executor, forge=forge,
                          workspaces=ws, limits=cfg.limits)
    return scheduler, forge, decider


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
    paused = "yes" if stop_requested(store, target.name) else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"target: {target.name}  paused: {paused}  paused_until: {until}")
    tick = store.get_flag(f"last_tick:{target.name}")
    if tick is None:
        print("last tick: never  LOOP NOT RUNNING?")
    else:
        age = now - datetime.fromisoformat(tick)
        poll = int(store.get_flag("poll_s") or 60)
        warn = "  LOOP NOT RUNNING?" if age > timedelta(seconds=3 * poll) else ""
        busy = store.get_flag(f"busy:{target.name}")
        busy_line = ""
        if busy:
            # A long step blocks the loop, so an old tick is expected while it runs (I5).
            item_id, stage, since = busy.split("|", 2)
            busy_age = now - datetime.fromisoformat(since)
            busy_line = f"busy: #{item_id} {stage} for {_ago(busy_age)}"
            if busy_age > timedelta(minutes=target.limits.stale_after_minutes):
                busy_line += "  STUCK?"
            else:
                warn = ""
        print(f"last tick: {_ago(age)} ago{warn}")
        if busy_line:
            print(busy_line)
    print(f"today: {today.turns} turns, {today.tokens:,} tokens")
    stale_after = timedelta(minutes=target.limits.stale_after_minutes)
    busy_ids = {i.id for i in in_flight(store.items(target.name, ACTIVE_STAGES),
                                        target.limits.max_concurrent_items)}
    for i in store.items(target.name):
        reason = f" ({i.park_reason.value} from {i.parked_from.value})" \
            if i.park_reason and i.parked_from else ""
        pr = f" PR !{i.pr_id}" if i.pr_id else ""
        last = store.last_event_ts(i.id)
        seen = f"last {_ago(now - last)} ago" if last else "no events"
        denied = sum((i.data.get("denial_counts") or {}).values())
        active_stale = last and i.stage in ACTIVE_STAGES and now - last > stale_after
        if active_stale and i.id in busy_ids:
            status = " STALE"
        elif active_stale:
            status = " queued"
        else:
            status = ""
        print(f"#{i.external_id:<6} {i.stage.value:<15}{reason}{pr}  attempt {i.attempt}  "
              f"{i.usage.tokens:,} tok  {seen}  denied {denied}{status}  {i.title[:60]}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(Path(args.logs))
    target = load_target(Path(args.target))
    cfg = _global_config(args.config)
    store = _store(args.db)
    traces = Path(args.traces)

    if args.cmd == "pause":
        key = f"paused:{args.target_name}" if args.target_name else "paused"
        store.set_flag(key, "1")
        store.add_event("pause", {"target": args.target_name} if args.target_name else None)
    elif args.cmd == "resume":
        if args.target_name:
            store.set_flag(f"paused:{args.target_name}", None)
        else:
            store.set_flag("paused", None)
            store.set_flag("paused_until", None)
        store.add_event("resume", {"target": args.target_name} if args.target_name else None)
    elif args.cmd == "status":
        _status(target, store)
    elif args.cmd == "trace":
        try:
            item = store.get_by_ref(target.name, args.item_id)
            print(render_trace(store, item.id, args.full))
        except KeyError:
            print(f"no item #{args.item_id}")
            return 1
    elif args.cmd == "requeue":
        item = store.get_by_ref(target.name, args.item_id)
        if args.local:
            new = requeue(item)
            store.save(new, events=[EventInput("requeue", {
                "from_reason": item.park_reason.value if item.park_reason else None,
                "to": new.stage.value, "approved": False, "local": True})], at=item)
        else:
            scheduler, *_ = _runtime(cfg, target, store, Path(args.workspaces), False, traces)
            scheduler.requeue_item(item.id)
    elif args.cmd == "calibrate":
        gates = [args.gate] if args.gate else sorted(GATES)
        for gate in gates:
            for q in GATES[gate]:
                r = calibrate_question(store, gate, q, cfg.laya.max_ece, args.promote)
                print(f"{gate}.{q}: n={r.n} T={r.temperature:.3f} ECE={r.ece:.3f} "
                      f"acc={r.accuracy:.3f} mode={r.mode} — {r.message}")
    elif args.cmd == "label":
        if args.gate == "triage" and not args.abandoned:
            _, forge, decider = _runtime(cfg, target, store, Path(args.workspaces), True, traces)
            n = label_triage(forge, decider, store, args.limit, input, target=target.name)
        else:
            n = label_logged(store, args.gate, args.limit, input, abandoned_only=args.abandoned)
        print(f"recorded {n} labels")
    elif args.cmd == "metrics":
        now = datetime.now(UTC)
        print(render_metrics(store, target.name, now - timedelta(days=args.days), now))
    elif args.cmd == "run":
        store.set_flag("poll_s", str(args.poll))
        scheduler, *_ = _runtime(cfg, target, store, Path(args.workspaces), args.dry_run_push,
                                 traces)
        if args.once:
            asyncio.run(scheduler.tick())
        else:
            asyncio.run(scheduler.run_forever(args.poll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
