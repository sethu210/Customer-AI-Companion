"""
ask_support: one customer message through the multi-agent network, for an assistant.

Turns take minutes on a slow model, longer than an MCP client waits, so the turn runs in the background: ask returns
a request id at once and get_answer reads the result. The turn goes through the same gateway as the website (sly_data
built in code, finish_turn's checks), and the result carries structured data from the case record next to the reply.
"""

import asyncio
import logging
from typing import Any

from assistant_gateway import activity
from assistant_gateway.grants import GrantStore
from assistant_gateway.views import case_summary
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from demo_console.stream import Normaliser
from demo_console.stream import stream_turn

LOGGER = logging.getLogger(__name__)
CHANNEL = "assistant"


def sly_data_for(grant: dict[str, Any], case_id: str | None) -> dict[str, Any]:
    """The gateway contract: identity from the grant, never from the assistant. Only the case id may come back."""
    sly = {"customer_id": grant["customer_id"], "channel": CHANNEL, "auth_level": grant["auth_level"]}
    if case_id:
        sly["case_id"] = case_id
    return sly


async def run_turn(store: GrantStore, grant: dict[str, Any], request_id: str, message: str, case_id: str | None,
                   agent_url: str, timeout_s: float) -> None:
    """Runs one turn and records the answer on the request. Never raises."""
    normaliser, since = Normaliser(), turn_start()
    context = store.chat_context(grant["grant_id"], case_id) if case_id else None
    events: list[dict[str, Any]] = []  # the agents' steps, for the demo map (activity)
    activity.turn_started(grant, request_id, message)
    try:
        async with asyncio.timeout(timeout_s):
            async for event in stream_turn(agent_url, message, sly_data_for(grant, case_id), context, normaliser,
                                           timeout_s):
                if event["kind"] in activity.MAP_EVENTS:
                    events.append(event)
    except Exception as e:  # noqa: BLE001 - recorded on the request, the assistant is told to ask again
        LOGGER.warning("ask_support %s failed: %s", request_id, e)
        case_id = normaliser.sly_data.get("case_id") or case_id
        if case_id:  # nothing stays silently open: the end-of-turn check hands open issues to a person
            finish_turn(case_id, since, None)
        failed = ("Support could not finish this request. Please ask again; nothing was changed through this "
                  "assistant.")
        store.finish_request(request_id, status="failed", case_id=case_id, result={}, answer=failed)
        activity.turn_finished(grant, request_id, message, "failed", events, failed, case_id)
        return
    case_id = normaliser.sly_data.get("case_id") or case_id
    answer, checked = finish_turn(case_id, since, normaliser.answer, customer_message=message)
    activity.turn_finished(grant, request_id, message, "done", events, answer, case_id)
    result: dict[str, Any] = {"reply_checked": not checked["reply_problem"],
                              "_chat_context": normaliser.chat_context}  # internal: underscore keys never leave
    if case_id:
        try:
            cases = CaseStore()
            row = cases.get_case(case_id)
            if row and row["customer_id"] == grant["customer_id"]:
                result["case"] = case_summary(cases.case_view(case_id), row)
        except CaseError:
            pass
    store.finish_request(request_id, status="done", answer=answer, result=result, case_id=case_id)
