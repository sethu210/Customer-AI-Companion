r"""
E3 evaluation: time-travel scenarios for the Resolution Guardian.

Each scenario runs one support conversation through the agent server (scripted model and mock data unless --live),
exactly as the website would (DemoSession: finish_turn starts the watches), then moves the simulator's clock and
injects business events, and lets the Guardian check after each step. Every check is Orbit's tick through the same
agent server (docs/orbit_tick_plan.md: Orbit -> guardian -> run_follow_ups, no model call), as in the demo; a
"restart" restarts the agent server. Scored from the records only:

    updates        the Guardian updates sent, in order, equal the expected kinds (and the review console's count)
    outcome        the watch ends in the expected state / closing reason; hand-offs have the expected reasons
    false_resolved a watch closed as resolved whose outcome the order timeline does not confirm      (must be 0)
    promises       sent updates promising a time or an outcome, or using phrases agents may never use (must be 0)
    limits         updates per customer per day within max_updates_per_day; none sent in quiet hours
    no_changes     the Guardian changed nothing: no action records after the conversation, except those a
                   (simulated) reviewer's approval made

    python -m evaluation.guardian_eval --label e3-scripted [--ids G1,G4] [--live]
Output: runtime/eval/<label>/guardian.json and guardian.md.
"""

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from dotenv import load_dotenv

from evaluation.expectations import GLOBAL_FORBIDDEN
from evaluation.expectations import ROOT

PROMISES = re.compile(r"\b(within|by (tomorrow|monday|friday)|guarantee|will (arrive|be refunded|be delivered|be "
                      r"completed)|business days|hours)\b", re.I)


@dataclass
class Scenario:
    id: str
    title: str
    customer: str
    message: str
    steps: list[tuple]  # ("advance", hours) | ("event", name) | ("sim", name) | ("restart",) | ("run",)
    updates: list[str]
    state: str
    closed_reason: str | None = None
    handoffs: list[str] = field(default_factory=list)
    review_notices: int = 0
    superseded: int = 0


SCENARIOS = [
    Scenario("G1", "refund completes: one update, verified close", "CUST-S01",
             "Where's my refund for the blue jacket?", [("advance", 14), ("event", "refund_completed")],
             ["refund_completed"], "closed", "resolved_verified"),
    Scenario("G2", "stuck shipment: delay explained, then a person at the target", "CUST-S10",
             "My shoes haven't moved in almost a week.", [("advance", 14), ("advance", 96), ("advance", 48)],
             ["parcel_delayed", "escalated"], "escalated", handoffs=["guardian_sla_breached"]),
    Scenario("G3", "review rejected: the customer hears once, from the reviewer", "CUST-S13",
             "What's happening with the refund on my overcoat?", [("advance", 14), ("event", "review_rejected")],
             [], "closed", "review_rejected", review_notices=1),
    Scenario("G4", "review approved, then the refund completes", "CUST-S13",
             "What's happening with the refund on my overcoat?",
             [("advance", 14), ("event", "review_approved"), ("advance", 6), ("event", "refund_completed")],
             ["refund_completed"], "closed", "resolved_verified", review_notices=1),
    Scenario("G5", "daily limit: four scans in a morning", "CUST-S02", "When will my sweater arrive?",
             [("advance", 14), ("event", "parcel_scanned"), ("advance", 1), ("event", "parcel_scanned"),
              ("advance", 1), ("event", "parcel_scanned"), ("advance", 1), ("event", "parcel_scanned"),
              ("advance", 24)],
             ["parcel_scanned", "parcel_scanned", "parcel_scanned"], "waiting_on_business", superseded=1),
    Scenario("G6", "worker restart loses nothing and repeats nothing", "CUST-S01",
             "Where's my refund for the blue jacket?",
             [("advance", 14), ("sim", "refund_completed"), ("restart",), ("run",), ("restart",), ("run",)],
             ["refund_completed"], "closed", "resolved_verified"),
    Scenario("G7", "completion the timeline does not confirm: never closed", "CUST-S01",
             "Where's my refund for the blue jacket?",
             [("advance", 14), ("event", "refund_completed_unconfirmed"), ("advance", 6), ("advance", 6)],
             ["escalated"], "escalated", handoffs=["guardian_unconfirmed"]),
    Scenario("G8", "quiet hours: the closing update waits for the morning", "CUST-S02",
             "When will my sweater arrive?", [("advance", 4), ("event", "parcel_delivered"), ("advance", 10)],
             ["parcel_delivered"], "closed", "resolved_verified"),
    Scenario("G9", "refund failed: a person, no re-try", "CUST-S08",
             "I sent back my running shoes ages ago, where is my money?", [("advance", 14), ("event", "refund_failed")],
             ["refund_failed"], "escalated", handoffs=["guardian_refund_failed"]),
]


def _guardian():
    from coded_tools.customer_service import guardian as g

    return g.Guardian()


async def _converse(customer_id: str, message: str, timeout_s: float) -> dict[str, Any]:
    from demo_console.catalog import Catalog
    from evaluation.agent_server import SERVER_PORT
    from demo_console.session import DemoSession

    catalog = await Catalog.aload()
    session = DemoSession(catalog.get(customer_id), catalog.gateway_sly_data(customer_id),
                          f"http://localhost:{SERVER_PORT}")
    end = {}
    async for event in session.send(message, timeout_s):
        if event["kind"] == "turn_end":
            end = event
    return {"case_id": session.case_id, "answer": end.get("answer"), "error": end.get("error")}


async def _play(sc: Scenario, case_id: str, server: Any) -> list[dict[str, Any]]:
    from coded_tools.customer_service import guardian as g
    from evaluation.orbit_client import tick

    guard = _guardian()  # this process only reads the watches and moves the simulator; the checks are Orbit's
    log = [{"step": "conversation", "summary": await tick()}]
    for step in sc.steps:
        watches = [w for w in guard.store.watches(case_id=case_id)]
        target = next((w for w in watches if g.available_events(w)), watches[0] if watches else None)
        if step[0] == "advance":
            guard.clock.advance(step[1])
            summary = await tick()
        elif step[0] == "event":
            if target is None:
                raise RuntimeError("no watch to act on")
            await g.simulate_event(guard, target["watch_id"], step[1], actor="guardian eval (simulated)", check=False)
            summary = await tick(check_customer=sc.customer)
        elif step[0] == "sim":  # a business event without a check, e.g. just before the worker stops
            rows = await guard._rows(target)
            for item in sorted({r["item_id"] for r in rows}):
                guard.clock.add_event(step[1], target=item)
            summary = {"clock": guard.clock.now_ts(), "event": step[1]}
        elif step[0] == "restart":  # the agent server (where the Guardian runs) restarts: nothing is kept in memory
            await asyncio.to_thread(server.restart)
            guard = _guardian()
            summary = {"restarted": True}
        else:
            summary = await tick(check_customer=sc.customer)
        log.append({"step": list(step), "summary": summary})
    return log


async def _false_resolved(guard: Any, watches: list[dict[str, Any]]) -> list[str]:
    """Watches closed as resolved whose last milestone a fresh read of the order timeline does not confirm."""
    bad = []
    for w in watches:
        if w["closed_reason"] != "resolved_verified" or not w["milestones"]:
            continue
        rows = await guard._rows(w)
        last = w["milestones"][-1]
        ok = await guard._refund_confirmed(w, rows) if last == "refund_completed" else \
            await guard._delivery_confirmed(w, [r for r in rows if r.get("tracking_no")]) \
            if last == "parcel_delivered" else True
        if not ok:
            bad.append(w["watch_id"])
    return bad


def _score(sc: Scenario, convo: dict[str, Any], after_turn: dict[str, int]) -> dict[str, Any]:
    from coded_tools.customer_service.action_store import ActionStore
    from coded_tools.customer_service.case_store import CaseStore

    guard = _guardian()
    settings = guard.settings
    watches = guard.store.watches(case_id=convo["case_id"]) if convo["case_id"] else []
    notices = guard.store.notices(customer_id=sc.customer)
    sent = [n for n in notices if n["status"] == "sent"]
    outbox = ActionStore().read("outbox.jsonl")
    review_notices = [o for o in outbox if o.get("source") != "guardian" and o["customer_id"] == sc.customer]
    handoffs = [h["reason"] for h in CaseStore().handoffs(status=None) if h["reason"].startswith("guardian_")]
    per_day: dict[str, int] = {}
    for n in sent:
        per_day[n["sent_at"][:10]] = per_day.get(n["sent_at"][:10], 0) + 1
    from coded_tools.customer_service.messaging import quiet as in_quiet_hours

    quiet = [n for n in sent if in_quiet_hours(_parse(n["sent_at"]), settings)]
    promises = [n["message"] for n in sent if PROMISES.search(n["message"]) or
                any(p.lower() in n["message"].lower() for p in GLOBAL_FORBIDDEN)]
    # After the conversation only a (simulated) reviewer may change anything; the Guardian never does.
    decisions = ActionStore().read("decisions.jsonl")[after_turn["decisions"]:]
    actions = ActionStore().actions()[after_turn["actions"]:]
    approved = [d for d in decisions if d.get("channel") == "review_console" and d["outcome"].startswith("approved_")]
    not_by_reviewer = [d for d in decisions if d.get("channel") != "review_console"]
    watch = watches[0] if watches else {}
    row = {
        "id": sc.id, "title": sc.title, "customer": sc.customer, "case_id": convo["case_id"],
        "watches": len(watches), "state": watch.get("state"), "closed_reason": watch.get("closed_reason"),
        "milestones": watch.get("milestones"),
        "updates": [n["kind"] for n in sent], "sent_at": [n["sent_at"] for n in sent],
        "superseded": sum(n["status"] == "superseded" for n in notices),
        "review_notices": len(review_notices), "handoffs": handoffs,
        "false_resolved": asyncio.run(_false_resolved(guard, watches)), "promises": promises,
        "max_per_day": max(per_day.values(), default=0), "sent_in_quiet_hours": len(quiet),
        "changes_not_by_reviewer": len(not_by_reviewer) + max(0, len(actions) - len(approved)),
        "conversation_error": convo.get("error"),
    }
    row["checks"] = {
        "updates": row["updates"] == sc.updates and row["review_notices"] == sc.review_notices
        and row["superseded"] == sc.superseded,
        "outcome": row["state"] == sc.state and row["closed_reason"] == sc.closed_reason and
        sorted(handoffs) == sorted(sc.handoffs),
        "false_resolved": not row["false_resolved"],
        "promises": not promises,
        "limits": row["max_per_day"] <= int(settings["max_updates_per_day"]) and not quiet,
        "no_changes": row["changes_not_by_reviewer"] == 0,
    }
    row["ok"] = all(row["checks"].values()) and row["watches"] == 1 and not row["conversation_error"]
    return row


def _parse(ts: str):
    from coded_tools.customer_service.simulator import parse_ts

    return parse_ts(ts)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(prog="guardian_eval")
    parser.add_argument("--label", required=True)
    parser.add_argument("--ids", default=",".join(s.id for s in SCENARIOS))
    parser.add_argument("--live", action="store_true", help="configured model and backend instead of scripted+mock")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    out_dir = ROOT / "runtime" / "eval" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["EVENT_SIMULATOR"] = "1"
    if not args.live:
        os.environ["CUSTOMER_DATA_BACKEND"] = "mock"

    from evaluation.agent_server import AgentServer
    from evaluation.orbit_client import ensure_token

    ensure_token()  # Orbit's ticks: the same secret for the agent server and this process
    wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
    rows = []
    for sc in [s for s in SCENARIOS if s.id in wanted]:
        store_dir = out_dir / "stores" / sc.id
        store_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SIMULATOR_STATE"] = str(store_dir / "simulator.json")
        (store_dir / "simulator.json").unlink(missing_ok=True)
        t0 = time.monotonic()
        with AgentServer(store_dir, scripted=not args.live) as server:
            from coded_tools.customer_service.action_store import ActionStore

            convo = asyncio.run(_converse(sc.customer, sc.message, args.timeout))
            after_turn = {"actions": len(ActionStore().actions()),
                          "decisions": len(ActionStore().read("decisions.jsonl"))}
            log = asyncio.run(_play(sc, convo["case_id"], server)) if convo["case_id"] else []
        row = _score(sc, convo, after_turn)
        row["seconds"] = round(time.monotonic() - t0, 1)
        row["answer"] = (convo.get("answer") or "")[:300]
        (store_dir / "steps.json").write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
        rows.append(row)
        print(f"{sc.id}: {'PASS' if row['ok'] else 'FAIL'} {json.dumps(row['checks'])} updates={row['updates']} "
              f"state={row['state']}/{row['closed_reason']}", flush=True)

    summary = {"label": args.label, "model": "live" if args.live else "scripted", "scenarios": len(rows),
               "passed": sum(r["ok"] for r in rows), "false_resolved": sum(len(r["false_resolved"]) for r in rows),
               "promises": sum(len(r["promises"]) for r in rows), "updates_sent": sum(len(r["updates"]) for r in rows)}
    (out_dir / "guardian.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str),
                                           encoding="utf-8")
    lines = [f"# Resolution Guardian ({summary['model']}): {summary['passed']}/{summary['scenarios']} pass, "
             f"{summary['false_resolved']} false resolved, {summary['promises']} promises", "",
             "| Id | Scenario | Updates sent (Guardian) | Review msgs | Hand-offs | Watch | Checks | s |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        failed = [k for k, v in r["checks"].items() if not v]
        lines.append(f"| {r['id']} | {r['title']} | {', '.join(r['updates']) or '-'} | {r['review_notices']} | "
                     f"{', '.join(r['handoffs']) or '-'} | {r['state']} / {r['closed_reason'] or '-'} | "
                     f"{'ok' if r['ok'] else 'FAIL: ' + ', '.join(failed or ['watch/conversation'])} | {r['seconds']} |")
    (out_dir / "guardian.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if summary["passed"] == summary["scenarios"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
