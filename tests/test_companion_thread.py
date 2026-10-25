"""
Companion plan P2 (no LLM): the continuous thread per customer, the gateway recording each turn, updates merged in,
the greeting, and the middleware that gives the front agent its companion notes in code.
"""

import asyncio
from pathlib import Path

from langchain_core.messages import SystemMessage
from pyhocon import ConfigFactory

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import companion_middleware as cm
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from coded_tools.customer_service.guardian import OUTBOX
from coded_tools.customer_service.policy import load_policy
from test_companion import refund_question
from test_multi_agent_foundation import S18_PHONE
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

POLICY = load_policy()
SETTINGS = POLICY["companion"]
NETWORKS = Path(__file__).resolve().parents[1] / "registries" / "customer_service"
LATER = "2100-01-01T00:00:00Z"  # a customer message after everything else in the test


def run(coro):
    return asyncio.run(coro)


def setup_function():
    cp._DATA_CACHE.clear()
    cm._NOTES.clear()


def view(mock, customer_id):
    return run(cp.load_view(customer_id, adapter=mock, policy=POLICY))


def update(customer_id, message, source="guardian"):
    ActionStore().append(OUTBOX, {"customer_id": customer_id, "support_case_id": "CASE-X", "source": source,
                                  "message": message, "status": "pending_send"})


# ---------------------------------------------------------------------------- the gateway records the thread


def test_finish_turn_records_the_message_and_the_reply_shown(mock):
    sly = web(mock, "CUST-S02")
    since = turn_start()
    start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where", "item_id": "ITM-S02-1-1"})
    shown, checked = finish_turn(sly["case_id"], since, '{"not": "readable"}', customer_message="Where is my parcel?")
    assert checked["thread_recorded"]
    thread = cp.CompanionStore().thread("CUST-S02", since=None)
    assert [(m["role"], m["text"]) for m in thread] == [("customer", "Where is my parcel?"), ("companion", shown)]
    assert thread[0]["at"] == since and thread[0]["channel"] == "web_session"  # the fallback was shown, not the JSON
    assert thread[1]["case_id"] == sly["case_id"]


def test_finish_turn_without_a_message_or_customer_records_nothing(mock):
    sly = web(mock, "CUST-S02")
    since = turn_start()
    start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where"})
    finish_turn(sly["case_id"], since, "Here it is.")  # a gateway that does not pass the message
    assert cp.CompanionStore().thread("CUST-S02", since=None) == []
    phone = {"channel": "phone", "identifier": S18_PHONE}  # ambiguous: no customer, so no thread
    start(phone, {"domain": "other", "kind": "question", "summary": "hello"})
    _, checked = finish_turn(phone["case_id"], turn_start(), "Please sign in.", customer_message="hello")
    assert checked["thread_recorded"] is False


# ---------------------------------------------------------------------------- the thread


def test_updates_are_merged_into_the_thread_by_time():
    store = cp.CompanionStore()
    store.record_turn("CUST-S01", case_id="CASE-1", channel="web_session", message="Where is my refund?",
                      reply="It is pending.", asked_at="2000-01-01T00:00:00Z")
    update("CUST-S01", "Your refund has been completed. Case CASE-1.")
    update("CUST-S02", "Someone else's update.")
    thread = store.thread("CUST-S01", since=None)
    assert [m["role"] for m in thread] == ["customer", "companion", "update"]
    assert thread[2]["source"] == "guardian" and "Someone else" not in str(thread)
    assert store.thread("CUST-S01", since=None, limit=1)[0]["role"] == "update"


def test_an_update_is_new_until_the_customer_writes_again():
    store = cp.CompanionStore()
    store.record_turn("CUST-S01", case_id=None, channel="web_session", message="hi", reply="hello",
                      asked_at="2000-01-01T00:00:00Z")
    update("CUST-S01", "Your refund has been completed.")
    assert [u["text"] for u in store.new_updates("CUST-S01", since=None)] == ["Your refund has been completed."]
    store.record_turn("CUST-S01", case_id=None, channel="web_session", message="thanks", reply="you're welcome",
                      asked_at=LATER)
    assert store.new_updates("CUST-S01", since=None) == []


def test_reset_hides_the_earlier_thread_and_export_holds_it():
    store = cp.CompanionStore()
    store.record_turn("CUST-S01", case_id=None, channel="web_session", message="hi", reply="hello",
                      asked_at="2000-01-01T00:00:00Z")
    assert len(store.export("CUST-S01", SETTINGS)["thread"]) == 2
    reset_at = store.reset("CUST-S01", SETTINGS)["reset_at"]
    assert store.thread("CUST-S01", since=reset_at) == []


# ---------------------------------------------------------------------------- greeting and digest


def test_first_conversation_gets_an_introduction(mock):
    v = view(mock, "CUST-S01")
    assert v["greeting"] == "Hi Maya, I'm Everly, your AI companion at ACME Retail. How can I help today?"
    assert "This is their first conversation with you." in v["digest"]


def test_returning_customer_is_welcomed_back_with_what_changed(mock):
    refund_question(mock)  # a finished conversation (the gateway recorded it; the Guardian may follow it)
    store = cp.CompanionStore()
    store.record_turn("CUST-S01", case_id=None, channel="web_session", message="hi", reply="hello",
                      asked_at="2000-01-01T00:00:00Z")
    update("CUST-S01", "Your refund of $129.00 for the blue classic puffer jacket has been completed. Case CASE-1.")
    v = view(mock, "CUST-S01")
    assert v["greeting"].startswith("Welcome back, Maya. Since we last spoke: Your refund of $129.00")
    assert v["greeting"].endswith("How can I help today?")
    assert 'Sent to them since their last message: "Your refund of $129.00' in v["digest"]
    assert "first conversation" not in v["digest"]


def test_greeting_promises_updates_only_with_consent(mock):
    refund_question(mock, "CUST-S08", "ITM-S08-1-1")  # the Guardian follows a stuck refund
    assert "will send you an update" in view(mock, "CUST-S08")["greeting"]
    cp.CompanionStore().set_consent("CUST-S08", "service_updates", False, channel="web")
    greeting = view(mock, "CUST-S08")["greeting"]
    assert "still following one of your requests." in greeting and "will send you an update" not in greeting


# ---------------------------------------------------------------------------- the middleware


def test_notes_are_built_in_code_for_an_identified_customer_once_per_turn(mock, monkeypatch):
    sly = web(mock, "CUST-S01")
    notes = run(cm.notes_for(sly))
    assert notes.startswith("Everly's notes on this customer") and "Maya" in notes
    assert sly["customer_id"] == "CUST-S01" and "CUST-S01" not in notes  # identity resolved in code, id not shown
    calls = []
    monkeypatch.setattr(cp, "load_view", lambda *a, **k: calls.append(1))
    assert run(cm.notes_for(sly)) == notes and calls == []  # the same turn reuses them


def test_no_notes_for_an_unidentified_contact(mock):
    assert run(cm.notes_for({"channel": "phone", "identifier": S18_PHONE})) == ""


def test_the_current_conversation_is_not_an_open_item_in_the_notes(mock):
    sly = web(mock, "CUST-S02")
    start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where"})
    sly.pop("turn_id", None)  # a new turn of the same conversation
    assert "Open:" not in run(cm.notes_for(sly))


class _Request:
    """The part of a langchain ModelRequest the middleware uses."""

    def __init__(self, system: str | None) -> None:
        self.system_message = SystemMessage(content=system) if system is not None else None

    def override(self, system_message):
        return _Request(system_message.text)


def test_notes_are_appended_to_the_instructions_and_a_failure_never_breaks_the_turn(mock, monkeypatch):
    sly = web(mock, "CUST-S01")
    seen = []

    async def handler(request):
        seen.append(request.system_message.text)
        return "reply"

    middleware = cm.CompanionNotesMiddleware(sly_data=sly)
    assert run(middleware.awrap_model_call(_Request("You are the companion."), handler)) == "reply"
    assert seen[0].startswith("You are the companion.\n\nEverly's notes on this customer")

    async def broken(_sly):
        raise RuntimeError("data down")

    monkeypatch.setattr(cm, "notes_for", broken)
    assert run(middleware.awrap_model_call(_Request("You are the companion."), handler)) == "reply"
    assert seen[1] == "You are the companion."


def test_both_networks_give_the_front_agent_the_notes_middleware():
    for name in ("support_multi_agent.hocon", "support_multi_agent_lean.hocon"):
        conf = ConfigFactory.parse_file(str(NETWORKS / name))
        front = next(t for t in conf["tools"] if t["name"] == "support_orchestrator")
        assert [m["class"] for m in front["middleware"]] == [  # Orbit's clock first (a tick needs no notes)
            "coded_tools.customer_service.system_events.ScriptedEventMiddleware",
            "coded_tools.customer_service.companion_middleware.CompanionNotesMiddleware"]
        assert "notes on this customer" in front["instructions"]
