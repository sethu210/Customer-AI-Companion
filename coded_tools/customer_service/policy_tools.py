"""
Coded tools for the Policy, Risk & Escalation agent. Both are read-only: the agent recommends, the action layer
decides. Risk flags and their names never leave the policy engine; the agent only learns that an account
review is needed.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import action_layer
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

# Settings the agent may use to explain a decision. Internal limits and risk settings are deliberately absent.
SHAREABLE = ("reference_date", "return_window_days", "stuck_refund_days", "stuck_shipment_days",
             "requires_customer_confirmation", "action_domains", "version")


class EvaluatePolicy(CodedTool):
    """Dry run of the action layer for an issue's proposed action: recommendation, checks and explanation."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = ctx.require_issue(args.get("issue_id"))
            result = await action_layer.evaluate(ctx, issue,
                                                 customer_confirmed=bool(args.get("customer_confirmed")))
            ctx.store.add_finding(issue["case_id"], issue["issue_id"], source="evaluate_policy", author="tool",
                                  data={k: result.get(k) for k in ("recommendation", "kind", "missing", "reason",
                                                                   "policy_version")})
            return result
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("evaluate_policy failed")
            return error(f"policy_unavailable ({type(e).__name__}). Recommend a hand-off to a person.")


class GetPolicyConfig(CodedTool):
    """The policy settings that may be used to explain decisions (no limits or risk settings)."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
        except Exception as e:  # noqa: BLE001
            return error(f"policy_unavailable ({type(e).__name__}).")
        config = {k: ctx.policy[k] for k in SHAREABLE if k in ctx.policy}
        wanted = str(args.get("setting") or "").strip()
        if wanted:
            return {wanted: config[wanted]} if wanted in config else error(f"{wanted} is not a shareable setting.")
        return config
