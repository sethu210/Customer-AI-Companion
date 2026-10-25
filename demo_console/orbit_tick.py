"""
Orbit's clock and launch events (docs/orbit_tick_plan.md T3, L1): every orbit_tick_every_s (demo.yaml, 60 s) the
console sends Orbit a tick through the agent server, the way a gateway sends a customer message. Orbit then calls
the Guardian (the follow-up checks that are due) and Aura (held picks that may go now). The Launch panel's button
sends Orbit a launch event instead, and Orbit has Aura launch the product. Code decides every step and no model is
called (coded_tools/customer_service/system_events.py). The Inbox tab's clock and event buttons tick at once.

One request at a time. Each carries the secret ORBIT_TICK_TOKEN, which scripts\\demo.ps1 gives to the agent server
and to this console; without it the agent server refuses them and the result reports why.

The last HISTORY requests are kept (numbered by seq) for the demo map's background lane, each with the customers that
got a message during it (delivered_to, from the outbox reader the console gives).
"""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any
from typing import Awaitable
from typing import Callable

from coded_tools.customer_service.system_events import EVENT_CHANNEL
from coded_tools.customer_service.system_events import LAUNCH
from coded_tools.customer_service.system_events import TICK
from coded_tools.customer_service.system_events import TICK_CHANNEL
from coded_tools.customer_service.system_events import TOKEN_ENV
from coded_tools.customer_service.system_events import parse
from demo_console.stream import Normaliser
from demo_console.stream import stream_turn

LOGGER = logging.getLogger(__name__)

# (sly_data) -> (events as the map sees them, Orbit's answer)
Sender = Callable[[dict[str, Any]], Awaitable[tuple[list[dict[str, Any]], str]]]
HISTORY = 20
MAP_EVENTS = ("delegate", "agent_reply", "tool_call", "tool_result")


def system_sly_data(channel: str, **fields: Any) -> dict[str, Any]:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is not set: start the demo with scripts\\demo.ps1")
    return {"channel": channel, "tick_token": token, **{k: v for k, v in fields.items() if v}}


class OrbitClock:
    def __init__(self, base_url: str, *, timeout_s: float = 120, send: Sender | None = None, every_s: float = 60,
                 outbox: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.every_s = every_s
        self.send: Sender = send or self._post
        self.outbox = outbox  # the outbox rows, to tell who got a message during a request
        self.last: dict[str, Any] | None = None  # the last tick: when, events, summary, error
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._seq = 0
        self._lock = asyncio.Lock()

    def since(self, seq: int) -> list[dict[str, Any]]:
        """The kept requests after seq, oldest first."""
        return [r for r in self.history if r["seq"] > seq]

    async def _post(self, sly: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        normaliser = Normaliser()
        text = "Clock tick." if sly["channel"] == TICK_CHANNEL else f"System event: {sly.get('event')}."
        events = [e async for e in stream_turn(self.base_url, text, sly, None, normaliser, self.timeout_s)]
        return events, normaliser.answer

    async def _request(self, event: str, sly_fields: dict[str, Any]) -> dict[str, Any]:
        """One system request through Orbit. Never raises: a failure is reported in 'error'."""
        async with self._lock:
            started = time.time()
            self._seq += 1
            out: dict[str, Any] = {"seq": self._seq, "event": event, "at": started, "events": [], "summary": None,
                                   "error": None, "delivered_to": []}
            before = self._outbox_size()
            try:
                out["events"], answer = await self.send(system_sly_data(**sly_fields))
                out["summary"] = parse(answer)
                if not isinstance(out["summary"], dict) or out["summary"].get("event") != event:
                    out["error"] = f"unexpected answer from Orbit: {str(answer)[:200]}"
            except Exception as e:  # noqa: BLE001 - the agent server may be starting or stopped
                out["error"] = f"{type(e).__name__}: {e}"
            out["seconds"] = round(time.time() - started, 2)
            if before is not None:
                out["delivered_to"] = [{"customer_id": r.get("customer_id"), "source": r.get("source")}
                                       for r in self.outbox()[before:]]
            self.history.append({**out, "events": [e for e in out["events"] if e["kind"] in MAP_EVENTS]})
            return out

    def _outbox_size(self) -> int | None:
        try:
            return len(self.outbox()) if self.outbox else None
        except Exception:  # noqa: BLE001 - only the map's "a message went out" depends on it
            LOGGER.exception("outbox unreadable")
            return None

    async def tick(self, check_customer: str | None = None) -> dict[str, Any]:
        """One tick: the Guardian's checks, then held picks. A failed tick is retried by the next one."""
        out = await self._request(TICK, {"channel": TICK_CHANNEL, "check_customer": check_customer})
        if out["error"]:
            LOGGER.warning("Orbit clock tick failed: %s", out["error"])
        elif _did_something(out["summary"]):
            LOGGER.info("Orbit clock tick: %s", out["summary"])
        self.last = out
        return out

    async def launch(self, product_id: str, *, launched_by: str = "launch panel") -> dict[str, Any]:
        """A new-in launch through Orbit and Aura. 'error' also holds Aura's refusal (for example: not staged)."""
        out = await self._request(LAUNCH, {"channel": EVENT_CHANNEL, "event": LAUNCH, "product_id": product_id,
                                           "launched_by": launched_by})
        result = (out["summary"] or {}).get("stylist") if isinstance(out["summary"], dict) else None
        if out["error"] is None and not isinstance(result, dict):
            out["error"] = str(result or "no answer from Aura")
        LOGGER.info("launch %s through Orbit: %s", product_id, out["error"] or result)
        return out

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.every_s)
            await self.tick()


def _did_something(summary: dict[str, Any]) -> bool:
    g, s = summary.get("guardian"), summary.get("stylist")
    if not isinstance(g, dict) or not isinstance(s, dict):
        return True  # an error text from a tool: worth logging
    return any(g.get(k) for k in ("checked", "notices", "handoffs")) or any(s.get(k) for k in ("sent", "expired",
                                                                                                "suppressed"))
