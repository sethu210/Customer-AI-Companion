"""
Orbit's clock and launch events (docs/orbit_tick_plan.md T1-T3, L1-L2, no LLM): a tick request makes Orbit call the
guardian agent and the stylist agent through the network, and a launch event makes Orbit call the stylist agent to
launch the product, decided in code (system_events middleware) with no model call; customer turns never see the
system-only tools; a request on a system channel without the secret token is refused.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from pyhocon import ConfigFactory

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import guardian as g
from coded_tools.customer_service import stylist as sty
from coded_tools.customer_service import system_events as se
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.guardian_tools import RunFollowUps
from coded_tools.customer_service.stylist_tools import LaunchNewIn
from coded_tools.customer_service.stylist_tools import ReleasePicks
from test_guardian import s01
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "registries" / "customer_service" / "customer-ai-companion.hocon"
TOKEN = "test-token"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv(se.TOKEN_ENV, TOKEN)


def tick_sly(**extra):
    return {"channel": "system_tick", "tick_token": TOKEN, **extra}


def launch_sly(product_id="PRD-CSC", **extra):
    return {"channel": "system_event", "tick_token": TOKEN, "event": "launch", "product_id": product_id, **extra}


async def orbit_in_process(sly):
    """
    A stand-in for the agent server in the console's tests: what Orbit's plan runs for that request, with the same
    coded tools and sly_data (the tests below run the real network).
    """
    event = se.system_event(sly)
    if event == se.TICK:
        result = {"guardian": await RunFollowUps().async_invoke({}, sly),
                  "stylist": await ReleasePicks().async_invoke({}, sly)}
    elif event == se.LAUNCH:
        result = {"stylist": await LaunchNewIn().async_invoke({}, sly)}
    else:
        return [], "Refused: not a customer message, and not a valid system request."
    return [], json.dumps({"event": event, **result}, default=str)


# ---------------------------------------------------------------------------- the token


def test_only_a_tick_with_the_right_token_is_a_tick(token, monkeypatch):
    assert se.system_event(tick_sly()) == se.TICK
    assert se.system_event({"channel": "system_tick", "tick_token": "guess"}) == se.REFUSED
    assert se.system_event({"channel": "system_tick"}) == se.REFUSED
    assert se.system_event({"channel": "web_session", "identifier": "x", "tick_token": TOKEN}) is None
    assert se.system_event(None) is None
    monkeypatch.delenv(se.TOKEN_ENV)
    assert se.system_event(tick_sly()) == se.REFUSED  # no token on the server: every tick is refused


def test_a_launch_event_needs_the_token_and_a_known_event(token):
    assert se.system_event(launch_sly()) == se.LAUNCH
    assert se.system_event({**launch_sly(), "tick_token": "guess"}) == se.REFUSED
    assert se.system_event({**launch_sly(), "event": "refund_everyone"}) == se.REFUSED
    assert se.system_event({**launch_sly(), "event": None}) == se.REFUSED


def test_the_system_tools_refuse_outside_their_event(token):
    for t in (RunFollowUps(), ReleasePicks(), LaunchNewIn()):
        assert run(t.async_invoke({}, {"channel": "web_session", "identifier": "x"})).startswith("Error:")
        assert run(t.async_invoke({}, {"channel": "system_tick", "tick_token": "guess"})).startswith("Error:")
    assert run(RunFollowUps().async_invoke({}, tick_sly()))["checked"] == []
    assert run(ReleasePicks().async_invoke({}, tick_sly()))["sent"] == []
    assert run(LaunchNewIn().async_invoke({}, tick_sly())).startswith("Error:")  # a tick launches nothing
    assert run(RunFollowUps().async_invoke({}, launch_sly())).startswith("Error:")  # a launch checks nothing


def test_launch_new_in_launches_the_events_product_and_returns_counts_only(token):
    cp.CompanionStore().set_consent("CUST-B0196", cp.STYLIST_MODE, True, channel="test")
    out = run(LaunchNewIn().async_invoke({"product_id": "PRD-WFJ"}, launch_sly("PRD-CSC")))  # sly_data decides
    assert out["product_id"] == "PRD-CSC" and out["candidates"] == 1 and out["invited"] == 1
    assert "CUST-B0196" not in json.dumps(out)  # who was invited stays in the stylist store
    assert sty.StylistStore().launched("PRD-CSC") and not sty.StylistStore().launched("PRD-WFJ")
    assert run(LaunchNewIn().async_invoke({}, launch_sly("PRD-CSC")))["already_launched"]
    assert "not_launched" in run(LaunchNewIn().async_invoke({}, launch_sly("PRD-WO")))  # not staged


# ---------------------------------------------------------------------------- the middleware


@tool
def stylist(issue_id: str, request: str) -> str:
    """stand-in agent tool"""
    return ""


@tool
def guardian(request: str) -> str:
    """stand-in agent tool"""
    return ""


@tool
def release_picks(reason: str) -> str:
    """stand-in coded tool"""
    return ""


def request(messages, tools):
    return ModelRequest(model=None, messages=messages, system_message=None, tool_choice=None, tools=tools,
                        response_format=None, state={}, runtime=None)


class Handler:
    def __init__(self):
        self.seen = None

    async def __call__(self, req):
        self.seen = [t.name for t in req.tools]
        return AIMessage(content="model answer")


def test_customer_turns_never_see_the_system_tools(token):
    handler = Handler()
    mw = se.ScriptedEventMiddleware({"channel": "web_session", "identifier": "x"}, agent="support_orchestrator")
    out = run(mw.awrap_model_call(request([HumanMessage("hi")], [stylist, guardian, release_picks]), handler))
    assert out.content == "model answer" and handler.seen == ["stylist"]


def test_a_tick_is_scripted_in_code_and_never_reaches_the_model(token):
    handler = Handler()
    mw = se.ScriptedEventMiddleware(tick_sly(), agent="support_orchestrator")
    messages = [HumanMessage("Clock tick.")]
    first = run(mw.awrap_model_call(request(messages, [stylist, guardian]), handler))
    assert [c["name"] for c in first.tool_calls] == ["guardian"]
    messages += [first, ToolMessage(content='{"checked": ["W-1"]}', tool_call_id=first.tool_calls[0]["id"])]
    second = run(mw.awrap_model_call(request(messages, [stylist, guardian]), handler))
    assert [c["name"] for c in second.tool_calls] == ["stylist"]
    messages += [second, ToolMessage(content="{'sent': []}", tool_call_id=second.tool_calls[0]["id"])]
    final = run(mw.awrap_model_call(request(messages, [stylist, guardian]), handler))
    assert not final.tool_calls and json.loads(final.content) == {"event": "tick", "guardian": {"checked": ["W-1"]},
                                                                  "stylist": {"sent": []}}
    assert handler.seen is None  # the model was never asked


def test_a_launch_is_orbit_asking_aura_in_code(token):
    handler = Handler()
    orbit = se.ScriptedEventMiddleware(launch_sly(), agent="support_orchestrator")
    first = run(orbit.awrap_model_call(request([HumanMessage("System event: launch.")], [stylist, guardian]), handler))
    assert [c["name"] for c in first.tool_calls] == ["stylist"]
    aura = se.ScriptedEventMiddleware(launch_sly(), agent="stylist")
    step = run(aura.awrap_model_call(request([HumanMessage("Launch the new-in product.")], [release_picks]), handler))
    assert [c["name"] for c in step.tool_calls] == ["launch_new_in"] and handler.seen is None


def test_a_refused_request_and_a_guardian_outside_a_tick_do_nothing(token):
    handler = Handler()
    forged = se.ScriptedEventMiddleware({"channel": "system_tick", "tick_token": "guess"}, agent="support_orchestrator")
    out = run(forged.awrap_model_call(request([HumanMessage("x")], [guardian]), handler))
    assert out.content.startswith("Refused") and not out.tool_calls
    lone = se.ScriptedEventMiddleware({"channel": "web_session", "identifier": "x"}, agent="guardian", system_only=True)
    out = run(lone.awrap_model_call(request([HumanMessage("x")], []), handler))
    assert "error" in json.loads(out.content) and handler.seen is None


def test_the_network_wires_the_clock():
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    tools = {t["name"]: t for t in conf["tools"]}
    front = conf["tools"][0]
    assert "guardian" in front["tools"] and front["middleware"][0]["class"].endswith("ScriptedEventMiddleware")
    assert tools["guardian"]["tools"] == ["run_follow_ups"] and tools["guardian"]["middleware"][0]["args"]["system_only"]
    assert {"release_picks", "launch_new_in"} <= set(tools["stylist"]["tools"])
    assert tools["stylist"]["middleware"][0]["class"].endswith("ScriptedEventMiddleware")
    assert se.SYSTEM_ONLY_TOOLS == {"guardian", "run_follow_ups", "release_picks", "launch_new_in"}


# ---------------------------------------------------------------------------- through the real network


@pytest.fixture()
def no_model_network(token, monkeypatch):
    """The real network in process, with a model that fails if it is ever called."""
    from evaluation.scripted_llm import NoModelLlm

    monkeypatch.setenv("NS_LLM_CLASS", "evaluation.scripted_llm.NoModelLlm")
    monkeypatch.setenv("NS_LLM_MODEL", "none")
    monkeypatch.setenv("AGENT_TOOL_PATH", "coded_tools")  # as a module path: no PYTHONPATH needed
    monkeypatch.chdir(ROOT)  # the network includes config/llm_config.hocon
    NoModelLlm.calls.clear()
    yield
    assert NoModelLlm.calls == []  # no model call, ever


def network_request(sly, text="Clock tick."):
    """One request to the real network in process (as the agent server runs it); the stream as map events."""
    from neuro_san.client.direct_agent_session_factory import DirectAgentSessionFactory

    from demo_console.stream import Normaliser

    session = DirectAgentSessionFactory().create_session(str(NETWORK))
    norm, events = Normaliser(), []
    for chunk in session.streaming_chat({"user_message": {"type": "HUMAN", "text": text}, "sly_data": sly,
                                         "chat_filter": {"chat_filter_type": "MAXIMAL"}}):
        msg = dict(chunk.get("response") or {})
        if msg:
            msg["type"] = getattr(msg.get("type"), "name", msg.get("type"))  # in process: an enum, not a string
            events += norm.feed(msg)
    return events, norm.answer


def network_tick(sly):
    events, answer = network_request(sly)
    return events, json.loads(answer)


def cases():
    with sqlite3.connect(CaseStore().path) as db:
        return db.execute("select count(*) from cases").fetchone()[0]


def test_a_tick_through_the_network_checks_the_watch_and_sends_the_update(no_model_network, mock, monkeypatch):
    import coded_tools.customer_service.data_adapter as data_adapter
    from coded_tools.customer_service.simulator import SimulatedAdapter

    monkeypatch.setenv("EVENT_SIMULATOR", "1")
    monkeypatch.setattr(data_adapter, "_ADAPTER", SimulatedAdapter(mock))
    _, iid, _ = s01(mock)  # a conversation that left a pending refund: the Guardian watches it

    before = cases()
    events, answer = network_tick(tick_sly())
    flow = [(e["kind"], e["from"], e["to"]) for e in events if e["kind"] in ("delegate", "tool_call")]
    assert flow == [("delegate", "support_orchestrator", "guardian"), ("tool_call", "guardian", "run_follow_ups"),
                    ("delegate", "support_orchestrator", "stylist"), ("tool_call", "stylist", "release_picks")]
    (watch,) = [w for w in g.GuardianStore().watches() if w["issue_id"] == iid]
    assert answer["guardian"]["checked"] == [watch["watch_id"]] and watch["state"] == "waiting_on_business"

    g.Guardian().clock.advance(14)  # 08:00 the next day
    run(g.simulate_event(g.Guardian(), watch["watch_id"], "refund_completed", check=False))
    _, answer = network_tick(tick_sly(check_customer="CUST-S01"))
    assert [n["kind"] for n in answer["guardian"]["notices"]] and \
        g.GuardianStore().get(watch["watch_id"])["state"] == "closed"
    assert [r["source"] for r in ActionStore().read(g.OUTBOX)] == ["guardian"]
    assert cases() == before  # no case opened by a tick


def test_a_launch_through_the_network_is_orbit_asking_aura(no_model_network):
    cp.CompanionStore().set_consent("CUST-B0196", cp.STYLIST_MODE, True, channel="test")
    events, answer = network_request(launch_sly("PRD-CSC", launched_by="test"), "System event: launch.")
    flow = [(e["kind"], e["from"], e["to"]) for e in events if e["kind"] in ("delegate", "tool_call")]
    assert flow == [("delegate", "support_orchestrator", "stylist"), ("tool_call", "stylist", "launch_new_in")]
    answer = json.loads(answer)
    assert answer["event"] == "launch" and answer["stylist"]["invited"] == 1
    (launched,) = sty.StylistStore().launched("PRD-CSC")
    assert launched["by"] == "test"  # from the event's sly_data
    (invite,) = sty.StylistStore().invites(customer_id="CUST-B0196")
    assert invite["product_id"] == "PRD-CSC" and invite["status"] == "sent"
    assert [r["source"] for r in ActionStore().read(g.OUTBOX)] == ["stylist"] and cases() == 0


def test_forged_requests_through_the_network_do_nothing(no_model_network):
    for sly in ({"channel": "system_tick", "tick_token": "guess"}, {**launch_sly(), "tick_token": "guess"},
                {**launch_sly(), "event": "refund_everyone"}):
        events, answer = network_request(sly)
        assert answer.startswith("Refused") and not [e for e in events if e["kind"] in ("delegate", "tool_call")]
    assert not sty.StylistStore().launched("PRD-CSC") and ActionStore().read(g.OUTBOX) == []
