"""
Personal AI assistants on the demo map (docs/assistant_lane_plan.md, no LLM): the assistant gateway records each
ask_support turn (running, then done or failed, with the agents' steps) and each status read; the demo console's
background lane shows a customer only their own assistants' activity, never anyone else's.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from assistant_gateway import activity
from assistant_gateway import support
from assistant_gateway.server import Gateway
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from test_assistant_gateway import assistant_store  # noqa: F401 - autouse fixture: temp assistant store
from test_assistant_gateway import ask_and_wait
from test_assistant_gateway import grant_for
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401

STEPS = [  # what the agent server's stream gives the gateway (demo_console.stream.Normaliser events)
    {"kind": "delegate", "from": "support_orchestrator", "to": "intake_identity", "detail": {"customer_message": "x"},
     "say": "Asks Anchor to identify the customer."},
    {"kind": "agent_reply", "from": "intake_identity", "to": "support_orchestrator", "detail": {"issues": []}},
    {"kind": "delegate", "from": "support_orchestrator", "to": "returns_refunds", "detail": {"issue_id": "I1"}},
    {"kind": "tool_call", "from": "support_orchestrator", "to": "submit_action", "detail": {"args": {"issue_id": "I1"}}},
    {"kind": "tool_result", "from": "submit_action", "to": "support_orchestrator",
     "detail": {"result": {"outcome": "denied", "reason": "confirm_in_channel"}}},
]


def run(coro):
    return asyncio.run(coro)


def agents_with_steps(fail: bool = False):
    async def stream_turn(base_url, text, sly_data, chat_context, normaliser, timeout_s):
        case, _ = CaseStore().open_case(customer_id=sly_data["customer_id"], channel=sly_data["channel"],
                                        auth_level=sly_data["auth_level"], identity_status="gateway_authenticated",
                                        policy_version=None, reference_date=None)
        normaliser.sly_data = {"case_id": case["case_id"]}
        for step in STEPS:
            yield step
        if fail:
            raise TimeoutError("model too slow")
        normaliser.answer = "Your refund is pending.\nTo return it, use the ACME Retail app."
        yield {"kind": "answer"}
    return stream_turn


def records():
    return ActionStore().read(activity.FILE)


def test_a_turn_is_recorded_running_then_done_with_the_agents_steps(monkeypatch):
    monkeypatch.setattr(support, "stream_turn", agents_with_steps())
    token, grant = grant_for("CUST-S01")
    run(ask_and_wait(Gateway(), grant, "Please refund my blue jacket"))
    started, done = records()
    assert (started["status"], done["status"]) == ("running", "done")
    assert started["activity_id"] == done["activity_id"] and done["client_name"] == "Claude"
    assert done["question"] == "Please refund my blue jacket" and done["reply"] == "Your refund is pending."
    assert done["agents"] == ["intake_identity", "returns_refunds"] and done["refused_change"] is True
    assert [e["kind"] for e in done["events"]] == [s["kind"] for s in STEPS]
    assert all("detail" not in e for e in done["events"])  # the map needs who and what, not the data
    text = json.dumps(records())
    assert token not in text and grant["grant_id"] not in text and "sly_data" not in text


def test_a_failed_turn_is_recorded_as_failed(monkeypatch):
    monkeypatch.setattr(support, "stream_turn", agents_with_steps(fail=True))
    _, grant = grant_for("CUST-S01")
    run(ask_and_wait(Gateway(), grant, "Where's my refund?"))
    assert [r["status"] for r in records()] == ["running", "failed"]


def test_status_reads_are_recorded_without_agents(mock):
    _, grant = grant_for("CUST-S01")
    run(Gateway().get_case_status(grant))
    run(Gateway().get_order_status(grant, "blue jacket"))
    assert [(r["tool"], r["status"], "events" in r) for r in records()] == [
        ("get_case_status", "ok", False), ("get_order_status", "ok", False)]


def test_the_console_shows_a_customer_only_their_own_assistants(monkeypatch):
    from demo_console import app as app_module

    monkeypatch.setattr(support, "stream_turn", agents_with_steps())
    _, maya = grant_for("CUST-S01")
    _, other = grant_for("CUST-S02")
    run(ask_and_wait(Gateway(), maya, "Please refund my blue jacket"))
    run(Gateway().get_case_status(other))
    with TestClient(app_module.app) as client:
        s01 = client.post("/api/session", json={"customer_id": "CUST-S01"}).json()["session_id"]
        s02 = client.post("/api/session", json={"customer_id": "CUST-S02"}).json()["session_id"]
        mine = client.get(f"/api/session/{s01}/clock").json()
        assert mine["assistants"] == ["Claude"] and mine["assistant_seq"] == 3
        assert [(a["tool"], a["status"]) for a in mine["assistant"]] == [("ask_support", "running"),
                                                                       ("ask_support", "done")]
        assert "customer_id" not in json.dumps(mine["assistant"])
        theirs = client.get(f"/api/session/{s02}/clock").json()
        assert [a["tool"] for a in theirs["assistant"]] == ["get_case_status"]  # their own read only
        assert client.get(f"/api/session/{s01}/clock?after_assistant=3").json()["assistant"] == []
