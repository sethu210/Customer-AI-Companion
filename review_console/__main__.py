r"""
Review console: web page or command line.

    .\.venv\Scripts\python.exe -m review_console                          # web page at http://127.0.0.1:8090
    .\.venv\Scripts\python.exe -m review_console serve --port 8091
    .\.venv\Scripts\python.exe -m review_console list [--all]
    .\.venv\Scripts\python.exe -m review_console show <review_id>
    .\.venv\Scripts\python.exe -m review_console approve <review_id> --reviewer "Sam" [--note "..."]
    .\.venv\Scripts\python.exe -m review_console reject <review_id> --reviewer "Sam" --reason "..." [--message "..."]
    .\.venv\Scripts\python.exe -m review_console resolve <review_id> --reviewer "Sam" --note "..." [--message "..."]
    .\.venv\Scripts\python.exe -m review_console outbox
"""

import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()  # same .env as the agent server: backend choice and Databricks settings
    parser = argparse.ArgumentParser(prog="review_console", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8090)
    listing = sub.add_parser("list")
    listing.add_argument("--all", action="store_true")
    sub.add_parser("show").add_argument("review_id")
    for name in ("approve", "reject", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("review_id")
        p.add_argument("--reviewer", required=True)
        if name == "approve":
            p.add_argument("--note", default="")
        if name == "reject":
            p.add_argument("--reason", required=True)
        if name == "resolve":
            p.add_argument("--note", required=True)
        if name != "approve":
            p.add_argument("--message", default="", help="text for the customer (queued in the outbox)")
    sub.add_parser("outbox")
    args = parser.parse_args()

    if args.cmd in (None, "serve"):
        import uvicorn

        uvicorn.run("review_console.app:app", host="127.0.0.1", port=getattr(args, "port", 8090))
        return

    from review_console.app import service
    from coded_tools.customer_service.review_service import ReviewError

    svc = service()
    try:
        if args.cmd == "list":
            for i in svc.queue(include_decided=args.all):
                what = f"{i.get('action') or i['kind']} {i.get('target_id') or ''}".strip()
                print(f"{i['review_id']:16} {i['status']:9} {i['type']:16} {what:40} {i['summary'][:60]}")
            return
        if args.cmd == "show":
            result = svc.detail(args.review_id)
        elif args.cmd == "approve":
            result = asyncio.run(svc.approve(args.review_id, reviewer=args.reviewer, note=args.note))
        elif args.cmd == "reject":
            result = svc.reject(args.review_id, reviewer=args.reviewer, reason=args.reason,
                                customer_message=args.message)
        elif args.cmd == "resolve":
            result = svc.resolve(args.review_id, reviewer=args.reviewer, note=args.note,
                                 customer_message=args.message)
        else:
            result = svc.outbox()
    except ReviewError as e:
        sys.exit(f"Not done: {e}")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
