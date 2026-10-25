"""
Companion plan P6 (no LLM): the stylist agent's tools (journey J9). The customer asks about a pick in chat: the
answer comes only from what was really sent (the invite, its stored breakdown, the catalog now), never a score;
nothing can be ordered; "not for me" is recorded as feedback and lowers similar launches.
"""

import asyncio
import json

import pytest

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import recommendations
from coded_tools.customer_service import stylist as sty
from coded_tools.customer_service.stylist_tools import ExplainPick
from coded_tools.customer_service.stylist_tools import GetStylistPicks
from coded_tools.customer_service.stylist_tools import PickNotForMe
from coded_tools.customer_service.simulator import Simulator
from test_multi_agent_foundation import NETWORK
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import issue
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

FAN, SCARF = "CUST-B0196", "PRD-CSC"
ASK = {"domain": "stylist", "kind": "pick_question", "summary": "tell me about the scarf you sent"}
NOPE = {"domain": "stylist", "kind": "pick_feedback", "summary": "the scarf is not for me"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def launched(mock, monkeypatch, tmp_path):
    """FAN switched Stylist Mode on and was sent the cashmere scarf."""
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))
    cp._DATA_CACHE.clear()
    cp._STYLE_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()
    cp.CompanionStore().set_consent(FAN, cp.STYLIST_MODE, True, channel="test")
    stylist = sty.Stylist(adapter=mock, simulator=Simulator(tmp_path / "simulator.json"))
    run(stylist.launch(SCARF))
    assert stylist.store.invites()[0]["status"] == "sent"
    return stylist


def picks(sly, iid):
    return run(GetStylistPicks().async_invoke({"issue_id": iid}, sly))


def test_the_pick_comes_from_what_was_sent_without_scores(launched, mock):
    sly = web(mock, FAN)
    (iid,) = start(sly, ASK)
    out = picks(sly, iid)
    (pick,) = out["picks"]
    assert (pick["title"], pick["list_price"], pick["product_id"]) == ("Cashmere Scarf", 87.99, SCARF)
    assert pick["picked_because"][0] == "It goes very well with your navy wool overcoat."
    assert pick["in_stock_now"]["size"] == "one size" and "navy" in pick["in_stock_now"]["colours"]
    data = json.dumps(out["picks"]).lower()  # the hint itself says "never mention scores"
    assert "score" not in data and "signal" not in data and FAN.lower() not in data
    assert "Nothing can be ordered" in out["message_hint"]
    assert out["closed"] and issue(sly, iid)["status"] == "resolved_info_only"


def test_explain_pick_gives_reasons_only_for_a_sent_pick(launched, mock):
    sly = web(mock, FAN)
    (iid,) = start(sly, ASK)
    out = run(ExplainPick().async_invoke({"issue_id": iid, "product_id": "prd-csc"}, sly))
    assert out["title"] == "Cashmere Scarf" and len(out["picked_because"]) >= 3
    assert any("style" in r for r in out["picked_because"])
    other = run(ExplainPick().async_invoke({"issue_id": iid, "product_id": "PRD-WFJ"}, sly))
    assert other.startswith("Error:") and "not one of the picks" in other


def test_not_for_me_is_recorded_and_lowers_similar_launches(launched, mock):
    sly = web(mock, FAN)
    (iid,) = start(sly, NOPE)
    out = run(PickNotForMe().async_invoke({"issue_id": iid, "product_id": SCARF}, sly))
    assert out["status"] == "recorded" and out["closed"]
    assert launched.store.feedback(FAN)[0]["kind"] == "not_for_me"
    (iid2,) = start(sly, ASK)
    assert picks(sly, iid2)["picks"][0]["your_feedback"] == "not_for_me"


def test_not_for_me_needs_a_sign_in_that_may_change_things(launched, mock):
    phone = next(i["identifier"] for i in mock.identities if i["customer_id"] == FAN and i["channel"] == "phone")
    sly = {"channel": "phone", "identifier": phone}
    (iid,) = start(sly, NOPE)
    out = run(PickNotForMe().async_invoke({"issue_id": iid, "product_id": SCARF}, sly))
    assert out["status"] == "not_allowed" and launched.store.feedback(FAN) == []


def test_no_picks_says_why(mock, monkeypatch, tmp_path):
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))
    sly = web(mock, "CUST-S01")
    (iid,) = start(sly, ASK)
    off = picks(sly, iid)
    assert off["picks"] == [] and off["stylist_mode"] == "off" and "switch it on" in off["message_hint"]
    cp.CompanionStore().set_consent("CUST-S01", cp.STYLIST_MODE, True, channel="test")
    (iid2,) = start(sly, ASK)
    assert "No picks have been sent" in picks(sly, iid2)["message_hint"]


def test_stylist_tools_only_answer_a_stylist_issue(launched, mock):
    sly = web(mock, FAN)
    (iid,) = start(sly, {"domain": "other", "kind": "question", "summary": "hello"})
    assert picks(sly, iid).startswith("Error:")


def test_scorer_checks_the_pick_was_sent_and_the_feedback_recorded():
    from evaluation.expectations import conversations
    from evaluation.score import score_stylist

    n1 = next(c for c in conversations() if c.id == "N1")
    ok = {"stylist": {"invites": [{"product_id": SCARF, "status": "sent"}],
                      "feedback": [{"product_id": SCARF, "kind": "not_for_me"}]}}
    assert score_stylist(n1, ok)["stylist_ok"]
    missing = score_stylist(n1, {"stylist": {"invites": [{"product_id": SCARF, "status": "held"}], "feedback": []}})
    assert missing["stylist_ok"] is False and len(missing["stylist_problems"]) == 2


def test_naming_a_pick_that_was_sent_is_not_an_invented_product():
    from evaluation.expectations import conversations
    from evaluation.score import score_recommendations

    n1 = next(c for c in conversations() if c.id == "N1")
    text = "The Cashmere Scarf is $87.99."
    assert score_recommendations(n1, text, {})["invented_products"] == ["Cashmere Scarf"]
    assert score_recommendations(n1, text, {}, {SCARF})["invented_products"] == []


def test_the_network_has_the_stylist_agent_and_routes_to_it():
    from pyhocon import ConfigFactory

    for path in (NETWORK, NETWORK.with_name("support_multi_agent_lean.hocon")):
        tools = {t["name"]: t for t in ConfigFactory.parse_file(str(path))["tools"]}
        # release_picks, launch_new_in: Orbit's system requests only; hidden from the model on customer turns
        # (test_orbit_tick)
        assert tools["stylist"]["tools"] == ["get_stylist_picks", "explain_pick", "pick_not_for_me", "release_picks",
                                             "launch_new_in"]
        assert "stylist" in tools["support_orchestrator"]["tools"]
        assert "stylist" in tools["support_orchestrator"]["instructions"]
        assert all(tools[n]["class"].startswith("customer_service.stylist_tools.") for n in tools["stylist"]["tools"])
