"""
Controlled Action & Verification Layer for the multi-agent network. Deterministic code, no LLM.

submit() runs every sensitive operation through the same eight steps:
    1. identity and auth level come from sly_data; a failed or ambiguous identity is blocked and handed to a
       person as a security case (nothing about the account is disclosed);
    2. the target record is read fresh and must be on this customer's account        (action_service/decide)
    3. the policy decides independently of any agent's recommendation                 (action_service/decide)
    4. the issue must exist, be owned by the domain that owns the action, and the target must belong to it (with
       no reference from the customer: a target the records pin down, identified_targets);
       and the policy file must agree with the data (reference guard) before anything can change;
    5. idempotency: one key per customer + action + target, so two issues can never repeat a refund
    6. execute in shadow or live mode against the system of record                   (action_service)
    7. verify: an execution only counts when the system of record confirms it by read-back
    8. return a typed result, update the issue status and write the audit trail

evaluate() runs steps 1-4 plus the policy decision as a dry run, for the Policy, Risk & Escalation agent.
"""

import time
from typing import Any

from coded_tools.customer_service import action_service
from coded_tools.customer_service import confirmation
from coded_tools.customer_service import tracing
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import EXECUTED_VERIFIED
from coded_tools.customer_service.case_store import HANDED_OFF
from coded_tools.customer_service.case_store import PROPOSED
from coded_tools.customer_service.case_store import QUEUED_FOR_REVIEW
from coded_tools.customer_service.case_store import TERMINAL
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.policy import DENY
from coded_tools.customer_service.policy import ITEM_ACTIONS
from coded_tools.customer_service.policy import PolicyDecision
from coded_tools.customer_service.policy import check_data_reference
from coded_tools.customer_service.policy import decide
from coded_tools.customer_service.policy import recommendation
from coded_tools.customer_service.tool_support import ToolContext
from coded_tools.customer_service.tool_support import ToolError

QUEUED_OUTCOMES = {"queued_for_review", "queued_for_approval", "escalated"}
GUARD_TTL_S = 600
_GUARD_CACHE: dict[str, tuple[dict[str, Any], float]] = {}

BLOCK_HINTS = {
    "identity_failed": "The customer is not identified, so nothing can be changed. Share no account details. A "
                       "person from the security team will review the contact; give the case id.",
    "policy_data_mismatch": "Nothing was changed: the service's rules and the account data disagree on today's "
                            "settings, so no change is allowed until a person checks it. Give the case id.",
    "not_authorised_for_issue": "Nothing was changed: that action does not belong to this request. Ask the "
                                "specialist for this issue to re-check it.",
    "data_unavailable": "Nothing was changed: the account data could not be read after several tries. Tell the "
                        "customer a person will pick this up and give the case id.",
    "target_unclear": "Nothing was changed: the records do not show for certain which item or order this is about. "
                      "Ask the customer which one they mean, built from their own items (title, colour, order date), "
                      "and set the issue to awaiting_customer; then have the specialist find it with their answer.",
}
# Blocked requests that stay with the agents (the customer or a specialist can resolve them): no hand-off.
KEEP_WITH_AGENTS = {"not_authorised_for_issue", "target_unclear"}


class Blocked(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


async def reference_guard(ctx: ToolContext) -> dict[str, Any]:
    """Policy file vs the data's config table. Successful checks are cached per policy version for 10 minutes."""
    version = str(ctx.policy.get("version"))
    cached = _GUARD_CACHE.get(version)
    if cached and time.monotonic() - cached[1] < GUARD_TTL_S:
        return cached[0]
    check = await check_data_reference(ctx.adapter, ctx.policy)
    if check["ok"]:
        _GUARD_CACHE[version] = (check, time.monotonic())
    return check


def identified_targets(issue: dict[str, Any], rows: list[dict]) -> set[str]:
    """
    For an issue without references (the customer described the item): the item and order ids the records pin down,
    never a pick of the model's (review finding D2). The latest find_order_items on the issue with a unique match
    (after an ambiguous one, the customer's answer narrows it), else the only item or order on the account, else the
    only stuck case the specialist's get_stuck_cases found.
    """
    ids: set[str] = set()
    finds = [f for f in issue.get("findings") or [] if f["source"] == "find_order_items" and isinstance(f["data"], dict)]
    if finds and finds[-1]["data"].get("result") == "unique":
        ids |= {finds[-1]["data"]["items"][0]["item_id"], finds[-1]["data"]["items"][0]["order_id"]}
    items, orders = {r["item_id"] for r in rows}, {r["order_id"] for r in rows}
    if len(items) == 1:
        ids |= items
    if len(orders) == 1:
        ids |= orders
    stuck = [row for f in issue.get("findings") or [] if f["source"] == "get_stuck_cases" and isinstance(f["data"], dict)
             for row in f["data"].get("stuck_cases") or []]
    if len({(r.get("item_id"), r.get("order_id")) for r in stuck}) == 1:
        ids |= {v for v in (stuck[0].get("item_id"), stuck[0].get("order_id")) if v}
    return ids


def _target_matches_issue(issue: dict[str, Any], action: str, target_id: str, rows: list[dict]) -> bool:
    """With references on the issue, the target must be that item/order (or the item's order, or an item in it)."""
    refs = issue.get("refs") or {}
    ref_item, ref_order = refs.get("item_id"), refs.get("order_id")
    if not ref_item and not ref_order:  # a description only: the records must pin the target down (D2)
        return target_id in identified_targets(issue, rows)
    order_of = {r["item_id"]: r["order_id"] for r in rows}
    if action in ITEM_ACTIONS:
        return target_id == ref_item if ref_item else order_of.get(target_id) == ref_order
    return target_id == ref_order if ref_order else order_of.get(ref_item) == target_id


async def authorise(ctx: ToolContext, issue: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Steps 1 and 4. Returns (customer_id, proposal) or raises Blocked / ToolError."""
    if ctx.identity["identity_status"] not in IDENTIFIED or not ctx.customer_id:
        raise Blocked("identity_failed", ctx.identity["identity_status"])
    if issue["status"] in TERMINAL:
        raise ToolError(f"issue {issue['issue_id']} is already {issue['status']}; nothing was submitted.")
    proposal = issue.get("proposal")
    if issue["status"] != PROPOSED or not proposal:
        raise ToolError(f"issue {issue['issue_id']} has no proposed action; ask the specialist first.")
    if proposal["action"] not in ctx.policy["action_domains"].get(issue["domain"], []):
        raise Blocked("not_authorised_for_issue", f"{proposal['action']} is not a {issue['domain']} action")
    guard = await reference_guard(ctx)
    if not guard["ok"]:
        raise Blocked("policy_data_mismatch", str(guard.get("mismatched") or guard.get("error")))
    result = await ctx.adapter.fetch("get_customer_context", ctx.customer_id, policy=ctx.policy)
    if result.failed:
        raise Blocked("data_unavailable", result.status)
    full = next((i for i in ctx.store.case_view(issue["case_id"])["issues"] if i["issue_id"] == issue["issue_id"]),
                issue)  # with its findings: what the records pinned down
    if not _target_matches_issue(full, proposal["action"], proposal["target_id"], result.rows):
        refs = issue.get("refs") or {}
        reason = "not_authorised_for_issue" if refs.get("item_id") or refs.get("order_id") else "target_unclear"
        raise Blocked(reason, f"{proposal['target_id']} is not the target of this issue")
    return ctx.customer_id, proposal


def _block(ctx: ToolContext, issue: dict[str, Any], blocked: Blocked) -> dict[str, Any]:
    """Records a blocked request (audit + case) and hands the issue to a person when a person must act."""
    proposal = issue.get("proposal") or {}
    link = {"case_id": issue["case_id"], "issue_id": issue["issue_id"]}
    decision = PolicyDecision(proposal.get("action", "unknown"), proposal.get("target_id", ""), DENY, blocked.reason,
                              facts={"detail": blocked.detail, "policy_version": ctx.policy.get("version")})
    ActionStore().audit(ctx.customer_id, ctx.channel, ctx.auth_level, decision, ctx.policy["action_mode"],
                        "blocked", None, **link)
    status = issue["status"]
    result: dict[str, Any] = {"outcome": "blocked", "reason": blocked.reason, "case_id": issue["case_id"],
                              "issue_id": issue["issue_id"], "message_hint": BLOCK_HINTS[blocked.reason]}
    if blocked.reason not in KEEP_WITH_AGENTS:  # the others need a person; the issue is handed over
        packet = {"reason": blocked.reason, "detail": blocked.detail, "issue": issue,
                  "identity_status": ctx.identity["identity_status"], "customer_id": ctx.customer_id}
        result["handoff_id"] = ctx.store.add_handoff(issue["case_id"], [issue["issue_id"]], blocked.reason, packet)
        status = ctx.store.set_status(issue["case_id"], issue["issue_id"], HANDED_OFF, by="system",
                                      outcome={"outcome": "blocked", "reason": blocked.reason})["status"]
    result["issue_status"] = status
    return result


def confirmation_accepted(ctx: ToolContext, issue: dict[str, Any], proposal: dict[str, Any]) -> str:
    """
    '' if a customer_confirmed=true from the agent may be trusted, else why not. The agent may only pass on a
    confirmation it asked for: an earlier submit must have recorded awaiting_confirmation for this same action and
    target, in an earlier turn (the customer can only answer the question after it was asked), and the customer's
    own message in this turn must be a clear yes (confirmation.py), whatever the model concluded from it.
    """
    waiting = issue.get("outcome") or {}
    if waiting.get("outcome") != "awaiting_confirmation":
        return "the customer was not asked to confirm this action yet"
    if (waiting.get("action"), waiting.get("target_id")) != (proposal.get("action"), proposal.get("target_id")):
        return "the customer was asked to confirm a different action"
    if waiting.get("turn_id") == ctx.turn_id:
        return "the customer was asked in this same turn and has not answered yet"
    return confirmation.answer_refusal(ctx.sly_data)  # their own message must be a clear yes (D1)


def _checked_confirmation(ctx: ToolContext, issue: dict[str, Any], proposal: dict[str, Any],
                          customer_confirmed: bool) -> tuple[bool, str]:
    """(confirmed, why a claimed confirmation was not accepted)."""
    if not customer_confirmed:
        return False, ""
    refused = confirmation_accepted(ctx, issue, proposal)
    return not refused, refused


async def submit(ctx: ToolContext, issue: dict[str, Any], *, customer_confirmed: bool, note: str) -> dict[str, Any]:
    proposal = issue.get("proposal") or {}
    with tracing.span("action_layer.submit", case_id=issue["case_id"], issue_id=issue["issue_id"],
                      domain=issue["domain"], action=proposal.get("action"), mode=ctx.policy["action_mode"],
                      policy_version=ctx.policy.get("version")) as current:
        result = await _submit(ctx, issue, customer_confirmed=customer_confirmed, note=note)
        tracing.record(current, outcome=result.get("outcome"), reason=result.get("reason"),
                       reference=result.get("reference"), status=result.get("issue_status"))
        return result


async def _submit(ctx: ToolContext, issue: dict[str, Any], *, customer_confirmed: bool, note: str) -> dict[str, Any]:
    try:
        customer_id, proposal = await authorise(ctx, issue)
    except Blocked as blocked:
        return _block(ctx, issue, blocked)
    confirmed, refused = _checked_confirmation(ctx, issue, proposal, customer_confirmed)

    actions = ActionStore()
    link = {"case_id": issue["case_id"], "issue_id": issue["issue_id"]}
    try:
        result = await action_service.request_action(
            ctx.adapter, actions, ctx.policy, customer_id=customer_id, channel=ctx.channel, auth_level=ctx.auth_level,
            action=proposal["action"], target_id=proposal["target_id"], customer_confirmed=confirmed,
            note=note, link=link)
    except action_service.DataUnavailable as e:
        return _block(ctx, issue, Blocked("data_unavailable", str(e)))

    outcome = result["outcome"]
    if outcome == "already_handled":
        outcome = result.get("previous_outcome", "queued_for_review")
    record = {k: result.get(k) for k in ("outcome", "reference", "reason", "amount") if result.get(k) is not None}
    if outcome == "awaiting_confirmation":  # what was asked and when: the only confirmation a later turn may give
        record.update(action=proposal["action"], target_id=proposal["target_id"], turn_id=ctx.turn_id)
        if refused:
            result["confirmation_not_accepted"] = refused
    if outcome == "executed":
        if actions.executed_receipt(result.get("reference", "")):
            status = EXECUTED_VERIFIED
        else:  # step 7: the system of record does not confirm it, so it is never reported as done
            result.update(outcome="executed_unverified",
                          message_hint="The change could not be confirmed. Do not say it was done; tell the customer "
                                       "a person will check it and give the reference.")
            record["outcome"] = "executed_unverified"
            ctx.store.add_handoff(issue["case_id"], [issue["issue_id"]], "unverified_outcome",
                                  {"reason": "unverified_outcome", "issue": issue, "action_result": result})
            status = HANDED_OFF
    elif outcome in QUEUED_OUTCOMES:
        status = QUEUED_FOR_REVIEW
    else:  # awaiting_confirmation or denied: stays proposed until the customer confirms or the issue is closed
        status = PROPOSED
    ctx.store.set_status(issue["case_id"], issue["issue_id"], status, by="system", outcome=record)
    result["issue_status"] = status
    result["issue_id"] = issue["issue_id"]
    return result


async def evaluate(ctx: ToolContext, issue: dict[str, Any], *, customer_confirmed: bool) -> dict[str, Any]:
    """
    Dry run for the Policy agent: same checks and decision as submit(), but nothing is executed or queued. The
    only thing it records is a pending customer confirmation, so the customer's answer in the next turn counts.
    """
    with tracing.span("action_layer.evaluate", case_id=issue["case_id"], issue_id=issue["issue_id"],
                      domain=issue["domain"]) as current:
        result = await _evaluate(ctx, issue, customer_confirmed=customer_confirmed)
        tracing.record(current, recommendation=result.get("recommendation"), kind=result.get("kind"),
                       reason=result.get("reason"), action=result.get("action"))
        return result


async def _evaluate(ctx: ToolContext, issue: dict[str, Any], *, customer_confirmed: bool) -> dict[str, Any]:
    base = {"issue_id": issue["issue_id"]}
    try:
        customer_id, proposal = await authorise(ctx, issue)
        context, stuck, signals = await action_service.fresh_facts(ctx.adapter, ctx.policy, customer_id)
    except Blocked as blocked:
        rec = {"recommendation": "HUMAN_APPROVAL_REQUIRED", "kind": "security", "missing": None} \
            if blocked.reason == "identity_failed" else {"recommendation": "DENY", "kind": None, "missing": None}
        return {**base, **rec, "reason": blocked.reason, "explanation": BLOCK_HINTS[blocked.reason], "checks": []}
    except action_service.DataUnavailable:
        return {**base, "recommendation": "HUMAN_APPROVAL_REQUIRED", "kind": "tool_unavailable", "missing": None,
                "reason": "data_unavailable", "explanation": BLOCK_HINTS["data_unavailable"], "checks": []}
    confirmed, refused = _checked_confirmation(ctx, issue, proposal, customer_confirmed)
    decision = decide(proposal["action"], proposal["target_id"], ctx.auth_level or "weak", context, stuck, signals,
                      ctx.policy, confirmed, channel=ctx.channel,
                      recent_auto_refunds=ActionStore().auto_refunds(customer_id))
    if decision.requires_confirmation:  # the customer will be asked now; a later turn may pass on their answer
        ctx.store.set_status(issue["case_id"], issue["issue_id"], PROPOSED, by="system",
                             outcome={"outcome": "awaiting_confirmation", "reason": decision.reason_code,
                                      "action": proposal["action"], "target_id": proposal["target_id"],
                                      "turn_id": ctx.turn_id})
    facts = decision.facts
    checks = [
        {"check": "identity", "passed": True},
        {"check": "channel_allows_changes", "passed": decision.reason_code != "confirm_in_channel"},
        {"check": "sign_in_strength", "passed": decision.reason_code != "step_up_required"},
        {"check": "target_on_account", "passed": decision.reason_code != "not_found"},
        {"check": "rules_allow_action", "passed": decision.decision != DENY, "reason": decision.reason_code},
        {"check": "account_review_needed", "passed": decision.reason_code != "needs_specialist_review"},
        {"check": "within_automatic_limit",
         "passed": decision.reason_code not in ("above_auto_limit", "above_cumulative_limit"),
         "amount": decision.amount or None},
        {"check": "customer_confirmed", "passed": not decision.requires_confirmation,
         **({"reason": refused} if refused and decision.requires_confirmation else {})},
    ]
    return {**base, "action": proposal["action"], "target_id": proposal["target_id"], **recommendation(decision),
            "reason": decision.reason_code, "explanation": decision.customer_hint, "checks": checks,
            "as_of": facts.get("as_of"), "policy_version": facts.get("policy_version"),
            "note": "Recommendation only. submit_action re-checks everything and is the only path that changes "
                    "anything."}
