"""
Coded tool for the Support Orchestrator: product suggestions, on the customer's request only.

get_recommendations only works on an issue intake recorded as a shopping / product_question issue, i.e. when the
customer asked; it cannot be used to suggest products on any other issue. It is refused while another request on
the case is still open (the customer's problem comes first), and the engine (recommendations.py) refuses when the
customer is not identified, signed in too weakly, has a case with a person, or has risk flags. What was offered is
filed as tool evidence on the issue (with SKUs, for checking later); the model sees no SKUs or stock numbers.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import companion
from coded_tools.customer_service import recommendations
from coded_tools.customer_service.case_store import HANDED_OFF
from coded_tools.customer_service.case_store import INVESTIGATING
from coded_tools.customer_service.case_store import OPEN
from coded_tools.customer_service.case_store import QUEUED_FOR_REVIEW
from coded_tools.customer_service.case_store import TERMINAL
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

SHOPPING = ("shopping",)


def _for_the_model(result: dict[str, Any]) -> dict[str, Any]:
    """The engine's result without SKUs: the model names colours and sizes, never internal ids."""
    shown = {k: v for k, v in result.items() if k != "recommendations"}
    shown["recommendations"] = [
        {**{k: v for k, v in r.items() if k != "available"},
         "available": [{"color": o["color"], "size": o["size"]} for o in r["available"]]}
        for r in result["recommendations"]
    ]
    return shown


class GetRecommendations(CodedTool):
    """Up to three product suggestions for a shopping issue, decided by code, with the reason for each."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = ctx.require_issue(args.get("issue_id"), SHOPPING)
            if issue["status"] in TERMINAL:
                raise ToolError(f"issue {issue['issue_id']} is already {issue['status']}.")
            view = ctx.store.case_view(issue["case_id"])
            others = [i for i in view["issues"] if i["issue_id"] != issue["issue_id"]]
            still_open = [i["issue_id"] for i in others if i["status"] not in TERMINAL]
            if still_open:
                raise ToolError(
                    f"the customer's other requests come first: {', '.join(still_open)} still open. Resolve or "
                    f"hand them off, then call this again; if they wait for the customer's answer, set this issue "
                    f"to awaiting_customer and offer suggestions once that is settled.")
            with_person = bool(view["handoffs"]) or any(i["status"] in (HANDED_OFF, QUEUED_FOR_REVIEW)
                                                        for i in others)
            # The customer's stated, confirmed preferences (companion memory, P3); none for an unknown customer.
            # Colour families (P4) let "earth tones" or "neutrals" stand for their colours.
            preferences = None
            if ctx.customer_id:
                memories = companion.CompanionStore(ctx.store.path).memories(ctx.customer_id)
                style = await companion.load_style_data(ctx.adapter, ctx.policy) if memories else None
                preferences = companion.preference_signals(memories, (style or {}).get("families"))
            result = await recommendations.get_recommendations(
                ctx.adapter, ctx.policy, customer_id=ctx.customer_id,
                identity_status=ctx.identity["identity_status"], auth_level=ctx.auth_level,
                case_with_person=with_person, anchor_item_id=str(args.get("item_id") or "").strip() or None,
                product_query=str(args.get("product_query") or "").strip()[:200] or None, preferences=preferences)
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="get_recommendations", author="tool",
                                  data=result)
            if issue["status"] == OPEN:
                ctx.store.set_status(issue["case_id"], issue["issue_id"], INVESTIGATING, by="system")
            return {"issue_id": issue["issue_id"], **_for_the_model(result)}
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001 - nothing is suggested rather than guessing
            LOGGER.exception("get_recommendations failed")
            return error(f"recommendations_unavailable ({type(e).__name__}). Suggest no products.")
