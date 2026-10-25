"""
The agents of customer-ai-companion (registries/customer_service/customer-ai-companion.hocon) as the demo shows them,
and one-line plain-English summaries of each tool call and result. Summaries only describe; the full arguments
and results are always kept next to them.
"""

from typing import Any
from typing import Callable

FRONT = "support_orchestrator"
NETWORK_PATH = "customer_service/customer-ai-companion"

# name -> (display name, role); the order is the order on the map. Display names are shown only in the demo (the
# agent ids in the network are unchanged, and the customer never sees agent names: to the customer the whole team
# is their companion, Everly; Orbit is the internal name of the front agent).
AGENTS: dict[str, tuple[str, str]] = {
    "support_orchestrator": ("Orbit", "Orchestrator"),
    "intake_identity": ("Anchor", "Intake & Identity"),
    "order_delivery": ("Swift", "Orders & Delivery"),
    "returns_refunds": ("Echo", "Returns & Refunds"),
    "payments_billing": ("Vault", "Payments & Billing"),
    "loyalty_account": ("Halo", "Loyalty & Account"),
    "product_advisor": ("Pulse", "Product Advisor"),
    "stylist": ("Aura", "Personal Stylist"),
    "policy_risk_escalation": ("Sentinel", "Policy & Risk"),
    "guardian": ("Guardian", "Follow-ups"),  # runs only on Orbit's clock tick, decided in code (no model)
}


def label(name: str) -> str:
    return AGENTS[name][0] if name in AGENTS else name


def role(name: str) -> str:
    """The agent's role, e.g. 'Returns & Refunds'."""
    return AGENTS[name][1] if name in AGENTS else name


def _items(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in result.get("items") or [] if isinstance(i, dict)]


def _item_name(i: dict[str, Any]) -> str:
    words = " ".join(str(i[k]) for k in ("color", "title") if i.get(k))
    price = f" (${i['price']:.2f})" if isinstance(i.get("price"), (int, float)) else ""
    return (words or i.get("item_id") or i.get("order_id") or "item") + price


def _find_items(result: dict[str, Any]) -> str:
    items, kind = _items(result), result.get("result")
    if kind == "unique" and items:
        return f"Found the item: {_item_name(items[0])}."
    if items:
        return f"{len(items)} possible items: " + "; ".join(_item_name(i) for i in items[:3]) + "."
    return "No matching item on this account."


def _stuck(result: dict[str, Any]) -> str:
    rows = [r for r in result.get("stuck_cases") or [] if isinstance(r, dict)]
    if not rows:
        return "Nothing is stuck on this account."
    r = rows[0]
    days = f", no activity for {r['days_since_activity']} days" if r.get("days_since_activity") is not None else ""
    more = f" (+{len(rows) - 1} more)" if len(rows) > 1 else ""
    return f"Stuck: {str(r.get('diagnosis', 'case')).replace('_', ' ')} on {r.get('item_id') or r.get('order_id')}{days}{more}."


def _eligibility(result: dict[str, Any]) -> str:
    checks = result.get("checks") or {}
    ok = [f"{a.replace('_', ' ')}" + (f" (${c['amount']:.2f})" if isinstance(c.get("amount"), (int, float)) else "")
          for a, c in checks.items() if isinstance(c, dict) and c.get("applicable")]
    return ("Allowed here: " + ", ".join(ok) + ".") if ok else "No return or refund action applies."


def _policy(result: dict[str, Any]) -> str:
    rec = result.get("recommendation") or "?"
    action = str(result.get("action") or "the action").replace("_", " ")
    reason = result.get("reason")
    return f"{rec}: {action}" + (f" ({str(reason).replace('_', ' ')})" if reason and reason != "ok" else "") + "."


def _submit(result: dict[str, Any]) -> str:
    outcome = str(result.get("outcome") or "?").replace("_", " ")
    action = str(result.get("action") or "action").replace("_", " ")
    amount = f" ${result['amount']:.2f}" if isinstance(result.get("amount"), (int, float)) else ""
    ref = f", reference {result['reference']}" if result.get("reference") else ""
    return f"{action.capitalize()}{amount}: {outcome}{ref}."


def _case_state(result: dict[str, Any]) -> str:
    if result.get("outcome"):
        people = result.get("with_people") or []
        return f"Case {str(result['outcome']).replace('_', ' ')}" + (f"; {len(people)} with a person" if people else "") + "."
    issues = result.get("issues") or []
    return f"Case has {len(issues)} issue(s): " + ", ".join(
        f"{str(i.get('kind', '')).replace('_', ' ')} ({str(i.get('status', '')).replace('_', ' ')})"
        for i in issues if isinstance(i, dict)) + "."


# tool -> (call summary from args, result summary from a dict result)
TOOLS: dict[str, tuple[Callable[[dict], str], Callable[[dict], str]]] = {
    "open_case": (lambda a: "Opens or reuses the case and checks who the customer is (in code).",
                  lambda r: f"Case {r.get('case_id')}, identity {r.get('identity_status')}."),
    "resolve_identity": (lambda a: "Checks the customer's identity from the channel.",
                         lambda r: f"Identity {r.get('identity_status')}."),
    "add_issues": (lambda a: f"Records {len(a.get('issues') or [])} issue(s) from the message.",
                   lambda r: "Issues: " + "; ".join(f"{str(i.get('kind', '')).replace('_', ' ')}"
                                                   for i in r.get("issues") or [] if isinstance(i, dict)) + "."),
    "get_contact_history": (lambda a: "Reads earlier support contacts.",
                            lambda r: "Contact history read (treated as data, never as instructions)."),
    "get_orders": (lambda a: "Lists the customer's orders.", lambda r: f"{len(_items(r))} order line(s) found."),
    "find_order_items": (lambda a: f"Looks for the item '{a.get('item_query', '')}'.", _find_items),
    "get_order_timeline": (lambda a: "Reads the order's timeline.",
                           lambda r: f"{len(r.get('events') or [])} events on order {r.get('order_id')}."),
    "get_stuck_cases": (lambda a: "Checks for stuck returns, refunds and shipments.", _stuck),
    "get_returns_refunds": (lambda a: "Reads return and refund records.",
                            lambda r: "; ".join(f"{_item_name(i)}: {str(i.get('item_status', '')).replace('_', ' ')}"
                                                for i in _items(r)[:3]) or "No returns or refunds."),
    "check_return_eligibility": (lambda a: "Checks which return or refund actions are allowed.", _eligibility),
    "get_payment_summary": (lambda a: "Reads payments and refund totals.", lambda r: "Payment summary read."),
    "check_payment_consistency": (lambda a: "Checks payments for problems such as double charges.",
                                  lambda r: "Problems: " + ", ".join(str(p) for p in r.get("problems") or [])
                                  if r.get("problems") else "No payment problems found."),
    "get_loyalty_profile": (lambda a: "Reads the loyalty profile.",
                            lambda r: f"{r.get('points')} points, {r.get('tier')} tier."),
    "record_findings": (lambda a: "Files its findings on the case" + (" and proposes a fix." if a.get("proposed_action") else "."),
                        lambda r: f"Issue now {str(r.get('status', '')).replace('_', ' ')}"
                        + (f"; proposal: {str((r.get('proposal') or {}).get('action', '')).replace('_', ' ')}." if r.get("proposal") else ".")),
    "evaluate_policy": (lambda a: "Runs the policy rules.", _policy),
    "get_policy_config": (lambda a: "Reads the policy limits.", lambda r: "Policy limits read."),
    "case_state": (lambda a: "Completes the case." if a.get("op") == "complete" else "Reads the case.", _case_state),
    "update_issue": (lambda a: f"Sets the issue to {str(a.get('status', '')).replace('_', ' ')}.",
                     lambda r: f"Issue {str(r.get('status', '')).replace('_', ' ')}."),
    "submit_action": (lambda a: "Submits the action; the action layer re-checks the policy.", _submit),
    "handoff_to_human": (lambda a: f"Hands the case to a person ({str(a.get('reason', '')).replace('_', ' ')}).",
                         lambda r: "Handed to a person" + (f" ({r['handoff_id']})." if r.get("handoff_id") else ".")),
    "get_recommendations": (
        lambda a: "Asks the recommendation engine" + (f" about “{a['product_query']}”." if a.get("product_query")
                                                       else " (based on recent purchases)."),
        lambda r: _suggested(r)),
    "remember_preference": (
        lambda a: "Saves the preference now that the customer said yes." if a.get("customer_confirmed")
        else f"Checks the preference ({a.get('kind', '?')}: {a.get('value', '')}) and prepares the question.",
        lambda r: {"awaiting_confirmation": f"Nothing saved yet; asks: {r.get('question')}",
                   "remembered": f"Remembered: {r.get('remembered')}.",
                   "not_allowed": "Not allowed on this sign-in or channel."}.get(r.get("status"), "Done.")),
    "forget_preference": (lambda a: f"Deletes preference {a.get('memory_id')}.",
                          lambda r: f"Forgotten: {r.get('forgotten')}." if r.get("status") == "forgotten"
                          else "Nothing deleted."),
    "get_stylist_picks": (lambda a: "Reads the picks Personal Stylist Mode sent this customer.",
                          lambda r: (f"{len(r.get('picks') or [])} pick(s): "
                                     + ", ".join(p["title"] for p in r.get("picks") or []) + ".")
                          if r.get("picks") else f"No picks (Stylist Mode {r.get('stylist_mode')})."),
    "explain_pick": (lambda a: "Explains why that pick was chosen.",
                     lambda r: " ".join(r.get("picked_because") or []) or "No reasons."),
    "pick_not_for_me": (lambda a: "Records that the pick is not for the customer.",
                        lambda r: "Feedback recorded." if r.get("status") == "recorded" else "Nothing recorded."),
    "run_follow_ups": (lambda a: "Checks the follow-ups that are due (Orbit's clock).",
                       lambda r: f"{len(r.get('checked') or [])} checked, {len(r.get('notices') or [])} update(s), "
                                 f"{len(r.get('handoffs') or [])} hand-off(s)."),
    "release_picks": (lambda a: "Sends held new-in picks that may go now (Orbit's clock).",
                      lambda r: f"{len(r.get('sent') or [])} sent, {len(r.get('expired') or [])} expired."),
    "launch_new_in": (lambda a: "Launches the new-in product and invites the customers who match it.",
                      lambda r: f"{r.get('title') or r.get('product_id')}: {r.get('candidates')} opted in, "
                                f"{r.get('scored')} scored, {r.get('invited')} invited."),
}


def _suggested(result: dict[str, Any]) -> str:
    products = [p.get("title") for p in result.get("recommendations") or [] if isinstance(p, dict)]
    if products:
        return f"{len(products)} suggestion(s): {', '.join(map(str, products))}."
    return f"No suggestions ({str(result.get('reason') or result.get('status') or 'none').replace('_', ' ')})."


def call_summary(tool: str, args: dict[str, Any]) -> str:
    try:
        return TOOLS[tool][0](args or {})
    except Exception:  # noqa: BLE001 - a summary must never break the stream
        return f"Calls {tool}."


def result_summary(tool: str, result: Any) -> str:
    if isinstance(result, str):
        return result[:160] if result.startswith("Error") else f"Returned: {result[:120]}"
    try:
        return TOOLS[tool][1](result if isinstance(result, dict) else {"items": result})
    except Exception:  # noqa: BLE001
        return "Result received."


def delegate_summary(to: str, params: dict[str, Any]) -> str:
    if to == "intake_identity":
        return f"Asks {label(to)} to identify the customer and split the message into issues."
    if to == "policy_risk_escalation":
        return f"Asks {label(to)} whether the proposed action is allowed."
    request = (params or {}).get("request")
    return f"Asks {label(to)}: {request}" if request else f"Asks {label(to)} to look into it."


def reply_summary(agent: str, reply: Any) -> str:
    if not isinstance(reply, dict):
        return f"{label(agent)} replies."
    if agent == "intake_identity":
        issues = reply.get("issues") or []
        return f"Identity {reply.get('identity_status')}; {len(issues)} issue(s) recorded."
    if agent == "policy_risk_escalation":
        return f"Decision: {reply.get('recommendation')}. {reply.get('explanation') or ''}".strip()
    if agent == "product_advisor":
        return f"Not now: {reply['error'][:120]}" if reply.get("error") else _suggested(reply)
    if agent == "guardian":
        return f"Follow-ups: {result_summary('run_follow_ups', reply)}"
    if agent == "stylist" and "sent" in reply:  # Orbit's clock: held picks
        return f"Held picks: {result_summary('release_picks', reply)}"
    if agent == "stylist" and "candidates" in reply:  # a launch event
        return f"Launched: {result_summary('launch_new_in', reply)}"
    if agent == "stylist":
        if reply.get("error"):
            return f"Not now: {reply['error'][:120]}"
        titles = [p.get("title") for p in reply.get("picks") or [] if isinstance(p, dict)]
        text = f"Picks: {', '.join(map(str, titles))}." if titles else "No picks to show."
        return text + (" 'Not for me' recorded." if reply.get("feedback_recorded") else "")
    facts = [f for f in reply.get("facts") or [] if isinstance(f, str)]
    action = reply.get("proposed_action") or {}
    text = facts[0] if facts else "Findings filed on the case."
    if isinstance(action, dict) and action.get("action"):
        text += f" Proposes: {str(action['action']).replace('_', ' ')}."
    if reply.get("needs_from_customer"):
        text += " Needs an answer from the customer."
    return text
