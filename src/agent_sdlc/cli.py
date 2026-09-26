from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_sdlc.config import GlobalConfig, Loaded, load_config, load_single
from agent_sdlc.decisions.gates import GATES
from agent_sdlc.labeling import calibrate_question, label_logged, label_triage
from agent_sdlc.logctx import configure_logging
from agent_sdlc.metrics import render_metrics
from agent_sdlc.orchestrator.runtime import build_scheduler, claude_auth_env, make_forge
from agent_sdlc.orchestrator.scheduler import Scheduler, in_flight, stop_requested
from agent_sdlc.orchestrator.slots import SessionSlots
from agent_sdlc.orchestrator.supervisor import Supervisor
from agent_sdlc.orchestrator.transitions import requeue
from agent_sdlc.ports import DeciderPort
from agent_sdlc.store import Store
from agent_sdlc.targets import TargetConfig
from agent_sdlc.tracing import render_trace
from agent_sdlc.types import ACTIVE_STAGES, EventInput, Item

_STATE = Path("~/.agent-sdlc").expanduser()
_DEFAULT_DB = f"sqlite:///{_STATE / 'agent-sdlc-v2.db'}"
# Defaults resolve from the project root, not the CWD (M12).
_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sdlc")
    p.add_argument("--config", default=os.environ.get(
        "AGENT_SDLC_CONFIG", str(_ROOT / "agent-sdlc.yaml")))
    p.add_argument("--target", default=os.environ.get("AGENT_SDLC_TARGET"),
                   help="a single target file; overrides --config")
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
    st = sub.add_parser("status")
    st.add_argument("--target-name")
    rq = sub.add_parser("requeue")
    rq.add_argument("ref", help="<target>#<id>, or a bare id when only one target has it")
    rq.add_argument("--local", action="store_true", help="do not touch ADO tags")
    tr = sub.add_parser("trace")
    tr.add_argument("ref", help="<target>#<id>, or a bare id when only one target has it")
    tr.add_argument("--full", action="store_true", help="also print each session's tool calls")
    lab = sub.add_parser("label")
    lab.add_argument("gate", choices=sorted(GATES))
    lab.add_argument("--limit", type=int, default=20)
    lab.add_argument("--abandoned", action="store_true",
                     help="only decisions from items whose PR was abandoned")
    lab.add_argument("--target-name")
    cal = sub.add_parser("calibrate")
    cal.add_argument("gate", nargs="?", choices=sorted(GATES))
    cal.add_argument("--promote", action="store_true")
    me = sub.add_parser("metrics")
    me.add_argument("--days", type=int, default=30)
    me.add_argument("--target-name")
    return p


def _store(url: str) -> Store:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return Store(url)


def _load(args: argparse.Namespace) -> Loaded:
    return load_single(Path(args.target)) if args.target else load_config(Path(args.config))


def _decider(cfg: GlobalConfig, store: Store) -> DeciderPort:
    from agent_sdlc.decisions.decider import Decider, LayaPredictor, LockedDecider
    return LockedDecider(Decider(LayaPredictor(cfg.laya.model), store.calibration,
                                 cfg.laya.default_threshold))


def _scheduler(loaded: Loaded, target: TargetConfig, store: Store, args: argparse.Namespace,
              decider: DeciderPort, slots: SessionSlots, dry_run_push: bool) -> Scheduler:
    return build_scheduler(target, cfg=loaded.config, store=store, decider=decider, slots=slots,
                           workspaces=Path(args.workspaces), traces=Path(args.traces),
                           dry_run_push=dry_run_push, auth_env=claude_auth_env(loaded.config))


def resolve_ref(store: Store, names: list[str], ref: str) -> Item:
    """`<target>#<id>`, or a bare id when exactly one configured target has it (spec §4.3)."""
    if "#" in ref:
        name, _, num = ref.partition("#")
        try:
            return store.get_by_ref(name, int(num))
        except (KeyError, ValueError):
            raise LookupError(f"no item {ref}") from None
    if not ref.isdigit():
        raise LookupError(f"no item {ref} (use <target>#<id> or a number)")
    matches = store.find_external(int(ref), names)
    if not matches:
        raise LookupError(f"no item {ref}")
    if len(matches) > 1:
        refs = ", ".join(f"{m.target}#{m.external_id}" for m in matches)
        raise LookupError(f"{ref} is ambiguous: {refs}")
    return matches[0]


def _selected(loaded: Loaded, name: str | None) -> list[TargetConfig]:
    return [loaded.target(name)] if name else loaded.targets


def _ago(delta: timedelta) -> str:
    s = max(int(delta.total_seconds()), 0)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _target_status(target: TargetConfig, store: Store, now: datetime) -> None:
    tp = "yes" if stop_requested(store, target.name) else "no"
    print(f"\ntarget: {target.name}  forge: {target.forge.kind}  paused: {tp}")
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
            busy_line = f"busy: {target.name}#{item_id} {stage} for {_ago(busy_age)}"
            if busy_age > timedelta(minutes=target.limits.stale_after_minutes):
                busy_line += "  STUCK?"
            else:
                warn = ""
        print(f"last tick: {_ago(age)} ago{warn}")
        if busy_line:
            print(busy_line)
    stale_after = timedelta(minutes=target.limits.stale_after_minutes)
    busy_ids = {i.id for i in in_flight(store.items(target.name, ACTIVE_STAGES),
                                        target.limits.max_concurrent_items)}
    for i in store.items(target.name):
        reason = f" ({i.park_reason.value} from {i.parked_from.value})" \
            if i.park_reason and i.parked_from else ""
        pr = f" PR {'!' if target.forge.kind == 'ado' else '#'}{i.pr_id}" if i.pr_id else ""
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


def _status(loaded: Loaded, store: Store, name: str | None) -> None:
    now = datetime.now(UTC)
    paused = "yes" if store.get_flag("paused") == "1" else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"paused: {paused}  paused_until: {until}  "
          f"today: {today.turns} turns, {today.tokens:,} tokens")
    for target in _selected(loaded, name):
        _target_status(target, store, now)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(Path(args.logs))
    loaded: Loaded | None
    try:
        loaded = _load(args)
        load_error: Exception | None = None
    except Exception as e:  # noqa: BLE001 - the kill switch must work even with a bad
        # target file (M-2): a config load failure must not block global pause/resume.
        loaded, load_error = None, e
    store = _store(args.db)
    names = [t.name for t in loaded.targets] if loaded is not None else []

    def unknown_target(name: str | None) -> bool:
        """True (after printing the error) when `name` isn't one of the configured targets."""
        if name and name not in names:
            print(f"unknown target {name} (known: {', '.join(names)})")
            return True
        return False

    if args.cmd == "pause":
        if loaded is not None:
            if unknown_target(args.target_name):
                return 1
        elif args.target_name:
            print(f"warning: target config did not load ({load_error}); "
                  f"pausing {args.target_name} anyway")
        key = f"paused:{args.target_name}" if args.target_name else "paused"
        store.set_flag(key, "1")
        store.add_event("pause", {"target": args.target_name} if args.target_name else None)
        return 0
    if args.cmd == "resume":
        if loaded is not None:
            if unknown_target(args.target_name):
                return 1
        elif args.target_name:
            print(f"warning: target config did not load ({load_error}); "
                  f"resuming {args.target_name} anyway")
        if args.target_name:
            store.set_flag(f"paused:{args.target_name}", None)
        else:
            store.set_flag("paused", None)
            store.set_flag("paused_until", None)
        store.add_event("resume", {"target": args.target_name} if args.target_name else None)
        return 0

    if loaded is None:
        print(f"error loading targets: {load_error}")
        return 1

    if args.cmd == "status":
        if unknown_target(args.target_name):
            return 1
        _status(loaded, store, args.target_name)
    elif args.cmd in ("trace", "requeue"):
        try:
            item = resolve_ref(store, names, args.ref)
        except LookupError as e:
            print(e)
            return 1
        if args.cmd == "trace":
            print(render_trace(store, item.id, args.full))
        elif args.local:
            new = requeue(item)
            store.save(new, events=[EventInput("requeue", {
                "from_reason": item.park_reason.value if item.park_reason else None,
                "to": new.stage.value, "approved": False, "local": True})], at=item)
        else:
            target = loaded.target(item.target)
            sched = _scheduler(loaded, target, store, args, _decider(loaded.config, store),
                               SessionSlots(1), dry_run_push=False)
            sched.requeue_item(item.id)
    elif args.cmd == "calibrate":
        gates = [args.gate] if args.gate else sorted(GATES)
        for gate in gates:
            for q in GATES[gate]:
                r = calibrate_question(store, gate, q, loaded.config.laya.max_ece, args.promote)
                print(f"{gate}.{q}: n={r.n} T={r.temperature:.3f} ECE={r.ece:.3f} "
                      f"acc={r.accuracy:.3f} mode={r.mode} — {r.message}")
    elif args.cmd == "label":
        if unknown_target(args.target_name):
            return 1
        if args.gate == "triage" and not args.abandoned:
            if not args.target_name and len(names) > 1:
                print(f"choose a target with --target-name ({', '.join(names)})")
                return 1
            target = loaded.target(args.target_name or names[0])
            forge = make_forge(target, dry_run_push=True)
            n = label_triage(forge, _decider(loaded.config, store), store, args.limit, input,
                             target=target.name)
        else:
            n = label_logged(store, args.gate, args.limit, input, abandoned_only=args.abandoned)
        print(f"recorded {n} labels")
    elif args.cmd == "metrics":
        if unknown_target(args.target_name):
            return 1
        now = datetime.now(UTC)
        for target in _selected(loaded, args.target_name):
            print(render_metrics(store, target.name, now - timedelta(days=args.days), now))
    elif args.cmd == "run":
        store.set_flag("poll_s", str(args.poll))
        decider = _decider(loaded.config, store)
        slots = SessionSlots(loaded.config.limits.max_concurrent_sessions)
        sup = Supervisor(loaded.targets, lambda t: _scheduler(
            loaded, t, store, args, decider, slots, args.dry_run_push))
        if args.once:
            return 0 if sup.run_once() else 1
        sup.run_forever(args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
