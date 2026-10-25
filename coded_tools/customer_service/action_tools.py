"""
LLM-facing write tools. Every request goes through action_service.request_action, which re-reads the data and
asks the deterministic policy engine; there is no other code path that changes anything.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import action_service
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.identity import AUTH_LEVEL
from coded_tools.customer_service.identity import CHANNEL
from coded_tools.customer_service.identity import IdentityError
from coded_tools.customer_service.identity import authenticated_customer_id
from coded_tools.customer_service.policy import load_policy

LOGGER = logging.getLogger(__name__)


class RequestAction(CodedTool):
    """Asks for a write action (refund, return, trace, cancel) on the authenticated customer's item or order."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            customer_id = authenticated_customer_id(sly_data)
        except IdentityError as e:
            return str(e)
        action = str(args.get("action") or "").strip()
        target_id = str(args.get("target_id") or "").strip()[:64]
        if not action or not target_id:
            return "Error: action and target_id are required."
        try:
            return await action_service.request_action(
                get_adapter(), ActionStore(), load_policy(),
                customer_id=customer_id, channel=sly_data.get(CHANNEL), auth_level=sly_data.get(AUTH_LEVEL),
                action=action, target_id=target_id,
                customer_confirmed=bool(args.get("customer_confirmed", False)), note=str(args.get("note") or ""),
            )
        except Exception as e:  # noqa: BLE001 - never report success on failure
            LOGGER.exception("request_action failed")
            return (f"Error: action_unavailable ({type(e).__name__}). Nothing was changed. Offer to connect the "
                    "customer with a person.")


class EscalateToHuman(CodedTool):
    """Opens a case for a person with a summary, so the customer does not have to repeat anything."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            customer_id = authenticated_customer_id(sly_data)
        except IdentityError as e:
            return str(e)
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return "Error: summary is required: what the customer wants and what you found."
        return await action_service.escalate(ActionStore(), customer_id=customer_id,
                                             reason=str(args.get("reason") or "other"), summary=summary)
