"""
Turns the Neuro SAN streaming_chat messages (chat_filter MAXIMAL) into the events the demo page shows:

    delegate     an agent asks another agent           {"from", "to", "detail"}
    tool_call    an agent calls a coded tool           {"from" (agent), "to" (tool), "id", "detail"}
    tool_result  the tool's result for that call       {"from" (tool), "to" (agent), "call_id", "detail", "error"}
    agent_reply  an agent answers the agent that asked {"from", "to", "detail"}
    cost         token accounting of the whole request {"llm_calls", "tokens", "seconds", "cost"}
    answer       the reply shown to the customer       {"text"}

Every event also has "t" (seconds since the message was sent), "say" (plain English) and "tech" (raw form).
The message shapes are documented by example in runtime/eval/*/multi_streams/*.jsonl.
"""

import ast
import json
import time
from typing import Any
from typing import AsyncIterator

import httpx

from coded_tools.customer_service import gateway_auth
from demo_console import agents


def parse_text(text: str) -> Any:
    """Tool results arrive as JSON or as a Python literal; anything else stays text."""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
    return text


class Normaliser:
    """Stateful per turn: matches tool results to their calls and keeps the answer, context and sly_data."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.next_id = 1
        self.pending: dict[tuple[str, str], list[int]] = {}
        self.answer = ""
        self.chat_context: dict[str, Any] | None = None
        self.sly_data: dict[str, Any] = {}
        self.cost: dict[str, Any] = {}

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"id": self.next_id, "t": round(time.monotonic() - self.started, 2), "kind": kind, **fields}
        self.next_id += 1
        return event

    def feed(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        mtype = msg.get("type")
        origin = [o.get("tool") for o in msg.get("origin") or []]
        structure = msg.get("structure") if isinstance(msg.get("structure"), dict) else {}
        out: list[dict[str, Any]] = []
        if mtype == "AGENT_FRAMEWORK":
            self.answer = msg.get("text") or self.answer
            self.chat_context = msg.get("chat_context") or self.chat_context
            self.sly_data = msg.get("sly_data") or self.sly_data
            out.append(self._event("answer", text=self.answer, say=f"{agents.label(agents.FRONT)} writes the answer to the customer.",
                                   tech="AGENT_FRAMEWORK"))
        elif mtype == "AGENT" and structure.get("invoking_start") and origin:
            caller, target, params = origin[-1], structure.get("invoked_agent_name"), structure.get("params") or {}
            if target in agents.AGENTS:
                out.append(self._event("delegate", **{"from": caller, "to": target}, detail=params,
                                       say=agents.delegate_summary(target, params), tech=f"Invoking {target}"))
            else:
                event = self._event("tool_call", **{"from": caller, "to": target}, detail={"args": params},
                                    say=agents.call_summary(target, params),
                                    tech=f"{target}({json.dumps(params, default=str)[:300]})")
                self.pending.setdefault((caller, target), []).append(event["id"])
                out.append(event)
        elif mtype == "AGENT_TOOL_RESULT" and origin:
            source = [o.get("tool") for o in msg.get("tool_result_origin") or []]
            returned_from, receiver = (source[-1] if source else None), origin[-1]
            result = parse_text(msg.get("text") or "")
            if returned_from in agents.AGENTS:
                out.append(self._event("agent_reply", **{"from": returned_from, "to": receiver}, detail=result,
                                       say=agents.reply_summary(returned_from, result),
                                       tech=f"result from {returned_from}"))
            elif returned_from:
                calls = self.pending.get((receiver, returned_from)) or []
                error = isinstance(result, str) and result.startswith("Error")
                out.append(self._event("tool_result", **{"from": returned_from, "to": receiver},
                                       call_id=calls.pop(0) if calls else None, detail={"result": result},
                                       error=error, say=agents.result_summary(returned_from, result),
                                       tech=f"result from {returned_from}"))
        elif len(origin) == 1 and structure.get("total_tokens") is not None:
            # Two accounting messages arrive (main network, then request total); keep the larger.
            if structure.get("total_tokens", 0) >= self.cost.get("tokens", -1):
                self.cost = {"llm_calls": structure.get("successful_requests"), "tokens": structure.get("total_tokens"),
                             "seconds": structure.get("time_taken_in_seconds"), "cost": structure.get("total_cost")}
                out.append(self._event("cost", **self.cost, say="", tech="token accounting"))
        return out


async def stream_turn(base_url: str, text: str, sly_data: dict[str, Any], chat_context: dict[str, Any] | None,
                      normaliser: Normaliser, timeout_s: float = 960) -> AsyncIterator[dict[str, Any]]:
    """Posts one customer message to Neuro SAN and yields events as the agents work."""
    # The gateway signs the identity it sends (gateway_auth.py); system requests carry none.
    body: dict[str, Any] = {"user_message": {"type": "HUMAN", "text": text},
                            "sly_data": gateway_auth.sign(sly_data, customer_message=text),
                            "chat_filter": {"chat_filter_type": "MAXIMAL"}}
    if chat_context:
        body["chat_context"] = chat_context
    url = f"{base_url}/api/v1/{agents.NETWORK_PATH}/streaming_chat"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10)) as client:
        async with client.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                for event in normaliser.feed(json.loads(line).get("response") or {}):
                    yield event
