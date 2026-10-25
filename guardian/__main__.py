r"""
Resolution Guardian (E3): the watcher, and the local event simulator that makes things happen in the demo.

    .\.venv\Scripts\python.exe -m guardian run [--every 60] [--url http://localhost:8080]   # Orbit's clock
    .\.venv\Scripts\python.exe -m guardian once [--customer CUST-S01]
    .\.venv\Scripts\python.exe -m guardian status [--customer CUST-S01]
    .\.venv\Scripts\python.exe -m guardian sim show
    .\.venv\Scripts\python.exe -m guardian sim advance --hours 6 | --days 2
    .\.venv\Scripts\python.exe -m guardian sim event <watch_id> <event>   # e.g. refund_completed, parcel_scanned
    .\.venv\Scripts\python.exe -m guardian sim reset
    .\.venv\Scripts\python.exe -m guardian opt-out --customer CUST-S01 [--undo]

Uses the same stores as the agent server (CASE_STORE_PATH, ACTION_STORE_DIR) and the simulator state
(SIMULATOR_STATE). The data reads see the simulator's events when EVENT_SIMULATOR=1 (set by this command).
Every sim command runs a check of the watches afterwards (directly: a maintenance tool).

run is Orbit's clock when no demo console is running: it sends Orbit a tick through the agent server every --every
seconds, and Orbit calls the Guardian and Aura (docs/orbit_tick_plan.md). It needs the agent server's
ORBIT_TICK_TOKEN (put it in .env, which scripts\\demo.ps1 also uses).
"""

import argparse
import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv


def _print(value) -> None:
    print(json.dumps(value, indent=2, default=str))


def main() -> None:
    load_dotenv()  # same .env as the agent server: backend choice and Databricks settings
    os.environ.setdefault("EVENT_SIMULATOR", "1")
    parser = argparse.ArgumentParser(prog="guardian", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--every", type=float, default=60.0, help="seconds between ticks")
    run.add_argument("--url", default="http://localhost:8080", help="the agent server")
    for name in ("once", "status"):
        sub.add_parser(name).add_argument("--customer")
    sim = sub.add_parser("sim").add_subparsers(dest="sim_cmd", required=True)
    sim.add_parser("show")
    sim.add_parser("reset")
    adv = sim.add_parser("advance")
    adv.add_argument("--hours", type=float, default=0)
    adv.add_argument("--days", type=float, default=0)
    ev = sim.add_parser("event")
    ev.add_argument("watch_id")
    ev.add_argument("event")
    opt = sub.add_parser("opt-out")
    opt.add_argument("--customer", required=True)
    opt.add_argument("--undo", action="store_true")
    args = parser.parse_args()

    from coded_tools.customer_service import guardian as g

    guard = g.Guardian()
    if args.cmd == "run":
        from demo_console.orbit_tick import OrbitClock

        clock = OrbitClock(args.url)
        print(f"Orbit's clock: a tick to {args.url} every {args.every:g} s (Ctrl+C to stop)", flush=True)
        while True:
            tick = asyncio.run(clock.tick())
            print(json.dumps({k: tick[k] for k in ("error", "summary", "seconds")}, default=str), flush=True)
            time.sleep(args.every)
    if args.cmd == "once":
        _print(asyncio.run(guard.run_once(customer_id=args.customer)))
    elif args.cmd == "status":
        watches = guard.store.watches(customer_id=args.customer)
        _print({"clock": guard.clock.now_ts(),
                "watches": [{**g.brief(w, guard.store), "watch_id": w["watch_id"],
                             "can_happen": g.available_events(w)} for w in watches],
                "notices": guard.store.notices(customer_id=args.customer)})
    elif args.cmd == "opt-out":
        guard.set_opt_out(args.customer, not args.undo, channel="guardian_cli")
        print(f"{args.customer}: updates {'on' if args.undo else 'off'}")
    elif args.sim_cmd == "show":
        _print(guard.clock.state())
    elif args.sim_cmd == "reset":
        guard.clock.reset()
        print(f"Simulator reset: clock {guard.clock.now_ts()}, no events")
    elif args.sim_cmd == "advance":
        try:
            guard.clock.advance(args.hours + 24 * args.days)
        except ValueError as e:
            sys.exit(str(e))
        _print(asyncio.run(guard.run_once()))
    else:
        try:
            _print(asyncio.run(g.simulate_event(guard, args.watch_id, args.event, actor="cli (simulated)")))
        except ValueError as e:
            sys.exit(f"Not done: {e}")


if __name__ == "__main__":
    main()
