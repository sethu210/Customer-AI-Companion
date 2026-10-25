"""
Demo console (offline, mock data): the stream normaliser on a recorded scripted run, the website-only customer
catalog and simulated sign-in (sly_data built server-side, never taken from the browser), and take scoring.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coded_tools.customer_service import data_adapter
from demo_console import recording
from demo_console.catalog import Catalog
from demo_console.stream import Normaliser
from demo_console.stream import parse_text

FIXTURE = Path(__file__).parent / "fixtures" / "demo" / "m3_scripted_stream.jsonl"


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("CUSTOMER_DATA_BACKEND", "mock")
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "cases.db"))
    monkeypatch.setenv("ACTION_STORE_DIR", str(tmp_path / "actions"))
    monkeypatch.delenv("DEMO_RECORDING", raising=False)
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))
    monkeypatch.setattr(data_adapter, "_ADAPTER", None)
    monkeypatch.setattr(recording, "LIBRARY", tmp_path / "recordings")
    yield
    data_adapter._ADAPTER = None


def turn_events(turn: int) -> tuple[Normaliser, list[dict]]:
    norm, events = Normaliser(), []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["turn"] == turn:
            events += norm.feed(row["response"])
    return norm, events


def test_normaliser_turns_the_stream_into_agent_and_tool_events():
    norm, events = turn_events(1)
    kinds = [e["kind"] for e in events]
    delegates = [(e["from"], e["to"]) for e in events if e["kind"] == "delegate"]
    assert delegates == [("support_orchestrator", "intake_identity"), ("support_orchestrator", "order_delivery"),
                         ("support_orchestrator", "policy_risk_escalation")]
    replies = [(e["from"], e["to"]) for e in events if e["kind"] == "agent_reply"]
    assert ("intake_identity", "support_orchestrator") in replies and ("order_delivery", "support_orchestrator") in replies
    calls = {e["id"]: e for e in events if e["kind"] == "tool_call"}
    results = [e for e in events if e["kind"] == "tool_result"]
    assert {c["to"] for c in calls.values()} >= {"open_case", "add_issues", "get_orders", "get_stuck_cases",
                                                  "evaluate_policy", "submit_action", "case_state"}
    for r in results:  # every result is matched to the call of the same tool by the same agent
        call = calls[r["call_id"]]
        assert (call["from"], call["to"]) == (r["to"], r["from"])
    stuck = next(r for r in results if r["from"] == "get_stuck_cases")
    assert stuck["say"].startswith("Stuck: stuck shipment on ORD-S10-1")
    assert kinds[-1] == "answer" and norm.answer and norm.sly_data.get("case_id")
    assert norm.cost["llm_calls"] == 15 and all(e["t"] >= 0 for e in events)


def test_parse_text_reads_json_and_python_literals():
    assert parse_text('{"a": 1}') == {"a": 1}
    assert parse_text("{'a': None, 'b': True}") == {"a": None, "b": True}
    assert parse_text("Error: case_unavailable") == "Error: case_unavailable"


def test_catalog_is_website_only_with_scenarios_first():
    cat = Catalog.load()
    first = cat.search(kind="scenario", limit=3)["customers"]
    assert first[0]["customer_id"] == "CUST-S01" and first[0]["scenarios"][0]["id"] == "S01"
    # a customer with two scenarios is listed at its first one (Hannah: S03 and M1)
    assert [c["customer_id"] for c in first] == ["CUST-S01", "CUST-S02", "CUST-S03"]
    assert all(s["id"] != "S18" for c in cat.customers.values() for s in c.scenarios)  # phone-only scenario
    daniel = cat.get("CUST-S08")
    assert [s["id"] for s in daniel.scenarios] == ["S08", "M2"]
    assert cat.gateway_sly_data("CUST-S08") == {"channel": "web_session", "identifier": daniel.identifier}
    # 500 background customers + the scenario partners without a website scenario (S17B, S18A, S18B, S19B), minus
    # CUST-B0196, who has a website scenario of its own (N1, Personal Stylist pick)
    assert cat.search(kind="other", limit=1)["total"] == 503
    assert cat.search(query="okafor")["customers"][0]["customer_id"] == "CUST-S08"


def _shopping_case(customer_id: str, query: str) -> dict:
    """Runs a shopping request through the real tools (no LLM) and returns the case as the console reads it."""
    import asyncio

    from coded_tools.customer_service import recommendations
    from coded_tools.customer_service.case_store import CaseStore
    from coded_tools.customer_service.intake_tools import AddIssues
    from coded_tools.customer_service.intake_tools import OpenCase
    from coded_tools.customer_service.recommendation_tools import GetRecommendations

    recommendations._CATALOG_CACHE.clear()
    sly = Catalog.load().gateway_sly_data(customer_id)
    asyncio.run(OpenCase().async_invoke({}, sly))
    added = asyncio.run(AddIssues().async_invoke({"issues": [{"domain": "shopping", "kind": "product_question",
                                                              "summary": "suggestions", "item_query": query}]}, sly))
    iid = added["issues"][0]["issue_id"]
    asyncio.run(GetRecommendations().async_invoke({"issue_id": iid, "product_query": query}, sly))
    return CaseStore().case_view(sly["case_id"])


def test_product_cards_come_from_the_engines_evidence_and_show_once():
    from demo_console.session import suggestion_events

    view, shown = _shopping_case("CUST-S11", "chinos"), set()
    (event,) = suggestion_events(view, shown)
    assert event["kind"] == "suggestions" and event["status"] == "ok" and 1 <= len(event["items"]) <= 3
    first = event["items"][0]
    assert first["title"] == "Slim Chino Pants" and first["reason"] == "replacement_other_size"
    assert first["because"].startswith("The same Slim Chino Pants") and first["list_price"] == 57.99
    assert first["description"] and all(set(o) == {"color", "size"} for o in first["options"])
    text = json.dumps(event)
    assert "SKU-" not in text and "stock" not in text  # the page never sees SKUs or stock numbers
    assert suggestion_events(view, shown) == []  # each engine result is shown once


def test_a_refusal_shows_no_cards_but_says_why_in_the_case_panel():
    from demo_console.session import suggestion_events

    (event,) = suggestion_events(_shopping_case("CUST-S15", "jacket"), set())
    assert (event["items"], event["reason"]) == ([], "not_available")
    assert event["say"] == "Recommendation engine: none (not available)"


def test_recommendation_scenarios_are_offered_on_the_website_except_the_phone_one():
    cat = Catalog.load()
    assert "R1" in [s["id"] for s in cat.get("CUST-S11").scenarios]
    assert all(s["id"] != "R4" for c in cat.customers.values() for s in c.scenarios)


def test_sign_in_builds_sly_data_on_the_server():
    from demo_console import app as app_module

    with TestClient(app_module.app) as client:
        res = client.post("/api/session", json={"customer_id": "CUST-S08", "sly_data": {"customer_id": "CUST-S13"}})
        assert res.status_code == 200
        body = res.json()
        assert body["sly_data_sent"]["channel"] == "web_session"
        assert "customer_id" not in body["sly_data_sent"]  # the browser's sly_data is ignored
        session = app_module.STATE["sessions"][body["session_id"]]
        assert session.sly_data() == body["sly_data_sent"]
        assert client.post("/api/session", json={"customer_id": None}).status_code == 403  # not signed in: off
        assert client.post("/api/session", json={"customer_id": "CUST-NOPE"}).status_code == 404
        assert client.post(f"/api/session/{body['session_id']}/record", json={"on": True}).status_code == 403
        meta = client.get("/api/meta").json()
        assert meta["brand"] == "Everly" and meta["tagline"] == "Your AI Companion, Always by Your Side"
        assert meta["retailer"] == "ACME Retail"
        assert meta["recording"] is False and [a["name"] for a in meta["agents"]][0] == "support_orchestrator"


def test_companion_view_is_the_signed_in_customers_own():
    from demo_console import app as app_module

    with TestClient(app_module.app) as client:
        sid = client.post("/api/session", json={"customer_id": "CUST-S01"}).json()["session_id"]
        body = client.get(f"/api/session/{sid}/companion").json()
        assert body["companion"]["name"] == "Everly" and body["profile"]["customer"]["first_name"] == "Maya"
        assert body["digest"].startswith("Everly's notes") and "CUST-S01" not in body["digest"]
        client.post(f"/api/session/{sid}/guardian/updates", json={"on": False})  # the Inbox switch is a consent
        consent = client.get(f"/api/session/{sid}/companion").json()["companion"]["consents"]["service_updates"]
        assert consent["granted"] is False and consent["set_by"] == "customer"
        assert client.get("/api/session/nope/companion").status_code == 404
        thread = client.get(f"/api/session/{sid}/thread").json()
        assert thread["thread"] == [] and thread["greeting"].startswith("Hi Maya, I'm Everly")


def test_inbox_shows_the_guardians_watch_and_simulated_events_reach_only_the_owner(monkeypatch, tmp_path):
    import asyncio

    from coded_tools.customer_service.case_store import turn_start
    from coded_tools.customer_service.gateway import finish_turn
    from coded_tools.customer_service.intake_tools import AddIssues
    from coded_tools.customer_service.intake_tools import OpenCase
    from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
    from coded_tools.customer_service.specialist_tools import RecordFindings
    from demo_console import app as app_module

    from coded_tools.customer_service.system_events import TOKEN_ENV
    from test_orbit_tick import TOKEN
    from test_orbit_tick import orbit_in_process  # what Orbit runs (test_orbit_tick runs the real network)

    monkeypatch.setenv("EVENT_SIMULATOR", "1")
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    sly = Catalog.load().gateway_sly_data("CUST-S01")
    since = turn_start()
    asyncio.run(OpenCase().async_invoke({}, sly))
    added = asyncio.run(AddIssues().async_invoke({"issues": [{"domain": "returns_refunds", "kind": "refund_status",
                                                              "summary": "refund?", "item_id": "ITM-S01-1-1"}]}, sly))
    iid = added["issues"][0]["issue_id"]
    asyncio.run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    asyncio.run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "pending", "answered": True}, sly))
    finish_turn(sly["case_id"], since, "It is pending.")

    with TestClient(app_module.app) as client:
        app_module.STATE["clock"].send = orbit_in_process
        maya = client.post("/api/session", json={"customer_id": "CUST-S01"}).json()["session_id"]
        other = client.post("/api/session", json={"customer_id": "CUST-S02"}).json()["session_id"]
        box = client.get(f"/api/session/{maya}/inbox").json()
        assert box["simulator"] and box["messages"] == [] and box["watches"][0]["state"] == "watching"
        box = client.post(f"/api/session/{maya}/guardian/advance", json={"hours": 14}).json()  # 08:00 next day
        assert box["tick_error"] is None
        (watch,) = box["watches"]
        assert watch["state"] == "waiting_on_business" and "refund_completed" in [e["event"] for e in watch["events"]]
        event = {"watch_id": watch["watch_id"], "event": "refund_completed"}
        assert client.post(f"/api/session/{other}/guardian/event", json=event).status_code == 400  # not theirs
        assert client.get(f"/api/session/{other}/inbox").json()["watches"] == []
        box = client.post(f"/api/session/{maya}/guardian/event", json=event).json()
        (msg,) = box["messages"]
        assert msg["source"] == "guardian" and "$129.00" in msg["message"] and box["watches"][0]["state"] == "closed"
        assert client.post(f"/api/session/{maya}/guardian/event", json=event).status_code == 400  # nothing left
        assert client.post(f"/api/session/{maya}/guardian/updates", json={"on": False}).json()["updates_on"] is False

        # the map's background lane: Orbit's ticks, and whether this customer got a message (never who else did)
        lane = client.get(f"/api/session/{maya}/clock").json()
        assert lane["every_s"] == 60 and [r["event"] for r in lane["requests"]] == ["tick", "tick"]
        assert [r["for_you"] for r in lane["requests"]] == [[], ["guardian"]]
        assert [r["for_you"] for r in client.get(f"/api/session/{other}/clock").json()["requests"]] == [[], []]
        assert client.get(f"/api/session/{maya}/clock?after={lane['requests'][-1]['seq']}").json()["requests"] == []
        assert "delivered_to" not in json.dumps(lane)


def test_take_is_saved_and_scored_like_an_evaluation_run():
    cat = Catalog.load()
    customer = cat.get("CUST-S10")
    take = recording.Take(customer.public(), cat.gateway_sly_data("CUST-S10"))
    for turn, question in ((1, "My shoes haven't moved for days."), (2, "Actually, I'd like to talk to a person please.")):
        norm, events = turn_events(turn)
        take.add_turn(question, events, norm.answer, 12.0, norm.cost)
    assert take.data["scenario"] == "M3"
    meta = take.finish({"case_id": "CASE-X", "identity_status": "verified", "issues": [], "handoffs": [
        {"handoff_id": "H1", "issue_ids": [], "reason": "customer_requested", "status": "open"}],
        "summary": {"outcome": "closed_with_handoff"}})
    assert meta["verdict"]["result"] == "PASS"  # escalate class: a person was involved
    assert recording.list_takes("M3")[0]["take_id"] == take.take_id
    assert recording.resolve_take("M3") == take.take_id
    recording.pick("M3", take.take_id)
    assert recording.load_picks() == {"M3": take.take_id}


def test_a_take_with_other_messages_is_unscored():
    cat = Catalog.load()
    customer = cat.get("CUST-S08")
    take = recording.Take(customer.public(), cat.gateway_sly_data("CUST-S08"))
    norm, events = turn_events(1)
    take.add_turn("Hello, what can you do?", events, norm.answer, 5.0, norm.cost)
    meta = take.finish(None)
    assert take.data["scenario"] is None and meta["verdict"]["result"] == "UNSCORED"
    assert (recording.LIBRARY / "manual-CUST-S08" / take.take_id / "recording.json").exists()
    with pytest.raises(LookupError):
        recording.resolve_take("manual-CUST-S08")  # never used automatically
