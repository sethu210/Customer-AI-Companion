"""
Runs one write-action request end to end: fresh facts -> policy decision -> idempotency -> outcome -> audit.

Outcomes returned to the agent:
    executed                 done (live mode)
    queued_for_review        shadow mode: the allowed action was handed to a person instead of being executed
    queued_for_approval      above an automatic limit; a person must approve
    escalated                passed to a specialist (reason not disclosed)
    awaiting_confirmation    allowed, but the customer must confirm first
    denied                   not permitted / not applicable (see reason)
    already_handled          the same action on the same target was already executed or queued
"""

from typing import Any

from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.action_store import MockSystemOfRecord
from coded_tools.customer_service.action_store import OverLimit
from coded_tools.customer_service.action_store import idempotency_key
from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.policy import ALLOW
from coded_tools.customer_service.policy import DENY
from coded_tools.customer_service.policy import ESCALATE
from coded_tools.customer_service.policy import NEEDS_APPROVAL
from coded_tools.customer_service.policy import REFUND_ACTIONS
from coded_tools.customer_service.policy import PolicyDecision
from coded_tools.customer_service.policy import decide
from coded_tools.customer_service.policy import refund_limits

CASE_KIND = {NEEDS_APPROVAL: "approval", ESCALATE: "specialist_review", ALLOW: "shadow_review"}
OUTCOME = {NEEDS_APPROVAL: "queued_for_approval", ESCALATE: "escalated"}


class DataUnavailable(RuntimeError):
    """A fresh read failed after bounded retries; no decision can be made, so nothing is changed."""


async def fresh_facts(adapter: CustomerDataAdapter, policy: dict[str, Any],
                      customer_id: str) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Context, stuck cases and policy signals read now (never from a conversation cache), with retries."""
    out = []
    for function in ("get_customer_context", "get_stuck_cases", "get_policy_signals"):
        result = await adapter.fetch(function, customer_id, policy=policy)
        if result.failed:
            raise DataUnavailable(f"{function}: {result.status}")
        out.append(result.rows)
    return out[0], out[1], (out[2] or [{}])[0]


async def request_action(adapter: CustomerDataAdapter, store: ActionStore, policy: dict[str, Any], *,
                         customer_id: str, channel: str | None, auth_level: str | None, action: str,
                         target_id: str, customer_confirmed: bool = False, note: str = "",
                         link: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    :param link: optional {"case_id", "issue_id"} from the multi-agent case store, written to every audit,
                 action and review record so a person can trace the action back to the conversation.
    """
    mode = policy["action_mode"]
    # Keyed on the target, not the issue: two issues asking for the same refund must never execute it twice.
    key = idempotency_key(customer_id, action, target_id)
    reference = f"ACT-{key[:10].upper()}"
    link = link or {}

    previous = store.find_final(key)
    if previous:
        return {"outcome": "already_handled", "reference": previous["reference"],
                "previous_outcome": previous["outcome"], "case_id": previous.get("case_id"),
                "message_hint": "This was already handled earlier; give the customer the same reference."}

    # Fresh reads, never the conversation cache: the decision must reflect the data as it is now.
    context, stuck, signals = await fresh_facts(adapter, policy, customer_id)
    decision = decide(action, target_id, auth_level or "weak", context, stuck, signals, policy, customer_confirmed,
                      channel=channel, recent_auto_refunds=store.auto_refunds(customer_id))

    case_id = None
    receipt = None
    if decision.decision == DENY:
        outcome = "denied"
    elif decision.decision == ALLOW and decision.requires_confirmation:
        outcome = "awaiting_confirmation"
    else:
        # Claimed before anything is executed or queued: of two requests, in any process, only one acts (P4).
        outcome = "executed" if decision.decision == ALLOW and mode == "live" else \
            OUTCOME.get(decision.decision, "queued_for_review")
        try:
            claimed = store.claim(key, reference=reference, customer_id=customer_id, decision=decision,
                                  outcome=outcome, limits=refund_limits(policy) if decision.decision == ALLOW and
                                  action in REFUND_ACTIONS else None)
        except OverLimit:  # another refund was claimed since the decision: this one now needs a person (D3)
            decision = PolicyDecision(action, target_id, NEEDS_APPROVAL, "above_cumulative_limit", decision.amount,
                                      facts=decision.facts)
            outcome = OUTCOME[NEEDS_APPROVAL]
            claimed = store.claim(key, reference=reference, customer_id=customer_id, decision=decision,
                                  outcome=outcome)
        if claimed is not None:
            return {"outcome": "already_handled", "reference": claimed["reference"],
                    "previous_outcome": claimed["outcome"], "case_id": claimed.get("case_id"),
                    "message_hint": "This was already handled earlier; give the customer the same reference."}
        if outcome == "executed":
            receipt = MockSystemOfRecord(store).execute(decision, reference)
        else:
            summary = f"{action} on {target_id} (amount {decision.amount:.2f}); decision {decision.decision}"
            if note:
                summary += f"; agent note: {note[:500]}"
            case_id = store.open_case(customer_id, CASE_KIND[decision.decision], summary,
                                      proposal={"action": action, "target_id": target_id, "amount": decision.amount,
                                                "reference": reference,
                                                **{f"support_{k}": v for k, v in link.items()}}, key=key)

    store.audit(customer_id, channel, auth_level, decision, mode, outcome,
                reference if outcome not in ("denied", "awaiting_confirmation") else None, **link)
    if outcome in ("executed", "queued_for_review", "queued_for_approval", "escalated"):
        store.record_action(key, customer_id, decision, outcome, reference, case_id, receipt, link=link)

    result: dict[str, Any] = {"outcome": outcome, "action": action, "target_id": target_id,
                              "reason": decision.reason_code, "message_hint": decision.customer_hint}
    if outcome not in ("denied",):
        result["amount"] = decision.amount
    if outcome not in ("denied", "awaiting_confirmation"):
        result["reference"] = reference
    if outcome == "queued_for_review":
        result["message_hint"] = ("A team member will complete this shortly; tell the customer it is being handled "
                                  "and give the reference. Do not say it is already done, and do not say you will "
                                  "update them later: they can check their order page or come back with the "
                                  "reference.")
    if outcome == "executed":
        result["message_hint"] = "Done. Tell the customer what was done, the amount if any, and the reference."
    return result


async def escalate(store: ActionStore, *, customer_id: str, reason: str, summary: str) -> dict[str, Any]:
    case_id = store.open_case(customer_id, f"customer_request:{reason}", summary[:2000])
    return {"outcome": "escalated", "case_id": case_id,
            "message_hint": "Tell the customer a person will follow up and give them the case id. "
                            "Do not promise a time or an outcome."}
