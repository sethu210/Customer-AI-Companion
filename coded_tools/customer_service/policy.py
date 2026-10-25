"""
Deterministic policy engine for write actions.

decide() is a pure function of (action, target, fresh facts from the data layer, policy config, auth level).
The LLM only names an action and a target id; amounts, statuses and risk signals always come from the data,
so a model that is confused or manipulated cannot raise an amount or skip a check.

Decisions:
    ALLOW           safe to do automatically (possibly after the customer confirms)
    NEEDS_APPROVAL  valid, but above an automatic limit: a person must approve
    ESCALATE        valid, but account signals require a specialist (the reason is never shown to the customer)
    DENY            not permitted or not applicable
"""

import hashlib
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from pyhocon import ConfigFactory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = PROJECT_ROOT / "config" / "service_policy.hocon"

ALLOW = "ALLOW"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
ESCALATE = "ESCALATE"
DENY = "DENY"

ITEM_ACTIONS = {"initiate_refund", "retrigger_refund", "reissue_refund_store_credit", "create_return"}
ORDER_ACTIONS = {"open_carrier_trace", "cancel_order", "open_billing_review"}
ACTIONS = ITEM_ACTIONS | ORDER_ACTIONS
# Actions that pay money out: their automatic amounts count towards the cumulative limits.
REFUND_ACTIONS = {"initiate_refund", "retrigger_refund", "reissue_refund_store_credit"}

# What the agent may tell the customer for each reason code. Internal detail stays in the audit log.
CUSTOMER_HINTS = {
    "ok": "The action is permitted.",
    "step_up_required": "Account changes need a stronger sign-in. Ask the customer to log in to the app or website, "
                        "or use the verification link sent to them.",
    "not_found": "That item or order is not on this customer's account.",
    "unknown_action": "That action is not available.",
    "return_not_received": "The return has not been received at the warehouse yet, so a refund cannot start.",
    "refund_exists": "A refund for this item already exists; check its status instead.",
    "refund_not_stuck": "The refund is not stuck; it is within the normal processing time.",
    "refund_not_failed": "The refund has not failed, so it cannot be reissued.",
    "shipment_not_stuck": "The shipment is moving normally, so no carrier trace is needed.",
    "not_delivered": "The item has not been delivered yet, so a return cannot start.",
    "return_exists": "A return already exists for this item.",
    "outside_return_window": "The return window has closed for this item. Offer a hand-off to a person if the "
                             "customer wants to ask for an exception.",
    "already_shipped": "The order has already shipped, so it cannot be cancelled. Once it is delivered the customer "
                       "can ask for a return; a return cannot be set up before delivery.",
    "above_auto_limit": "This needs approval from a team member. Tell the customer it has been sent for review "
                        "and give the reference. Do not say it will be approved, completed or refunded.",
    "above_cumulative_limit": "This needs approval from a team member. Tell the customer it has been sent for review "
                              "and give the reference. Do not say it will be approved, completed or refunded.",
    "needs_specialist_review": "A specialist needs to review this. Tell the customer it has been passed on for "
                               "review and give the reference. Do not say it will be approved, completed or "
                               "refunded, and do not speculate about why.",
    "needs_customer_confirmation": "Explain exactly what will happen and ask the customer to confirm before doing it.",
    "confirm_in_channel": "Nothing was changed: requests made through a personal AI assistant cannot change "
                          "anything. Tell the customer they can do this in the ACME Retail app or website.",
    "billing_review": "The billing team needs to check this against the payment processor's records. Tell the "
                      "customer it has been passed to the billing team for review and give the reference. Do not "
                      "say a charge was duplicated or wrong, and do not say anything will be refunded.",
}


def load_policy(path: Path = POLICY_FILE) -> dict[str, Any]:
    conf = ConfigFactory.parse_file(str(path))
    policy = json.loads(json.dumps(conf.as_plain_ordered_dict()))
    policy["action_mode"] = os.environ.get("ACTION_MODE", policy.get("action_mode", "shadow")).strip().lower()
    policy["version"] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return policy


# Recommendation vocabulary of the multi-agent design. decide() keeps its own four decisions (used by the
# action layer); recommendation() translates them for the Policy agent.
REC_ALLOW = "ALLOW"
REC_DENY = "DENY"
REC_MORE_INFO = "NEEDS_MORE_INFORMATION"
REC_HUMAN = "HUMAN_APPROVAL_REQUIRED"


def recommendation(decision: "PolicyDecision") -> dict[str, Any]:
    """Maps a PolicyDecision to {recommendation, kind, missing}; carries no risk details."""
    rec: dict[str, Any] = {"recommendation": REC_DENY, "kind": None, "missing": None}
    if decision.decision == ALLOW:
        if decision.requires_confirmation:
            rec.update(recommendation=REC_MORE_INFO, missing="customer_confirmation")
        else:
            rec.update(recommendation=REC_ALLOW)
    elif decision.decision == DENY and decision.reason_code == "step_up_required":
        rec.update(recommendation=REC_MORE_INFO, missing="step_up_auth")
    elif decision.decision == NEEDS_APPROVAL:
        rec.update(recommendation=REC_HUMAN, kind="approval")
    elif decision.decision == ESCALATE:
        rec.update(recommendation=REC_HUMAN,
                   kind="billing_review" if decision.reason_code == "billing_review" else "specialist_review")
    return rec


# Keys the data's config table shares with service_policy.hocon; both must agree before any write is allowed.
DATA_REFERENCE_KEYS = ("reference_date", "return_window_days", "stuck_refund_days", "stuck_shipment_days",
                       "auto_refund_max")


def compare_data_reference(policy: dict[str, Any], data_config: dict[str, str]) -> dict[str, Any]:
    """Pure check: every DATA_REFERENCE_KEYS value in the policy must equal the data's config table."""
    mismatched = []
    for key in DATA_REFERENCE_KEYS:
        ours, theirs = policy.get(key), data_config.get(key)
        if key == "reference_date":
            same = ours is not None and theirs is not None and str(ours)[:10] == str(theirs)[:10]
        else:
            try:
                same = float(ours) == float(theirs)
            except (TypeError, ValueError):
                same = False
        if not same:
            mismatched.append({"key": key, "policy": ours, "data": theirs})
    return {"ok": not mismatched, "mismatched": mismatched, "policy_version": policy.get("version"),
            "reference_date": policy.get("reference_date")}


async def check_data_reference(adapter: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """
    Reads the data's config table and compares it with the policy. A read failure counts as a mismatch,
    so writes stay blocked until the check can actually be made.
    """
    result = await adapter.fetch("get_reference_config", policy=policy)
    if result.failed:
        return {"ok": False, "mismatched": [], "error": result.status, "policy_version": policy.get("version"),
                "reference_date": policy.get("reference_date")}
    return compare_data_reference(policy, {r["key"]: r["value"] for r in result.rows})


@dataclass
class PolicyDecision:
    action: str
    target_id: str
    decision: str
    reason_code: str
    amount: float = 0.0
    requires_confirmation: bool = False
    facts: dict[str, Any] = field(default_factory=dict)  # snapshot for the audit log only

    @property
    def customer_hint(self) -> str:
        return CUSTOMER_HINTS.get(self.reason_code, "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _deny(action: str, target_id: str, reason: str, facts: dict | None = None) -> PolicyDecision:
    return PolicyDecision(action, target_id, DENY, reason, facts=facts or {})


def _applicability(action: str, target_id: str, items: list[dict], stuck: list[dict]) -> tuple[str | None, float]:
    """Returns (deny_reason or None, amount) for the action on its target, from fresh data only."""
    if action in ITEM_ACTIONS:
        item = items[0]
        price_total = round(item["price"] * item["qty"], 2)
        refund_status = item.get("refund_status")
        if action == "initiate_refund":
            if item.get("return_status") not in ("received", "inspected") or not item.get("return_warehouse_scanned_at"):
                return "return_not_received", 0.0
            if refund_status is not None:
                return "refund_exists", 0.0
            return None, price_total
        if action == "retrigger_refund":
            is_stuck = any(c["diagnosis"] == "stuck_refund" and c["item_id"] == target_id for c in stuck)
            if refund_status not in ("pending", "processing") or not is_stuck:
                return "refund_not_stuck", 0.0
            return None, float(item["refund_amount"])
        if action == "reissue_refund_store_credit":
            if refund_status != "failed":
                return "refund_not_failed", 0.0
            return None, float(item["refund_amount"])
        if action == "create_return":
            if item.get("return_status") is not None:
                return "return_exists", 0.0
            if not item.get("delivered_on"):
                return "not_delivered", 0.0
            if not item.get("return_window_open"):
                return "outside_return_window", 0.0
            return None, 0.0
    if action == "open_carrier_trace":
        if not any(c["diagnosis"] == "stuck_shipment" and c["order_id"] == target_id for c in stuck):
            return "shipment_not_stuck", 0.0
        return None, 0.0
    if action == "cancel_order":
        if any(i["order_status"] != "processing" for i in items):
            return "already_shipped", 0.0
        return None, round(sum(i["price"] * i["qty"] for i in items), 2)
    if action == "open_billing_review":
        return None, 0.0  # any order on the account can be reviewed; the review itself is always done by a person
    return "unknown_action", 0.0


def refund_limits(policy: dict[str, Any]) -> tuple[float, float]:
    """(daily, 30-day) caps on the automatic refunds of one customer."""
    return float(policy["auto_refund_daily_max"]), float(policy["auto_refund_30d_max"])


def over_cumulative_limit(action: str, amount: float, recent: tuple[float, float], policy: dict[str, Any]) -> bool:
    """Review finding D3: this refund plus the customer's automatic refunds of the last day / 30 days pass a cap."""
    if action not in REFUND_ACTIONS or amount <= 0:
        return False
    daily, monthly = refund_limits(policy)
    return recent[0] + amount > daily or recent[1] + amount > monthly


def decide(action: str, target_id: str, auth_level: str, context: list[dict], stuck: list[dict],
           signals: dict[str, Any], policy: dict[str, Any], customer_confirmed: bool = False,
           channel: str | None = None, recent_auto_refunds: tuple[float, float] = (0.0, 0.0)) -> PolicyDecision:
    """
    :param context: rows from get_customer_context for the authenticated customer (fresh, not cached)
    :param stuck: rows from get_stuck_cases for the authenticated customer
    :param signals: the get_policy_signals row for the authenticated customer
    :param channel: the contact's channel from sly_data; read-only channels never change anything
    :param recent_auto_refunds: the customer's automatic refund amounts in the last day and 30 days (action store)
    """
    if action not in ACTIONS:
        return _deny(action, target_id, "unknown_action")
    if channel in policy.get("read_only_channels", []):
        return _deny(action, target_id, "confirm_in_channel", {"channel": channel})
    if auth_level not in policy["write_auth_levels"]:
        return _deny(action, target_id, "step_up_required", {"auth_level": auth_level})

    key = "item_id" if action in ITEM_ACTIONS else "order_id"
    items = [r for r in context if r[key] == target_id]
    if not items:
        return _deny(action, target_id, "not_found")

    reason, amount = _applicability(action, target_id, items, stuck)
    facts = {
        "item_status": items[0].get("item_status"), "order_status": items[0].get("order_status"),
        "return_status": items[0].get("return_status"), "refund_status": items[0].get("refund_status"),
        "delivered_on": items[0].get("delivered_on"), "return_window_open": items[0].get("return_window_open"),
        "stuck": sorted({c["diagnosis"] for c in stuck}), "amount": amount,
        "risk_flags": signals.get("risk_flags") or "", "refunds_365d": signals.get("refunds_365d"),
        "as_of": items[0].get("as_of"), "policy_version": policy.get("version"),
    }
    if reason:
        return _deny(action, target_id, reason, facts)

    flags = {f for f in (signals.get("risk_flags") or "").split(";") if f}
    if flags & set(policy["escalate_risk_flags"]):
        return PolicyDecision(action, target_id, ESCALATE, "needs_specialist_review", amount, facts=facts)
    if action in policy.get("always_review_actions", []):
        return PolicyDecision(action, target_id, ESCALATE, "billing_review", amount, facts=facts)
    if amount > float(policy["auto_refund_max"]):
        return PolicyDecision(action, target_id, NEEDS_APPROVAL, "above_auto_limit", amount, facts=facts)
    if over_cumulative_limit(action, amount, recent_auto_refunds, policy):
        facts["auto_refunds_day_30d"] = list(recent_auto_refunds)
        return PolicyDecision(action, target_id, NEEDS_APPROVAL, "above_cumulative_limit", amount, facts=facts)

    needs_confirmation = action in policy["requires_customer_confirmation"] and not customer_confirmed
    return PolicyDecision(action, target_id, ALLOW, "needs_customer_confirmation" if needs_confirmation else "ok",
                          amount, requires_confirmation=needs_confirmation, facts=facts)
