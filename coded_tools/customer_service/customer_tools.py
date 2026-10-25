"""
LLM-facing coded tools for the zero-question support agent.

None of these tools takes a customer id from the model: the id always comes from sly_data (see identity.py),
so a prompt like "I'm actually customer X" or an id typed into a tool argument has no effect.
Results are compacted (null fields dropped) to keep the model's context small.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.identity import IdentityError
from coded_tools.customer_service.identity import authenticated_customer_id

LOGGER = logging.getLogger(__name__)
OVERVIEW_CACHE = "customer_overview_cache"  # sly_data key; never sent back to the client


def _compact(rows: list[dict[str, Any]], drop: tuple[str, ...] = ("as_of",)) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if v is not None and k not in drop} for row in rows]


def _error(message: str) -> str:
    return message if message.startswith("Error:") else f"Error: {message}"


class CustomerOverview(CodedTool):
    """Profile, every ordered item with its delivery/return/refund status, and stuck cases, in one call."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            customer_id = authenticated_customer_id(sly_data)
        except IdentityError as e:
            return str(e)

        cached = sly_data.get(OVERVIEW_CACHE)
        if isinstance(cached, dict) and cached.get("customer_id") == customer_id and not args.get("refresh"):
            return cached["overview"]

        adapter = get_adapter()
        try:
            profile = await adapter.get_customer_profile(customer_id)
            items = await adapter.get_customer_context(customer_id)
            stuck = await adapter.get_stuck_cases(customer_id)
        except Exception as e:  # noqa: BLE001 - never let a backend failure turn into a guess
            LOGGER.exception("customer_overview failed")
            return _error(f"customer_data_unavailable ({type(e).__name__}). Tell the customer you are checking "
                          "and offer a hand-off to a person; do not guess order or refund status.")
        if not profile:
            return _error("identity_unavailable. No account found for this session; ask the customer to log in again.")

        as_of = profile[0].get("as_of")
        overview = {
            "as_of": as_of,
            "customer": _compact(profile, drop=("as_of", "customer_id"))[0],
            "items": _compact(items),
            "stuck_cases": _compact(stuck),
            "data_source": adapter.name,
        }
        sly_data[OVERVIEW_CACHE] = {"customer_id": customer_id, "overview": overview}
        return overview


class FindOrderItems(CodedTool):
    """Resolves a description such as "blue jacket" to items on the authenticated customer's own orders."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            customer_id = authenticated_customer_id(sly_data)
        except IdentityError as e:
            return str(e)
        item_query = str(args.get("item_query") or "").strip()[:200]
        if not item_query:
            return _error("item_query is required, e.g. 'blue jacket'.")
        try:
            rows = await get_adapter().find_order_items(customer_id, item_query)
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("find_order_items failed")
            return _error(f"customer_data_unavailable ({type(e).__name__}).")
        verdict = "no_match" if not rows else ("unique" if len(rows) == 1 else "ambiguous")
        return {"item_query": item_query, "result": verdict, "items": _compact(rows, drop=("as_of", "match_score"))}


class GetOrderTimeline(CodedTool):
    """Step-by-step history of one of the authenticated customer's orders."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            customer_id = authenticated_customer_id(sly_data)
        except IdentityError as e:
            return str(e)
        order_id = str(args.get("order_id") or "").strip()[:64]
        if not order_id:
            return _error("order_id is required; take it from customer_overview or find_order_items.")
        try:
            rows = await get_adapter().get_case_timeline(customer_id, order_id)
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("get_order_timeline failed")
            return _error(f"customer_data_unavailable ({type(e).__name__}).")
        if not rows:
            # Same answer whether the order does not exist or belongs to someone else: no information leak.
            return _error(f"order {order_id} was not found on this customer's account.")
        return {"order_id": order_id, "as_of": rows[0]["as_of"], "events": _compact(rows)}
