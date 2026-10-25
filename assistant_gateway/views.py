"""
Case and order status for assistants, straight from the case store and the data layer: no LLM, fast and cheap.

Only listed fields leave the gateway (allow-lists below), all recorded by code. Never included: customer ids, tool
evidence, the agents' own wording (conclusions; ask_support gives an explanation), policy decisions' internal facts,
risk flags, payment tokens, SKUs or stock numbers.
"""

from typing import Any

from coded_tools.customer_service import guardian
from coded_tools.customer_service.case_store import CaseStore
from demo_console.session import suggestion_events

# Issue status -> (plain status, what happens next). Statuses are exact; never a promise of time or outcome.
NEXT_STEP = {
    "open": ("received", "Being looked into."),
    "investigating": ("being looked into", "Being looked into."),
    "awaiting_customer": ("waiting for you", "Support asked you a question; answer it to continue."),
    "proposed": ("fix proposed", "A fix was proposed and has not been carried out."),
    "executed_verified": ("done", "Carried out and confirmed in our systems."),
    "queued_for_review": ("with a team member", "A team member is reviewing it."),
    "review_rejected": ("reviewed", "A team member reviewed it and did not approve it."),
    "handed_off": ("with a colleague", "A colleague is following up; you will not need to repeat anything."),
    "resolved_info_only": ("answered", "Answered; nothing more to do."),
}
FOLLOW_UP = {"watching": "being followed up", "waiting_on_business": "being followed up",
             "waiting_on_customer": "waiting for your return parcel", "escalated": "with a colleague",
             "resolved": "follow-up finished", "closed": "follow-up finished"}
PENDING_CONFIRMATION = ("waiting for your confirmation",
                        "Nothing was changed. Changes asked for through an assistant are confirmed in the ACME Retail "
                        "app or website.")

ORDER_FIELDS = ("order_id", "placed_on", "order_status")
ITEM_FIELDS = ("title", "color", "size", "qty", "price", "currency", "item_status", "delivered_on",
               "return_window_open")
SHIPMENT_FIELDS = ("carrier", "tracking_no", "shipment_status", "last_shipment_event", "last_shipment_location",
                   "last_shipment_event_at", "delivered_on")
RETURN_FIELDS = ("return_status", "return_label_created_at", "return_warehouse_scanned_at")
REFUND_FIELDS = ("refund_status", "refund_amount", "refund_method", "refund_initiated_at", "refund_completed_at",
                 "refund_failure_reason")


def _issue(issue: dict[str, Any]) -> dict[str, Any]:
    outcome = issue.get("outcome") or {}
    status, next_step = NEXT_STEP.get(issue["status"], (issue["status"].replace("_", " "), ""))
    if issue["status"] == "proposed" and outcome.get("outcome") == "awaiting_confirmation":
        status, next_step = PENDING_CONFIRMATION
    out = {"request": issue.get("summary"), "area": issue["domain"].replace("_", " "), "status": status,
           "next_step": next_step}
    if outcome.get("reference"):
        out["reference"] = outcome["reference"]
    if outcome.get("outcome") == "denied" and outcome.get("reason"):
        out["refused_because"] = outcome["reason"].replace("_", " ")
    return out


def _follow_up(watch: dict[str, Any]) -> dict[str, Any]:
    """The Resolution Guardian's watch on a request: what it waits for and when it last checked (E3)."""
    brief = guardian.brief(watch)
    return {"state": FOLLOW_UP.get(watch["state"], watch["state"]), "waiting_for": brief["waiting_for"],
            "last_checked": brief["last_checked_at"]}


def case_summary(view: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """One case as an assistant may see it. `view` is CaseStore.case_view, `case` its cases row."""
    suggestions = [{k: item.get(k) for k in ("title", "because", "list_price", "currency", "options")}
                   for event in suggestion_events(view, set()) for item in event["items"]]
    watched = {w["issue_id"]: w for w in guardian.GuardianStore().watches(case_id=view["case_id"])
               if w["closed_reason"] != "nothing_pending"}
    requests = []
    for issue in view["issues"]:
        request = _issue(issue)
        if issue["issue_id"] in watched:
            request["follow_up"] = _follow_up(watched[issue["issue_id"]])
        requests.append(request)
    out = {"case_id": view["case_id"], "opened": case["created_at"], "last_update": case["updated_at"],
           "case_status": {"open": "open", "resolved": "resolved", "closed_with_handoff": "with a colleague"}.get(
               view["status"], view["status"].replace("_", " ")),
           "requests": requests}
    if suggestions:
        out["suggested_products"] = suggestions
    return out


def case_status(store: CaseStore, customer_id: str, case_id: str | None = None, limit: int = 5) -> dict[str, Any]:
    """The customer's recent cases, or one case. A case id that is not theirs is reported as not found."""
    cases = store.customer_cases(customer_id, limit=50 if case_id else limit)
    if case_id:
        cases = [c for c in cases if c["case_id"] == case_id.strip().upper()]
        if not cases:
            return {"found": False, "message": "No case with that reference on this account."}
    return {"found": bool(cases), "cases": [case_summary(store.case_view(c["case_id"]), c) for c in cases],
            **({} if cases else {"message": "No support cases on this account yet."})}


def order_status(rows: list[dict[str, Any]], order_ref: str | None = None) -> dict[str, Any]:
    """
    Orders with their items, shipment, return and refund status, from get_customer_context rows (one per item).
    order_ref: an order id, or a few words matched against item titles and colours ("blue jacket").
    """
    orders: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["placed_on"], r["order_id"]), reverse=True):
        order = orders.setdefault(r["order_id"], {**{k: r.get(k) for k in ORDER_FIELDS}, "items": []})
        item = {k: r.get(k) for k in ITEM_FIELDS}
        if r.get("carrier") or r.get("shipment_status"):
            item["shipment"] = {k: r.get(k) for k in SHIPMENT_FIELDS}
        if r.get("return_status"):
            item["return"] = {k: r.get(k) for k in RETURN_FIELDS}
        if r.get("refund_status"):
            item["refund"] = {k: r.get(k) for k in REFUND_FIELDS}
        order["items"].append(item)
    found = list(orders.values())
    ref = (order_ref or "").strip()
    if ref:
        words = ref.lower().split()
        found = [o for o in found if o["order_id"].lower() == ref.lower()] or [
            o for o in found if any(all(w in f"{i.get('color') or ''} {i.get('title') or ''}".lower() for w in words)
                                    for i in o["items"])]
        if not found:
            return {"found": False, "message": "No order on this account matches that.", "as_of": _as_of(rows)}
    return {"found": bool(found), "orders": found, "as_of": _as_of(rows),
            "note": "Card refunds usually take 5-10 business days to appear after the refund completes."}


def _as_of(rows: list[dict[str, Any]]) -> str | None:
    return rows[0].get("as_of") if rows else None
