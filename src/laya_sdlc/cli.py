from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from laya_sdlc.decisions.gates import GATES
from laya_sdlc.labeling import calibrate_question, label_logged, label_triage
from laya_sdlc.orchestrator.transitions import requeue
from laya_sdlc.store import Store
from laya_sdlc.targets import TargetConfig, load_target

_DEFAULT_DB = f"sqlite:///{Path('~/.laya-sdlc/state.db').expanduser()}"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="laya-sdlc")
    p.add_argument("--target", default=os.environ.get("LAYA_SDLC_TARGET",
                                                       "targets/rallysource.yaml"))
    p.add_argument("--db", default=os.environ.get("LAYA_SDLC_DB", _DEFAULT_DB))
    p.add_argument("--workspaces", default="workspaces")
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
    lab = sub.add_parser("label")
    lab.add_argument("gate", choices=sorted(GATES))
    lab.add_argument("--limit", type=int, default=20)
    cal = sub.add_parser("calibrate")
    cal.add_argument("gate", nargs="?", choices=sorted(GATES))
    cal.add_argument("--promote", action="store_true")
    return p


def _store(url: str) -> Store:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return Store(url)


def _runtime(target: TargetConfig, store: Store, workspaces: Path,
             dry_run_push: bool) -> tuple[Any, Any, Any]:
    from laya_sdlc.adapters.ado import AdoClient
    from laya_sdlc.agents.runner import ClaudeAgentRunner
    from laya_sdlc.decisions.decider import Decider, LayaPredictor
    from laya_sdlc.orchestrator.scheduler import Scheduler
    from laya_sdlc.orchestrator.stages import StageExecutor
    from laya_sdlc.policy import CommandPolicy, PathPolicy
    from laya_sdlc.secrets import (
        ADO_PAT,
        ANTHROPIC_KEY,
        CLAUDE_TOKEN,
        basic_auth_header,
        get_secret,
    )
    from laya_sdlc.workspaces import Workspaces

    pat = get_secret(*ADO_PAT)
    if target.auth.mode == "subscription":
        auth_env = {"CLAUDE_CODE_OAUTH_TOKEN": get_secret(*CLAUDE_TOKEN)}
    else:
        auth_env = {"ANTHROPIC_API_KEY": get_secret(*ANTHROPIC_KEY)}
    ado = AdoClient(target.ado, pat, dry_run_push=dry_run_push)
    ws = Workspaces(workspaces.resolve(), target, git_auth_header=basic_auth_header(pat))
    pp = PathPolicy(target.policy.protected_paths)
    cp = CommandPolicy([target.repo.install, *target.repo.commands.values()])
    runner = ClaudeAgentRunner(pp, cp, Path("~/.laya-sdlc/claude-config").expanduser(), auth_env,
                               should_stop=lambda: store.get_flag("paused") == "1")
    decider = Decider(LayaPredictor(target.laya.model), store.calibration,
                      target.laya.default_threshold)
    executor = StageExecutor(target=target, ado=ado, decider=decider, runner=runner,
                             workspaces=ws, path_policy=pp, decisions_for=store.decisions_for)
    scheduler = Scheduler(target=target, store=store, executor=executor, ado=ado, workspaces=ws)
    return scheduler, ado, decider


def _status(target: TargetConfig, store: Store) -> None:
    paused = "yes" if store.get_flag("paused") == "1" else "no"
    until = store.get_flag("paused_until") or "-"
    today = store.daily_usage(datetime.now().astimezone().date())
    print(f"target: {target.name}  paused: {paused}  paused_until: {until}")
    print(f"today: {today.turns} turns, {today.tokens:,} tokens")
    for i in store.items(target.name):
        reason = f" ({i.park_reason.value} from {i.parked_from.value})" \
            if i.park_reason and i.parked_from else ""
        pr = f" PR !{i.pr_id}" if i.pr_id else ""
        print(f"#{i.id:<6} {i.stage.value:<15}{reason}{pr}  attempt {i.attempt}  "
              f"{i.usage.tokens:,} tok  {i.title[:60]}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    target = load_target(Path(args.target))
    store = _store(args.db)

    if args.cmd == "pause":
        store.set_flag("paused", "1")
    elif args.cmd == "resume":
        store.set_flag("paused", None)
        store.set_flag("paused_until", None)
    elif args.cmd == "status":
        _status(target, store)
    elif args.cmd == "requeue":
        if args.local:
            store.save(requeue(store.get(args.item_id)))
        else:
            scheduler, *_ = _runtime(target, store, Path(args.workspaces), False)
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
            _, ado, decider = _runtime(target, store, Path(args.workspaces), True)
            n = label_triage(ado, decider, store, args.limit, input)
        else:
            n = label_logged(store, args.gate, args.limit, input)
        print(f"recorded {n} labels")
    elif args.cmd == "run":
        scheduler, *_ = _runtime(target, store, Path(args.workspaces), args.dry_run_push)
        if args.once:
            asyncio.run(scheduler.tick())
        else:
            asyncio.run(scheduler.run_forever(args.poll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
