r"""
Personal Stylist Mode (Everly companion plan P5): launches, scores, invites and feedback, on the simulator's clock.

    .\.venv\Scripts\python.exe -m stylist staged                          # products staged for launch
    .\.venv\Scripts\python.exe -m stylist launch PRD-CSC                  # score opted-in customers, send invites
    .\.venv\Scripts\python.exe -m stylist scores PRD-CSC [--top 10]       # every score with its breakdown
    .\.venv\Scripts\python.exe -m stylist invites [--customer CUST-...]
    .\.venv\Scripts\python.exe -m stylist run                             # send held invites that may go now
    .\.venv\Scripts\python.exe -m stylist opt-in --customer CUST-... [--undo]   # Stylist Mode on (demo / tests)
    .\.venv\Scripts\python.exe -m stylist feedback --customer CUST-... --product PRD-... --kind not_for_me

Uses the same stores as the agent server (CASE_STORE_PATH, ACTION_STORE_DIR, SIMULATOR_STATE). opt-in records the
customer's stylist_mode consent with channel "stylist_cli": in the product, only the customer switches it on.
"""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv


def _print(value) -> None:
    print(json.dumps(value, indent=2, default=str))


def main() -> None:
    load_dotenv()  # same .env as the agent server: backend choice and Databricks settings
    os.environ.setdefault("EVENT_SIMULATOR", "1")
    parser = argparse.ArgumentParser(prog="stylist", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("staged")
    sub.add_parser("run")
    sub.add_parser("launch").add_argument("product_id")
    scores = sub.add_parser("scores")
    scores.add_argument("product_id")
    scores.add_argument("--top", type=int, default=10)
    sub.add_parser("invites").add_argument("--customer")
    opt = sub.add_parser("opt-in")
    opt.add_argument("--customer", required=True)
    opt.add_argument("--undo", action="store_true")
    fb = sub.add_parser("feedback")
    fb.add_argument("--customer", required=True)
    fb.add_argument("--product", required=True)
    fb.add_argument("--kind", required=True, choices=["opened", "not_for_me", "bought"])
    args = parser.parse_args()

    from coded_tools.customer_service import companion as cp
    from coded_tools.customer_service.stylist import Stylist

    stylist = Stylist()
    try:
        if args.cmd == "staged":
            _print({"clock": stylist.clock.now_ts(), "staged": asyncio.run(stylist.staged())})
        elif args.cmd == "launch":
            _print(asyncio.run(stylist.launch(args.product_id, by="stylist_cli")))
        elif args.cmd == "scores":
            rows = stylist.store.scores(product_id=args.product_id)
            _print([{k: r["breakdown"].get(k) for k in ("customer_id", "score", "outcome", "gates", "points",
                                                         "penalty", "owned_item", "colours", "size")}
                    for r in rows[:args.top]])
        elif args.cmd == "invites":
            _print(stylist.store.invites(customer_id=args.customer))
        elif args.cmd == "run":
            _print(asyncio.run(stylist.run_once()))
        elif args.cmd == "opt-in":
            cp.CompanionStore(stylist.store.path).set_consent(args.customer, cp.STYLIST_MODE, not args.undo,
                                                              channel="stylist_cli")
            print(f"{args.customer}: Personal Stylist Mode {'off' if args.undo else 'on'}")
        else:
            stylist.feedback(args.customer, args.product, args.kind)
            print(f"{args.customer}: {args.kind} recorded for {args.product}")
    except ValueError as e:
        sys.exit(f"Not done: {e}")


if __name__ == "__main__":
    main()
