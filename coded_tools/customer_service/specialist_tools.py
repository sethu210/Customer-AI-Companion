"""
Coded tools for the specialist agents (Order & Delivery, Returns & Refunds, Payments & Billing, Loyalty & Account).

Every read is filed under an issue: the rows it returned are stored as that issue's evidence (author "tool"),
so the orchestrator decides from data recorded by code, not from an agent's retelling. A specialist's own
conclusion and any proposed action are stored separately (author "agent") by record_findings, which only
accepts actions the issue's domain owns and targets that are on the customer's account.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service.case_store import INVESTIGATING
from coded_tools.customer_service.case_store import OPEN
from coded_tools.customer_service.case_store import PROPOSED
from coded_tools.customer_service.case_store import RESOLVED_INFO_ONLY
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.orchestrator_tools import _needs_decision
from coded_tools.customer_service.policy import ITEM_ACTIONS
from coded_tools.customer_service.policy import _applicability
from coded_tools.customer_service.tool_support import ToolContext
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import compact
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import read
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

ORDER = ("order_delivery",)
RETURNS = ("returns_refunds",)
PAYMENTS = ("payments_billing",)
LOYALTY = ("loyalty_account",)
BOTH = ORDER + RETURNS
ORDER_LOOKUPS = ORDER + RETURNS + PAYMENTS  # domains that need to find an order or see its timeline
ALL_SPECIALISTS = ORDER + RETURNS + PAYMENTS + LOYALTY

ORDER_FIELDS = ("order_id", "placed_on", "order_status", "item_id", "title", "color", "size", "qty", "price",
                "currency", "item_status", "carrier", "tracking_no", "shipment_status", "last_shipment_event",
                "last_shipment_location", "last_shipment_event_at", "delivered_on")
RETURN_FIELDS = ("order_id", "item_id", "title", "color", "size", "qty", "price", "currency", "item_status",
                 "delivered_on", "return_window_open", "return_status", "return_label_created_at",
                 "return_warehouse_scanned_at", "refund_status", "refund_amount", "refund_method",
                 "refund_initiated_at", "refund_completed_at", "refund_failure_reason", "payment_last4")
STUCK_DIAGNOSES = {"order_delivery": {"stuck_shipment"}, "returns_refunds": {"stuck_refund", "refund_failed"}}


def _needs_person(issue: dict[str, Any]) -> str:
    """Why a loyalty issue may not be closed by its specialist, or '': missing points and tier disagreements go to a
    person (there is no points history, and the tier records are a data problem)."""
    if issue.get("kind") == "missing_points":
        return "missing points cannot be traced here; a person picks this up"
    for finding in issue["findings"]:
        if finding["source"] == "get_loyalty_profile" and isinstance(finding["data"], dict) \
                and finding["data"].get("tier_records_agree") is False:
            return "the account's tier records disagree (data_inconsistency); a person picks this up"
    return ""


class _IssueRead(CodedTool):
    """Base: resolves the issue, runs one read, stores the result as tool evidence, returns it."""

    domains: tuple[str, ...] = BOTH
    name = "read"

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            customer_id = ctx.require_customer()
            issue = ctx.require_issue(args.get("issue_id"), self.domains)
            result = await self.run(ctx, customer_id, issue, args)
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source=self.name, author="tool", data=result)
            if issue["status"] == OPEN:
                ctx.store.set_status(issue["case_id"], issue["issue_id"], INVESTIGATING, by="system")
            return result
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001 - never let a failure turn into a guess
            LOGGER.exception("%s failed", self.name)
            return error(f"customer_data_unavailable ({type(e).__name__}). Do not guess.")

    async def run(self, ctx: ToolContext, customer_id: str, issue: dict[str, Any],
                  args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class GetOrders(_IssueRead):
    """Orders, items and shipment status (optionally one order)."""

    domains = ORDER
    name = "get_orders"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_customer_context", customer_id)
        order_id = str(args.get("order_id") or "").strip()
        if order_id:
            rows = [r for r in rows if r["order_id"] == order_id]
            if not rows:
                raise ToolError(f"order {order_id} was not found on this customer's account.")
        return {"as_of": rows[0]["as_of"] if rows else None, "items": compact(rows, keep=ORDER_FIELDS)}


class GetReturnsRefunds(_IssueRead):
    """Delivery date, return window, return and refund status per item (optionally one item)."""

    domains = RETURNS
    name = "get_returns_refunds"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_customer_context", customer_id)
        item_id = str(args.get("item_id") or "").strip()
        if item_id:
            rows = [r for r in rows if r["item_id"] == item_id]
            if not rows:
                raise ToolError(f"item {item_id} was not found on this customer's account.")
        return {"as_of": rows[0]["as_of"] if rows else None, "items": compact(rows, keep=RETURN_FIELDS)}


class FindItems(_IssueRead):
    """Resolves a description such as 'blue jacket' to the customer's own items: unique, ambiguous or no_match."""

    domains = ORDER_LOOKUPS
    name = "find_order_items"

    async def run(self, ctx, customer_id, issue, args):
        query = str(args.get("item_query") or issue["refs"].get("item_query") or "").strip()[:200]
        if not query:
            raise ToolError("item_query is required, e.g. 'blue jacket'.")
        rows = await read(ctx, "find_order_items", customer_id, query)
        verdict = "no_match" if not rows else ("unique" if len(rows) == 1 else "ambiguous")
        return {"item_query": query, "result": verdict, "items": compact(rows, drop=("as_of", "match_score"))}


def _matched_order_id(ctx: ToolContext, issue: dict[str, Any]) -> str:
    """The order of the latest unique find_order_items match filed on this issue, or '' if there is none."""
    view = ctx.store.case_view(issue["case_id"])
    findings = next((i["findings"] for i in view["issues"] if i["issue_id"] == issue["issue_id"]), [])
    for finding in reversed(findings):
        data = finding["data"]
        if finding["source"] == "find_order_items" and isinstance(data, dict) and data.get("result") == "unique":
            return data["items"][0]["order_id"]
    return ""


class OrderTimeline(_IssueRead):
    """Every dated event on one order: carrier scans, return label and scan, refund steps, support contacts."""

    domains = ORDER_LOOKUPS
    name = "get_order_timeline"

    async def run(self, ctx, customer_id, issue, args):
        order_id = str(args.get("order_id") or issue["refs"].get("order_id") or "").strip()[:64]
        order_id = order_id or _matched_order_id(ctx, issue)
        if not order_id:
            raise ToolError("order_id is required: call find_order_items first, or pass the order_id.")
        rows = await read(ctx, "get_case_timeline", customer_id, order_id)
        if not rows:
            raise ToolError(f"order {order_id} was not found on this customer's account.")
        return {"order_id": order_id, "as_of": rows[0]["as_of"], "events": compact(rows)}


class StuckCases(_IssueRead):
    """Stuck cases in this issue's domain: stuck shipments, or stuck and failed refunds."""

    name = "get_stuck_cases"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_stuck_cases", customer_id)
        wanted = STUCK_DIAGNOSES.get(issue["domain"], set())
        rows = [r for r in rows if r["diagnosis"] in wanted]
        return {"stuck_cases": compact(rows), "note": "No rows means nothing in this area is stuck."}


class CheckReturnEligibility(_IssueRead):
    """For one item: which return/refund actions the rules allow on today's data, and why not for the others."""

    domains = RETURNS
    name = "check_return_eligibility"

    async def run(self, ctx, customer_id, issue, args):
        item_id = str(args.get("item_id") or issue["refs"].get("item_id") or "").strip()[:64]
        if not item_id:
            raise ToolError("item_id is required.")
        rows = [r for r in await read(ctx, "get_customer_context", customer_id) if r["item_id"] == item_id]
        if not rows:
            raise ToolError(f"item {item_id} was not found on this customer's account.")
        stuck = await read(ctx, "get_stuck_cases", customer_id)
        checks = {}
        for action in ctx.policy["action_domains"]["returns_refunds"]:
            reason, amount = _applicability(action, item_id, rows, stuck)
            checks[action] = {"applicable": reason is None, "reason": reason or "ok", "amount": amount or None}
        return {"item_id": item_id, "as_of": rows[0]["as_of"], "checks": checks,
                "note": "Applicability only. Sign-in strength, account checks, limits and customer confirmation "
                        "are decided when the action is submitted."}


PAYMENT_NOTICE = ("The payment records hold one payment per order with its status and card ending only: no amount, "
                  "currency, timestamp or processor data. A duplicate or wrong charge can therefore be neither "
                  "confirmed nor ruled out from them; only the billing team can check it with the processor.")


def expected_payment_status(order_status: str, order_total: float, completed_refunds: float) -> str:
    """The payment status the order and its completed refunds imply (store credit counts as refunded)."""
    if completed_refunds <= 0:
        return "authorized" if order_status in ("processing", "cancelled") else "captured"
    return "refunded" if completed_refunds >= order_total else "partially_refunded"


def payment_consistency(row: dict[str, Any]) -> dict[str, Any]:
    """Deterministic check of one order's payment record against its total, status and refunds."""
    problems = []
    if not row.get("payment_id"):
        return {"order_id": row["order_id"], "verdict": "insufficient_evidence", "problems": ["no_payment_record"]}
    if row["payments_on_order"] > 1:
        problems.append("multiple_payment_records")
    expected = expected_payment_status(row["order_status"], row["order_total"], row["refunds_completed_total"])
    if row["payment_status"] != expected:
        problems.append(f"payment_status_{row['payment_status']}_but_records_imply_{expected}")
    if row["refunds_completed_total"] > row["order_total"] + 0.005:
        problems.append("completed_refunds_exceed_order_total")
    verdict = "consistent" if not problems else (
        "possible_duplicate" if problems == ["multiple_payment_records"] else "inconsistent")
    return {"order_id": row["order_id"], "verdict": verdict, "problems": problems,
            "expected_payment_status": expected}


def _one_order(rows: list[dict[str, Any]], order_id: str) -> list[dict[str, Any]]:
    if not order_id:
        return rows
    rows = [r for r in rows if r["order_id"] == order_id]
    if not rows:
        raise ToolError(f"order {order_id} was not found on this customer's account.")
    return rows


class GetPaymentSummary(_IssueRead):
    """Per order: total, payment status, card ending and refund totals (optionally one order)."""

    domains = PAYMENTS
    name = "get_payment_summary"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_payment_summary", customer_id)
        rows = _one_order(rows, str(args.get("order_id") or issue["refs"].get("order_id") or "").strip())
        return {"as_of": rows[0]["as_of"] if rows else None, "orders": compact(rows), "notice": PAYMENT_NOTICE}


class CheckPaymentConsistency(_IssueRead):
    """Checks each order's payment record against its total, status and refunds: consistent or not."""

    domains = PAYMENTS
    name = "check_payment_consistency"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_payment_summary", customer_id)
        rows = _one_order(rows, str(args.get("order_id") or issue["refs"].get("order_id") or "").strip())
        checks = [payment_consistency(r) for r in rows]
        found = sorted({c["verdict"] for c in checks}) or ["no_orders"]
        return {"as_of": rows[0]["as_of"] if rows else None, "checks": checks, "verdicts": found,
                "notice": PAYMENT_NOTICE,
                "guidance": "consistent records do not disprove the customer's claim. Propose open_billing_review "
                            "for any charge the customer disputes, and for any order that is not consistent."}


class GetLoyaltyProfile(_IssueRead):
    """Points, tier and membership date, with a check that the two tier fields agree."""

    domains = LOYALTY
    name = "get_loyalty_profile"

    async def run(self, ctx, customer_id, issue, args):
        rows = await read(ctx, "get_loyalty_profile", customer_id)
        if not rows:
            raise ToolError("no loyalty record was found for this customer.")
        r = rows[0]
        # loyalty.tier is authoritative for loyalty benefits; a disagreement is a data problem for a person.
        consistent = r["loyalty_tier"] == r["customer_tier"]
        return {"as_of": r["as_of"], "points": r["points"], "tier": r["loyalty_tier"],
                "member_since": r["member_since"], "last_adjustment_at": r["last_adjustment_at"],
                "tier_records_agree": consistent,
                "notice": "There is no history of points earned or adjusted, so missing points cannot be traced "
                          "here, and points or tier cannot be changed by this service."
                          + ("" if consistent else " The account's tier records disagree: hand this to a person "
                                                    "(data_inconsistency) and quote only the loyalty tier.")}


class RecordFindings(CodedTool):
    """
    Stores the specialist's conclusion for an issue and, optionally, the one action it proposes. With answered=true
    and no proposal it also closes the issue as resolved_info_only in the same step, so the orchestrator does not
    spend a call on it; the same guard as update_issue applies (_needs_decision), plus the loyalty cases that need a
    person. A refused close leaves the findings recorded and says why (closed false).
    """

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        conclusion = str(args.get("conclusion") or "").strip()[:1000]
        if not conclusion:
            return error("conclusion is required: what the evidence shows, in two or three sentences.")
        try:
            ctx = await tool_context(sly_data)
            customer_id = ctx.require_customer()
            issue = ctx.require_issue(args.get("issue_id"), ALL_SPECIALISTS)
            proposal = await self._proposal(ctx, customer_id, issue, args.get("proposed_action"))
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="record_findings", author="agent",
                                  data={"conclusion": conclusion, "proposed_action": proposal})
            if proposal:
                issue = ctx.store.set_status(issue["case_id"], issue["issue_id"], PROPOSED, by="llm",
                                             proposal=proposal)
            elif issue["status"] == OPEN:
                issue = ctx.store.set_status(issue["case_id"], issue["issue_id"], INVESTIGATING, by="system")
            result = {"issue_id": issue["issue_id"], "status": issue["status"], "proposal": proposal}
            if args.get("answered") is True:
                result.update(self._close(ctx, issue, proposal))
            return result
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("record_findings failed")
            return error(f"case_unavailable ({type(e).__name__}).")

    @staticmethod
    def _close(ctx: ToolContext, issue: dict[str, Any], proposal: dict | None) -> dict[str, Any]:
        """Closes an answered issue, or says why it stays open for the orchestrator. Never raises."""
        full = next(i for i in ctx.store.case_view(issue["case_id"])["issues"] if i["issue_id"] == issue["issue_id"])
        reason = "an action was proposed; the orchestrator submits it" if proposal else \
            _needs_decision(full, ctx.turn_id, "") or _needs_person(full)
        if not reason:
            try:
                issue = ctx.store.set_status(issue["case_id"], issue["issue_id"], RESOLVED_INFO_ONLY, by="llm")
            except CaseError as e:
                reason = str(e)
        if reason:
            return {"closed": False, "not_closed_because": reason}
        return {"status": issue["status"], "closed": True}

    @staticmethod
    async def _proposal(ctx: ToolContext, customer_id: str, issue: dict[str, Any], raw: Any) -> dict | None:
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ToolError("proposed_action must be an object with action and target_id.")
        action = str(raw.get("action") or "").strip()
        target_id = str(raw.get("target_id") or "").strip()[:64]
        allowed = ctx.policy["action_domains"].get(issue["domain"], [])
        if action not in allowed:
            raise ToolError(f"{action or 'that action'} is not an action for {issue['domain']} issues; "
                            f"allowed: {', '.join(allowed) or 'none'}.")
        rows = await read(ctx, "get_customer_context", customer_id)
        key = "item_id" if action in ITEM_ACTIONS else "order_id"
        if target_id not in {r[key] for r in rows}:
            raise ToolError(f"{key} {target_id} is not on this customer's account.")
        return {"action": action, "target_id": target_id}
