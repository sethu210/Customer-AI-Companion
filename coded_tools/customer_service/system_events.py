"""
Requests from ACME Retail's own systems, not from a customer (docs/orbit_tick_plan.md): Orbit's clock tick and
new-in launches.

They reach Orbit (support_orchestrator) through the agent server like a customer message, with the sly_data
    tick     {"channel": "system_tick", "tick_token": <ORBIT_TICK_TOKEN>, "check_customer": <optional customer id>}
    launch   {"channel": "system_event", "tick_token": ..., "event": "launch", "product_id": "PRD-...",
              "launched_by": <optional, for the launch record>}
(check_customer: also check that customer's watches now, not only the due ones; the demo's simulated events use it).
Code decides every step: ScriptedEventMiddleware answers in place of the model on Orbit and on the agents it calls,
so Orbit -> guardian (run_follow_ups) and Orbit -> stylist (release_picks; on a launch, launch_new_in) are real
agent-to-agent calls in the agent server's stream, without a model call. What to launch comes from sly_data, never
from arguments. The secret token is set by scripts\\demo.ps1 for both processes.

On customer turns the same middleware hides the system-only tools from the model and changes nothing else. A
request on a system channel without the right token is refused: no tool and no model call.
"""

import ast
import hmac
import json
import logging
import os
import uuid
from typing import Any
from typing import Awaitable
from typing import Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

LOGGER = logging.getLogger(__name__)

TOKEN_ENV = "ORBIT_TICK_TOKEN"
TICK = "tick"
LAUNCH = "launch"
REFUSED = "refused"
TICK_CHANNEL = "system_tick"
EVENT_CHANNEL = "system_event"  # its event is named in sly_data["event"]
EVENTS = {LAUNCH}

# What each agent does on a tick, in order: (tool or agent, arguments). The arguments are fixed: nothing a model
# wrote, and what to check comes from sly_data in the tools themselves.
PLANS: dict[str, dict[str, list[tuple[str, dict[str, Any]]]]] = {
    TICK: {
        "support_orchestrator": [("guardian", {"request": "Run the follow-up checks that are due."}),
                                 ("stylist", {"issue_id": "SYSTEM", "request": "Send held picks that may go now."})],
        "guardian": [("run_follow_ups", {"reason": "clock tick"})],
        "stylist": [("release_picks", {"reason": "clock tick"})],
    },
    LAUNCH: {
        "support_orchestrator": [("stylist", {"issue_id": "SYSTEM", "request": "Launch the new-in product."})],
        "stylist": [("launch_new_in", {"reason": "new-in launch"})],
    },
}
# Never offered to a model on a customer turn.
SYSTEM_ONLY_TOOLS = {"guardian", "run_follow_ups", "release_picks", "launch_new_in"}


def system_event(sly_data: dict[str, Any] | None) -> str | None:
    """
    'tick' or 'launch' for a system request with the right token; 'refused' for any other request on a system
    channel (wrong or missing token, unknown event); None for a customer's request.
    """
    sly = sly_data or {}
    channel = str(sly.get("channel") or "")
    if channel not in (TICK_CHANNEL, EVENT_CHANNEL):
        return None
    expected = os.environ.get(TOKEN_ENV, "")
    given = str(sly.get("tick_token") or "")
    if not (expected and hmac.compare_digest(given.encode(), expected.encode())):
        LOGGER.warning("system request refused: %s", "no ORBIT_TICK_TOKEN on the agent server" if not expected
                       else "wrong or missing tick_token")
        return REFUSED
    if channel == TICK_CHANNEL:
        return TICK
    event = str(sly.get("event") or "")
    if event not in EVENTS:
        LOGGER.warning("system request refused: unknown event %r", event[:40])
        return REFUSED
    return event


def parse(text: Any) -> Any:
    """Tool and agent results arrive as JSON or as a Python literal; anything else stays text."""
    if not isinstance(text, str):
        return text
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
    return text


def _this_turn(messages: list[Any]) -> tuple[list[str], dict[str, Any]]:
    """The tools called since the last human message, in order, and their results by tool name."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    called, names, results = [], {}, {}
    for m in messages[last_human + 1:]:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                called.append(c["name"])
                names[c["id"]] = c["name"]
        elif isinstance(m, ToolMessage):
            results[names.get(m.tool_call_id) or m.name or "unknown"] = parse(m.content)
    return called, results


def scripted_step(agent: str, event: str, messages: list[Any]) -> AIMessage:
    """The next step of the agent's plan for this event, or its final answer: the results, as JSON."""
    plan = PLANS.get(event, {}).get(agent, [])
    called, results = _this_turn(messages)
    if len(called) < len(plan):
        name, args = plan[len(called)]
        return AIMessage(content="", tool_calls=[{"name": name, "args": dict(args), "type": "tool_call",
                                                  "id": f"call_{uuid.uuid4().hex[:12]}"}])
    if agent != "support_orchestrator" and len(results) == 1:
        return AIMessage(content=json.dumps(next(iter(results.values())), default=str))
    return AIMessage(content=json.dumps({"event": event, **results}, default=str))


class ScriptedEventMiddleware(AgentMiddleware):
    """
    On Orbit and the agents it calls on a tick ("middleware" in the network's hocon, created with the request's
    sly_data). system_only: the agent has no customer work (the guardian): any other request is refused in code.
    """

    def __init__(self, sly_data: dict[str, Any] | None = None, agent: str = "", system_only: bool = False) -> None:
        super().__init__()
        self.sly_data = sly_data if sly_data is not None else {}
        self.agent = agent
        self.system_only = system_only

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        event = system_event(self.sly_data)
        if event == REFUSED:
            return AIMessage(content="Refused: not a customer message, and not a valid system request.")
        if event is not None:
            return scripted_step(self.agent, event, request.messages)
        if self.system_only:
            return AIMessage(content=json.dumps({"error": "This runs only on Orbit's system requests."}))
        tools = [t for t in request.tools if _name(t) not in SYSTEM_ONLY_TOOLS]
        return await handler(request.override(tools=tools) if len(tools) != len(request.tools) else request)


def _name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
