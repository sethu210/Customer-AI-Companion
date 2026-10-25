"""
Coded tool of the guardian agent (docs/orbit_tick_plan.md T1): the Resolution Guardian's checks, run only on Orbit's
clock tick (system_events). The rules, templates and outbox are in guardian.py and unchanged.

    run_follow_ups   checks the watches that are due (and, with check_customer in the tick's sly_data, all of that
                     customer's watches now), then sends the updates that may go out; returns the summary
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import guardian
from coded_tools.customer_service.system_events import TICK
from coded_tools.customer_service.system_events import system_event
from coded_tools.customer_service.tool_support import error

LOGGER = logging.getLogger(__name__)


class RunFollowUps(CodedTool):
    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        if system_event(sly_data) != TICK:
            return error("follow-up checks run only on Orbit's clock tick")
        try:
            return await guardian.Guardian().run_once(customer_id=sly_data.get("check_customer") or None)
        except Exception as e:  # noqa: BLE001 - the next tick tries again
            LOGGER.exception("run_follow_ups failed")
            return error(f"follow_ups_unavailable ({type(e).__name__}); the next tick tries again")
