"""
Coded tools for the Support Orchestrator: read the case, update issue status, submit a proposed action, and
hand issues to a person.

submit_action only ever submits the action a specialist recorded on the issue (record_findings); the
orchestrator cannot invent an action or a target. It goes through action_layer.submit, the Controlled Action &
Verification Layer (identity, reference guard, issue authorisation, policy, idempotency, execution, read-back
verification, audit).
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import action_layer
from coded_tools.customer_service import guardian
from coded_tools.customer_service import tracing
from coded_tools.customer_service.case_store import HANDED_OFF
from coded_tools.customer_service.case_store import PROPOSED
from coded_tools.customer_service.policy import ITEM_ACTIONS
from coded_tools.customer_service.tool_support import read
from coded_tools.customer_service.case_store import LLM_SETTABLE
from coded_tools.customer_service.case_store import RESOLVED_INFO_ONLY
from coded_tools.customer_service.case_store import TERMINAL
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.identity import IDENTIFIER
from coded_tools.customer_service.tool_support import ToolContext
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

HANDOFF_REASONS = ("customer_requested", "identity_failed", "policy_exception", "insufficient_evidence",
                   "tool_unavailable", "unverified_outcome", "ambiguous", "data_inconsistency", "not_supported",
                   "other")

# Issues that ask for something to be done: closing one needs a recorded decision, even if it is a refusal.
# The action each one asks for: submit_action submits it itself when no specialist proposed anything, so the policy
# decides (and records a refusal) instead of the request ending in a hand-off for want of a proposal.
REQUESTED_ACTION = {"return_request": "create_return", "cancel_request": "cancel_order",
                    "duplicate_charge": "open_billing_review"}
ACTION_KINDS = set(REQUESTED_ACTION)


_subject = guardian.issue_subject


async def _propose_requested_action(ctx: ToolContext, issue: dict[str, Any]) -> dict[str, Any]:
    """Records the action the customer asked for as the issue's proposal (by the system), or raises ToolError."""
    full = next(i for i in ctx.store.case_view(issue["case_id"])["issues"] if i["issue_id"] == issue["issue_id"])
    action = REQUESTED_ACTION[issue["kind"]]
    item, order = _subject(full)
    if item and not order:  # an item reference alone: its order comes from the customer's own data
        rows = await read(ctx, "get_customer_context", ctx.require_customer())
        order = next((r["order_id"] for r in rows if r["item_id"] == item), None)
    target = item if action in ITEM_ACTIONS else order
    if not target:
        raise ToolError(f"issue {issue['issue_id']} has no proposed action and it is not known which "
                        f"{'item' if action in ITEM_ACTIONS else 'order'} the customer means: ask the specialist to "
                        f"identify it, or ask the customer (awaiting_customer).")
    return ctx.store.set_status(issue["case_id"], issue["issue_id"], PROPOSED, by="system",
                                proposal={"action": action, "target_id": target})


def _needs_decision(issue: dict[str, Any], turn_id: str, note: str) -> str:
    """
    Why this issue cannot be closed as information only yet, or '' if it can. A decision recorded by
    submit_action (denied, blocked) allows it. A proposed action must be submitted first, so a specialist's fix is
    never silently dropped. A pending confirmation may only be closed after the customer answered (a later turn)
    and with a note saying they declined. Otherwise an issue that asks for an action, or whose evidence shows a
    stuck or failed case on its item or order, must not be closed as "nothing to do" just because a specialist
    did not propose anything.
    """
    outcome = issue.get("outcome") or {}
    if outcome.get("outcome") == "awaiting_preference_confirmation":  # companion memory (companion_tools)
        if outcome.get("turn_id") == turn_id:
            return "the customer was just asked whether to remember this and has not answered yet"
        return ""
    if outcome.get("outcome") == "awaiting_confirmation":
        if outcome.get("turn_id") == turn_id:
            return "the customer was just asked to confirm the proposed action and has not answered yet"
        if not note:
            return ("the customer was asked to confirm the proposed action; close it only if they declined, with a "
                    "note saying so")
        return ""
    if outcome.get("outcome"):
        return ""
    if issue.get("proposal"):
        proposal = issue["proposal"]
        return (f"{proposal['action']} on {proposal['target_id']} was proposed but not submitted; call "
                f"submit_action (a refusal is recorded too)")
    if issue.get("kind") in ACTION_KINDS:
        return f"the customer asked for something to be done ({issue['kind']})"
    subject = {issue["refs"].get("order_id"), issue["refs"].get("item_id")} - {None, ""}
    for finding in issue["findings"]:
        data = finding["data"]
        if finding["source"] == "find_order_items" and isinstance(data, dict) and data.get("result") == "unique":
            subject |= {data["items"][0]["order_id"], data["items"][0]["item_id"]}
    for finding in issue["findings"]:
        if finding["source"] != "get_stuck_cases" or not isinstance(finding["data"], dict):
            continue
        for row in finding["data"].get("stuck_cases") or []:
            # Without a known item or order, any stuck case in the issue's area counts: better a hand-off too many.
            if not subject or {row.get("order_id"), row.get("item_id")} & subject:
                return f"the records show a {row.get('diagnosis')} on {row.get('item_id') or row.get('order_id')}"
    return ""


def _brief_view(view: dict[str, Any], include_evidence: bool) -> dict[str, Any]:
    """What the orchestrator needs: issue status, proposals, outcomes and the specialists' conclusions."""
    issues = []
    for issue in view["issues"]:
        brief = {k: issue[k] for k in ("issue_id", "domain", "kind", "summary", "refs", "status", "proposal",
                                       "outcome") if issue.get(k)}
        brief["conclusions"] = [f["data"].get("conclusion") for f in issue["findings"] if f["author"] == "agent"]
        evidence = [f for f in issue["findings"] if f["author"] == "tool"]
        brief["evidence_sources"] = sorted({f["source"] for f in evidence})
        if include_evidence:
            brief["evidence"] = [{"source": f["source"], "data": f["data"]} for f in evidence]
        issues.append(brief)
    return {"case_id": view["case_id"], "case_status": view["status"], "identity_status": view["identity_status"],
            "issues": issues, "handoffs": view["handoffs"], "summary": view["summary"]}


class CaseState(CodedTool):
    """op=get returns the case; op=complete closes it, and is refused while any issue is still open."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            case = ctx.require_case()
            if str(args.get("op") or "get") == "complete":
                return ctx.store.complete_case(case["case_id"])
            out = _brief_view(ctx.store.case_view(case["case_id"]), bool(args.get("include_evidence")))
            watching = guardian.customer_watching(ctx.customer_id, cases=ctx.store)
            if watching:  # the Resolution Guardian's follow-ups for this customer (this case and earlier ones)
                out["being_watched"] = watching
            return out
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("case_state failed")
            return error(f"case_unavailable ({type(e).__name__}).")


class UpdateIssue(CodedTool):
    """
    Moves an issue to investigating, proposed or resolved_info_only. The last needs tool evidence and, for an
    action request or a stuck case on the issue's item, a decision recorded by submit_action (_needs_decision).
    """

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        status = str(args.get("status") or "").strip()
        if status not in LLM_SETTABLE:
            return error(f"status must be one of {', '.join(sorted(LLM_SETTABLE))}.")
        try:
            ctx = await tool_context(sly_data)
            issue = ctx.require_issue(args.get("issue_id"))
            note = str(args.get("note") or "").strip()[:500]
            outcome = {"note": note} if note else None
            if status == RESOLVED_INFO_ONLY:
                full = next(i for i in ctx.store.case_view(issue["case_id"])["issues"]
                            if i["issue_id"] == issue["issue_id"])
                reason = _needs_decision(full, ctx.turn_id, note)
                if reason:
                    raise ToolError(
                        f"issue {issue['issue_id']} cannot be closed as information only: {reason}. "
                        + ("Call submit_action: for this request it submits the action the customer asked for "
                           "itself, and a refusal is recorded too. "
                           if full.get("kind") in REQUESTED_ACTION and not full.get("proposal") else
                           "Ask the specialist to propose the action (it may be refused; that is recorded) and "
                           "call submit_action. ")
                        + "Or call handoff_to_human. If a stuck case is about a different item than the customer "
                          "asked about, have the specialist call find_order_items first.")
                if (full.get("outcome") or {}).get("outcome") == "awaiting_confirmation":
                    outcome = {"outcome": "declined_by_customer", "action": full["outcome"].get("action"),
                               "note": note}
            updated = ctx.store.set_status(issue["case_id"], issue["issue_id"], status, by="llm", outcome=outcome)
            return {"issue_id": updated["issue_id"], "status": updated["status"]}
        except (ToolError, CaseError) as e:
            return error(str(e))


class SubmitAction(CodedTool):
    """Submits the action a specialist proposed on an issue to the Controlled Action & Verification Layer."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = ctx.require_issue(args.get("issue_id"))
            if not issue.get("proposal") and issue.get("kind") in REQUESTED_ACTION and issue["status"] not in TERMINAL:
                issue = await _propose_requested_action(ctx, issue)
            return await action_layer.submit(ctx, issue, customer_confirmed=bool(args.get("customer_confirmed")),
                                             note=str(args.get("note") or "")[:500])
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001 - never report success on failure
            LOGGER.exception("submit_action failed")
            return error(f"action_unavailable ({type(e).__name__}). Nothing was changed; hand the issue to a person.")


class HandoffToHuman(CodedTool):
    """Hands one or more issues to a person with a packet built from the case record."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        reason = str(args.get("reason") or "other").strip()
        if reason not in HANDOFF_REASONS:
            reason = "other"
        summary = str(args.get("summary") or "").strip()[:2000]
        if not summary:
            return error("summary is required: what the customer wants, what was found and what was tried.")
        try:
            ctx = await tool_context(sly_data)
            case = ctx.require_case()
            # A person can always be asked for, also after the case was completed: the same case is reopened so
            # the colleague sees everything that happened, and completed again below.
            reopened = case["status"] != "open"
            if reopened:
                case = ctx.store.reopen_case(case["case_id"], reason)
            view = ctx.store.case_view(case["case_id"])
            wanted = [str(i) for i in (args.get("issue_ids") or [])]
            issues = [i for i in view["issues"] if (not wanted or i["issue_id"] in wanted) and
                      i["status"] not in TERMINAL]
            if wanted and not issues and not reopened:
                raise ToolError("none of those issues is open on this case.")
            if not issues:
                domain = "account_identity" if reason == "identity_failed" else "other"
                issues = [ctx.store.add_issue(case["case_id"], domain=domain, kind=reason, summary=summary[:500],
                                              refs={})]
                issues[0]["findings"] = []
            packet = self._packet(ctx, case, issues, reason, summary, str(args.get("next_step") or "")[:500])
            ids = [i["issue_id"] for i in issues]
            with tracing.span("handoff", case_id=case["case_id"], reason=reason,
                              identity_status=ctx.identity["identity_status"]) as current:
                handoff_id = ctx.store.add_handoff(case["case_id"], ids, reason, packet)
                tracing.record(current, handoff_id=handoff_id)
            for issue_id in ids:
                ctx.store.set_status(case["case_id"], issue_id, HANDED_OFF, by="system",
                                     outcome={"handoff_id": handoff_id, "reason": reason})
            if reopened:
                ctx.store.complete_case(case["case_id"])
            hint =("Tell the customer which request a person will follow up on and give the case id. Do not "
                    "promise a time or an outcome.")
            if ctx.identity["identity_status"] not in IDENTIFIED:
                hint += " The customer is not identified: share no account details at all."
            return {"case_id": case["case_id"], "handoff_id": handoff_id, "issues_handed_off": ids,
                    "message_hint": hint}
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("handoff_to_human failed")
            return error(f"handoff_unavailable ({type(e).__name__}).")

    @staticmethod
    def _packet(ctx: ToolContext, case: dict, issues: list[dict], reason: str, summary: str, next_step: str) -> dict:
        """Everything a colleague needs so the customer never repeats themselves. Internal only."""
        return {
            "reason": reason,
            "customer_id": case["customer_id"],
            "channel": case["channel"],
            "identity_status": ctx.identity["identity_status"],
            "auth_level": case["auth_level"],
            # For identity failures the security team needs the raw channel identifier the gateway sent.
            "channel_identifier": None if case["customer_id"] else ctx.sly_data.get(IDENTIFIER),
            "policy_version": case["policy_version"],
            "reference_date": case["reference_date"],
            "issues": [{k: i.get(k) for k in ("issue_id", "domain", "kind", "summary", "refs", "status",
                                               "proposal", "outcome", "findings")} for i in issues],
            "agent_summary": summary,
            "agent_next_step": next_step,
        }
