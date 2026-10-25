"""
What the customers' AI assistants did, for the demo console's map (docs/assistant_lane_plan.md A1).

One JSON line per record in the shared action store (ACTION_STORE_DIR/assistant_activity.jsonl), append-only:
    ask_support   a 'running' record when the turn starts, then a 'done' or 'failed' record with the same
                  activity_id (the request id), carrying the agent-to-agent steps of the turn
    status reads  one 'ok' record (get_case_status, get_order_status: no agents)
Only what the case record and the agents' stream already hold: never the token, the grant or sly_data, and of the
reply only its first line. The customer id stays server-side; the console shows a customer only their own records.
Writing a record never breaks a call: a failure is logged and the call goes on.
"""

import logging
import time
from typing import Any

from coded_tools.customer_service.action_store import ActionStore

LOGGER = logging.getLogger(__name__)
FILE = "assistant_activity.jsonl"
MAP_EVENTS = ("delegate", "agent_reply", "tool_call", "tool_result")
REFUSED_ON_CHANNEL = "confirm_in_channel"  # policy's reason for a change asked through an assistant


def _write(record: dict[str, Any]) -> None:
    try:
        ActionStore().append(FILE, record)
    except Exception:  # noqa: BLE001 - only the demo map depends on it
        LOGGER.exception("assistant activity not recorded")


def _base(grant: dict[str, Any], activity_id: str, tool: str, status: str) -> dict[str, Any]:
    return {"activity_id": activity_id, "customer_id": grant["customer_id"],
            "client_name": grant.get("client_name") or "AI assistant", "tool": tool, "status": status,
            "time": time.time()}


def map_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """The part of a stream event the map needs (no arguments or results), or None."""
    if ev.get("kind") not in MAP_EVENTS:
        return None
    return {"kind": ev["kind"], "from": ev.get("from"), "to": ev.get("to"), "say": str(ev.get("say") or "")[:160]}


def refused_change(events: list[dict[str, Any]]) -> bool:
    """A change the customer asked for was refused because it came through an assistant (read-only channel)."""
    return any(e.get("kind") == "tool_result" and e.get("from") == "submit_action"
               and REFUSED_ON_CHANNEL in str((e.get("detail") or {}).get("result")) for e in events)


def turn_started(grant: dict[str, Any], request_id: str, message: str) -> None:
    _write({**_base(grant, request_id, "ask_support", "running"), "question": message[:160]})


def turn_finished(grant: dict[str, Any], request_id: str, message: str, status: str, events: list[dict[str, Any]],
                  answer: str | None, case_id: str | None) -> None:
    steps = [m for m in (map_event(e) for e in events) if m]
    first_line = (answer or "").strip().splitlines()[0][:160] if (answer or "").strip() else ""
    _write({**_base(grant, request_id, "ask_support", status), "question": message[:160], "events": steps,
            "agents": list(dict.fromkeys(e["to"] for e in steps if e["kind"] == "delegate")),
            "reply": first_line, "case_id": case_id, "refused_change": refused_change(events)})


def status_read(grant: dict[str, Any], tool: str, found: bool) -> None:
    _write({**_base(grant, f"{tool}-{time.time_ns()}", tool, "ok"), "found": found})
