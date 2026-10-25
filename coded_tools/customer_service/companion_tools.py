"""
Coded tools for the front agent: the customer's companion memory (companion plan P3).

Only for a "companion / preference" issue, i.e. when the customer asked to remember or forget something. The
customer's yes is checked in code, as for actions (G1):

    remember_preference  1. without customer_confirmed: checks the preference (fixed kinds, short plain text, a real
                            size), records it as a question waiting for the customer and returns the exact question
                            to ask. The issue waits for the customer (awaiting_customer).
                         2. in a LATER turn with customer_confirmed=true: the waiting question becomes a remembered
                            preference and the issue closes. A yes in the same turn as the question, or for a
                            question never asked, is refused.
    forget_preference    deletes one remembered preference (its id from the notes, e.g. P2) or all of them, at once.

Who may change memory: an identified customer on a sign-in and channel that may change things (write_auth_levels,
not read_only_channels), like any other change. Each call files tool evidence on the issue, without the value of a
forgotten preference.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import companion
from coded_tools.customer_service import confirmation
from coded_tools.customer_service.case_store import AWAITING_CUSTOMER
from coded_tools.customer_service.case_store import RESOLVED_INFO_ONLY
from coded_tools.customer_service.case_store import TERMINAL
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.companion import CompanionError
from coded_tools.customer_service.tool_support import ToolContext
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

COMPANION = ("companion",)
WAITING = "awaiting_preference_confirmation"  # the issue outcome while the customer's yes is outstanding

NOT_ALLOWED_HINT = ("Nothing was remembered or deleted: {why}. Tell the customer they can manage what you remember "
                    "in the ACME Retail app or website. Then close the issue (update_issue resolved_info_only).")


def may_not_change(ctx: ToolContext) -> str:
    """'' if this contact may change the customer's memory, else why not (in words for the customer)."""
    if not ctx.customer_id:
        return "the customer is not identified"
    if ctx.channel in ctx.policy.get("read_only_channels", []):
        return "changes cannot be made through a personal AI assistant"
    if ctx.auth_level not in ctx.policy.get("write_auth_levels", []):
        return "this needs a stronger sign-in (the app or the website)"
    return ""


def _open_issue(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    issue = ctx.require_issue(args.get("issue_id"), COMPANION)
    if issue["status"] in TERMINAL:
        raise ToolError(f"issue {issue['issue_id']} is already {issue['status']}.")
    return issue


def _refused(ctx: ToolContext, issue: dict[str, Any], why: str) -> dict[str, Any]:
    ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="companion_memory", author="tool",
                          data={"outcome": "not_allowed", "why": why})
    return {"issue_id": issue["issue_id"], "status": "not_allowed", "message_hint": NOT_ALLOWED_HINT.format(why=why)}


class RememberPreference(CodedTool):
    """Asks the customer to confirm a preference, then (after their yes, in a later message) remembers it."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = _open_issue(ctx, args)
            why = may_not_change(ctx)
            if why:
                return _refused(ctx, issue, why)
            settings = ctx.policy["companion"]
            store = companion.CompanionStore(ctx.store.path)
            if bool(args.get("customer_confirmed")):
                return self._confirm(ctx, store, issue, args, settings)
            kind = str(args.get("kind") or "").strip()
            value = companion.check_preference(kind, str(args.get("value") or ""), settings)
            if len(store.memories(ctx.customer_id)) >= settings["memory"]["max_active"]:
                raise ToolError("the customer already has the most preferences that can be kept; offer to forget one "
                                "first (forget_preference)")
            pending = store.propose_memory(ctx.customer_id, kind, value, case_id=issue["case_id"],
                                           issue_id=issue["issue_id"], turn_id=ctx.turn_id, channel=ctx.channel,
                                           pending_hours=settings["memory"]["pending_hours"])
            question = f"Shall I remember that {companion.preference_phrase(kind, value)}?"
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="companion_memory", author="tool",
                                  data={"outcome": "asked", "memory_id": pending["memory_id"], "kind": kind,
                                        "value": value})
            ctx.store.set_status(issue["case_id"], issue["issue_id"], AWAITING_CUSTOMER, by="system",
                                 outcome={"outcome": WAITING, "memory_id": pending["memory_id"], "kind": kind,
                                          "value": value, "turn_id": ctx.turn_id})
            return {"issue_id": issue["issue_id"], "status": "awaiting_confirmation", "question": question,
                    "message_hint": "Nothing is remembered yet. Ask the customer exactly this question and wait for "
                                    "their answer. If they say yes in their next message, call remember_preference "
                                    "again with this issue_id and customer_confirmed=true."}
        except (ToolError, CaseError, CompanionError) as e:
            return error(f"{e}. Nothing was remembered.")
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("remember_preference failed")
            return error(f"memory_unavailable ({type(e).__name__}). Nothing was remembered.")

    @staticmethod
    def _confirm(ctx: ToolContext, store: companion.CompanionStore, issue: dict[str, Any], args: dict[str, Any],
                 settings: dict[str, Any]) -> dict[str, Any]:
        """The customer's yes counts only for the question code recorded on this issue, in an earlier turn."""
        waiting = issue.get("outcome") or {}
        if waiting.get("outcome") != WAITING:
            raise ToolError("the customer was not asked yet: call remember_preference without customer_confirmed "
                            "first, ask the question it returns, and wait for their answer")
        if waiting.get("turn_id") == ctx.turn_id:
            raise ToolError("the customer was asked in this same turn and has not answered yet")
        kind = waiting["kind"]
        if args.get("kind") or args.get("value"):  # a different preference than the one the customer was asked about
            asked = (kind, waiting["value"])
            try:
                given = (str(args.get("kind") or kind), companion.check_preference(
                    str(args.get("kind") or kind), str(args.get("value") or waiting["value"]), settings))
            except CompanionError:
                given = None
            if given != asked:
                raise ToolError(f"the customer was asked about {kind} '{waiting['value']}', not this; ask them about "
                                f"the new one first (call without customer_confirmed)")
        if not any(m["memory_id"] == waiting["memory_id"] for m in store.memories(ctx.customer_id, status="pending")):
            raise ToolError("the question has expired; ask the customer again (call without customer_confirmed)")
        refused = confirmation.answer_refusal(ctx.sly_data)  # their own message must be a clear yes (D1)
        if refused:
            raise ToolError(refused)
        keep = settings["memory"]["occasion_keep_days" if kind == "occasion" else "keep_days"]
        memory = store.confirm_memory(ctx.customer_id, waiting["memory_id"], keep_days=keep)
        ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="companion_memory", author="tool",
                              data={"outcome": "remembered", "memory_id": memory["memory_id"], "kind": kind,
                                    "value": memory["value"]})
        ctx.store.set_status(issue["case_id"], issue["issue_id"], RESOLVED_INFO_ONLY, by="system",
                             outcome={"outcome": "remembered", "memory_id": memory["memory_id"]})
        return {"issue_id": issue["issue_id"], "status": "remembered", "closed": True,
                "remembered": companion.preference_phrase(kind, memory["value"]),
                "message_hint": "Tell the customer you will remember it, and that they can see or delete what you "
                                "remember at any time. The issue is closed."}


class ForgetPreference(CodedTool):
    """Deletes a remembered preference (or all of them) at the customer's request."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = _open_issue(ctx, args)
            why = may_not_change(ctx)
            if why:
                return _refused(ctx, issue, why)
            store = companion.CompanionStore(ctx.store.path)
            target = str(args.get("memory_id") or "").strip().upper()
            if not target:
                raise ToolError("memory_id is required: the id from your notes (e.g. P2), or ALL to forget everything")
            remembered = {m["memory_id"]: m for m in store.memories(ctx.customer_id)}
            if target != "ALL" and target not in remembered:
                raise ToolError(f"{target} is not one of the customer's remembered preferences "
                                f"({', '.join(remembered) or 'none'})")
            forgotten = store.forget(ctx.customer_id, None if target == "ALL" else target)
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="companion_memory", author="tool",
                                  data={"outcome": "forgotten", "memory_ids": forgotten})  # never the values
            ctx.store.set_status(issue["case_id"], issue["issue_id"], RESOLVED_INFO_ONLY, by="system",
                                 outcome={"outcome": "forgotten", "memory_ids": forgotten})
            what = "everything you remembered" if target == "ALL" else \
                companion.preference_phrase(remembered[target]["kind"], remembered[target]["value"])
            return {"issue_id": issue["issue_id"], "status": "forgotten", "closed": True, "forgotten": what,
                    "message_hint": "Tell the customer it is deleted and will not be used again. The issue is closed."}
        except (ToolError, CaseError) as e:
            return error(f"{e}. Nothing was deleted.")
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("forget_preference failed")
            return error(f"memory_unavailable ({type(e).__name__}). Nothing was deleted.")
