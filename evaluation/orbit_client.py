"""
How the evaluations make the Guardian and Aura act (docs/orbit_tick_plan.md T5/L6): as in the demo, a tick or a
new-in launch is a request to Orbit through the evaluation's agent server, and Orbit calls the guardian and stylist
agents (code decides, no model call). A request that fails raises: the scenario or journey then fails visibly.

ensure_token() must run before the agent server starts (AgentServer passes the environment on).
"""

import os
import uuid
from typing import Any

from coded_tools.customer_service.system_events import TOKEN_ENV
from demo_console.orbit_tick import OrbitClock
from evaluation.agent_server import SERVER_PORT


def ensure_token() -> None:
    os.environ.setdefault(TOKEN_ENV, uuid.uuid4().hex)


def _clock() -> OrbitClock:
    return OrbitClock(f"http://localhost:{SERVER_PORT}")


def _via(out: dict[str, Any]) -> list[str]:
    """The agent-to-agent calls of the request, e.g. ['support_orchestrator->guardian', ...]."""
    return [f"{e['from']}->{e['to']}" for e in out["events"] if e["kind"] == "delegate"]


async def tick(check_customer: str | None = None) -> dict[str, Any]:
    """One tick through Orbit: {"guardian": run_follow_ups' summary, "stylist": release_picks' summary, "via"}."""
    out = await _clock().tick(check_customer=check_customer)
    if out["error"]:
        raise RuntimeError(f"Orbit's tick failed: {out['error']}")
    return {**{k: v for k, v in out["summary"].items() if k != "event"}, "via": _via(out)}


async def launch(product_id: str, launched_by: str) -> dict[str, Any]:
    """A launch through Orbit and Aura: launch_new_in's counts (candidates, scored, invited, gated, ...) and "via"."""
    out = await _clock().launch(product_id, launched_by=launched_by)
    if out["error"]:
        raise RuntimeError(f"launch of {product_id} through Orbit failed: {out['error']}")
    return {**out["summary"]["stylist"], "via": _via(out)}
