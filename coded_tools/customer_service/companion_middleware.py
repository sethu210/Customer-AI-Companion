"""
Gives the front agent its companion notes (companion plan P2), in code.

A LangChain agent middleware on support_orchestrator (the "middleware" entry in the network's hocon, created by
Neuro SAN with the request's sly_data). Before each model call it appends the customer's companion digest
(companion.digest) to the agent's instructions:
    * only for an identified customer: identity is resolved in code from sly_data, exactly as the tools do it;
    * built once per request (turn) and reused for the agent's later model calls in that turn;
    * the conversation that is going on now is left out of the open items (its case comes from case_state);
    * never in the customer's message and never in sly_data, so the customer cannot write or forge it;
    * a failure (data down, store error) is logged and the agent runs without notes: the turn never breaks.
"""

import logging
from collections import OrderedDict
from typing import Any
from typing import Awaitable
from typing import Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from coded_tools.customer_service import companion
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.tool_support import CASE_ID
from coded_tools.customer_service.tool_support import TURN_ID
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)
_NOTES: "OrderedDict[str, str]" = OrderedDict()  # turn_id -> notes ('' = none); the last few turns only
_KEEP = 64


async def notes_for(sly_data: dict[str, Any]) -> str:
    """The digest for this request's customer, or '' (not identified, or not available)."""
    ctx = await tool_context(sly_data)  # resolves the identity in code and sets the turn id
    turn = sly_data[TURN_ID]
    if turn in _NOTES:
        return _NOTES[turn]
    notes = ""
    if ctx.identity["identity_status"] in IDENTIFIED and ctx.customer_id:
        view = await companion.load_view(ctx.customer_id, adapter=ctx.adapter, policy=ctx.policy, cases=ctx.store,
                                         exclude_case_id=sly_data.get(CASE_ID))
        notes = view["digest"]
    _NOTES[turn] = notes
    LOGGER.info("companion notes for turn %s: %d characters", turn[:8], len(notes))  # no customer data in logs
    while len(_NOTES) > _KEEP:
        _NOTES.popitem(last=False)
    return notes


def with_notes(request: ModelRequest, notes: str) -> ModelRequest:
    if not notes:
        return request
    base = request.system_message.text if request.system_message is not None else ""
    return request.override(system_message=SystemMessage(content=f"{base}\n\n{notes}" if base else notes))


class CompanionNotesMiddleware(AgentMiddleware):
    def __init__(self, sly_data: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.sly_data = sly_data if sly_data is not None else {}

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        try:
            notes = await notes_for(self.sly_data)
        except Exception:  # noqa: BLE001 - the agent answers without notes rather than not at all
            LOGGER.exception("companion notes unavailable for this turn")
            notes = ""
        return await handler(with_notes(request, notes))
