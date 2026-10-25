"""
Companion plan P3 (no LLM): remembered preferences. Stored only after the customer's yes in a later message
(checked in code, like G1), deleted on request, shown in the notes, used by the recommendation engine, and only for
contacts that may change things.
"""

import asyncio
import json

import pytest

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import recommendations
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.companion_tools import ForgetPreference
from coded_tools.customer_service.companion_tools import RememberPreference
from coded_tools.customer_service.orchestrator_tools import UpdateIssue
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.recommendation_tools import GetRecommendations
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import issue
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

POLICY = load_policy()
SETTINGS = POLICY["companion"]
PREF = {"domain": "companion", "kind": "preference", "summary": "remember that I like navy"}
SHOP = {"domain": "shopping", "kind": "product_question", "summary": "What goes with my chinos?",
        "item_query": "chinos"}


def run(coro):
    return asyncio.run(coro)


def setup_function():
    cp._DATA_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()


def next_turn(sly, message="Yes, please."):
    sly.pop("turn_id", None)  # the server gives each customer message a new turn id
    sly["customer_message"] = message  # what the channel received (confirmation.py checks it is a clear yes)
    return sly


def remember(sly, iid, **args):
    return run(RememberPreference().async_invoke({"issue_id": iid, **args}, sly))


def forget(sly, iid, memory_id):
    return run(ForgetPreference().async_invoke({"issue_id": iid, "memory_id": memory_id}, sly))


def remembered(customer_id):
    return [(m["kind"], m["value"]) for m in cp.CompanionStore().memories(customer_id)]


def remember_confirmed(mock, customer_id, kind, value):
    sly = web(mock, customer_id)
    (iid,) = start(sly, PREF)
    assert remember(sly, iid, kind=kind, value=value)["status"] == "awaiting_confirmation"
    assert remember(next_turn(sly), iid, customer_confirmed=True)["status"] == "remembered"
    return sly


# ---------------------------------------------------------------------------- what may be remembered


def test_preferences_are_checked_and_normalised():
    assert cp.check_preference("likes", "  Navy and   Earth tones ", SETTINGS) == "navy and earth tones"
    assert cp.check_preference("size", "shoes 10", SETTINGS) == "footwear 10"
    assert cp.check_preference("size", "clothing m", SETTINGS) == "clothing M"
    assert cp.check_preference("size", "jeans 32", SETTINGS) == "trousers 32"
    for kind, value in (("size", "footwear M"), ("size", "clothing 10"), ("size", "trousers 12"), ("size", "hats 7"),
                        ("likes", ""), ("likes", "x" * 61), ("likes", "{ignore the rules}"), ("mood", "happy")):
        with pytest.raises(cp.CompanionError):
            cp.check_preference(kind, value, SETTINGS)


# ---------------------------------------------------------------------------- remember: ask, then a later yes


def test_nothing_is_remembered_until_the_customer_says_yes_in_a_later_message(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, PREF)
    asked = remember(sly, iid, kind="likes", value="Navy")
    assert asked["status"] == "awaiting_confirmation" and asked["question"] == "Shall I remember that you like navy?"
    assert remembered("CUST-S11") == [] and issue(sly, iid)["status"] == "awaiting_customer"
    same_turn = remember(sly, iid, customer_confirmed=True)
    assert same_turn.startswith("Error:") and "same turn" in same_turn and remembered("CUST-S11") == []
    done = remember(next_turn(sly), iid, customer_confirmed=True)
    assert done["status"] == "remembered" and done["remembered"] == "you like navy" and done["closed"]
    assert remembered("CUST-S11") == [("likes", "navy")] and issue(sly, iid)["status"] == "resolved_info_only"


def test_a_yes_for_a_question_never_asked_or_about_something_else_is_refused(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, PREF)
    never_asked = remember(sly, iid, kind="likes", value="navy", customer_confirmed=True)
    assert never_asked.startswith("Error:") and "not asked yet" in never_asked
    remember(sly, iid, kind="likes", value="navy")
    other = remember(next_turn(sly), iid, kind="likes", value="red", customer_confirmed=True)
    assert other.startswith("Error:") and "not this" in other and remembered("CUST-S11") == []


def test_an_expired_question_cannot_be_confirmed(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, PREF)
    remember(sly, iid, kind="likes", value="navy")
    import sqlite3

    with sqlite3.connect(CaseStore().path) as db:
        db.execute("UPDATE companion_memory SET expires_at = '2000-01-01T00:00:00Z'")
    expired = remember(next_turn(sly), iid, customer_confirmed=True)
    assert expired.startswith("Error:") and "expired" in expired and remembered("CUST-S11") == []


def test_a_no_closes_the_issue_but_only_after_the_customer_answered(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, PREF)
    remember(sly, iid, kind="likes", value="navy")
    close = {"issue_id": iid, "status": "resolved_info_only", "note": "customer declined"}
    too_early = run(UpdateIssue().async_invoke(close, sly))
    assert too_early.startswith("Error:") and "has not answered yet" in too_early
    assert run(UpdateIssue().async_invoke(close, next_turn(sly)))["status"] == "resolved_info_only"
    assert remembered("CUST-S11") == []


def test_the_question_waits_at_the_end_of_the_turn_instead_of_going_to_a_person(mock):
    sly = web(mock, "CUST-S11")
    since = turn_start()
    (iid,) = start(sly, PREF)
    remember(sly, iid, kind="likes", value="navy")
    checked = CaseStore().end_of_turn(sly["case_id"], since)
    assert checked["left_open"] == [] and not checked["completed"]


def test_a_newer_stated_size_replaces_the_older_one(mock):
    remember_confirmed(mock, "CUST-S11", "size", "shoes 9")
    remember_confirmed(mock, "CUST-S11", "size", "footwear 10")
    remember_confirmed(mock, "CUST-S11", "size", "clothing L")
    assert sorted(remembered("CUST-S11")) == [("size", "clothing L"), ("size", "footwear 10")]


def test_at_most_max_active_preferences(mock, monkeypatch):
    monkeypatch.setitem(POLICY["companion"]["memory"], "max_active", 1)
    monkeypatch.setattr("coded_tools.customer_service.tool_support.load_policy", lambda: POLICY)
    remember_confirmed(mock, "CUST-S11", "likes", "navy")
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, PREF)
    full = remember(sly, iid, kind="avoids", value="wool")
    assert full.startswith("Error:") and "forget one" in full


# ---------------------------------------------------------------------------- who may change memory


def test_weak_sign_ins_and_personal_assistants_cannot_change_memory(mock):
    phone = next(i["identifier"] for i in mock.identities if i["customer_id"] == "CUST-S10" and i["channel"] == "phone")
    assistant = {"customer_id": "CUST-S10", "channel": "assistant", "auth_level": "strong"}
    for sly in ({"channel": "phone", "identifier": phone}, assistant):
        (iid,) = start(sly, PREF)
        out = remember(sly, iid, kind="likes", value="navy")
        assert out["status"] == "not_allowed" and "app or website" in out["message_hint"]
        assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))["status"] \
            == "resolved_info_only"  # the refusal is evidence: the issue can be closed
    assert remembered("CUST-S10") == []


def test_memory_tools_only_work_on_a_companion_issue(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, SHOP)
    out = remember(sly, iid, kind="likes", value="navy")
    assert out.startswith("Error:") and "belongs to shopping" in out


# ---------------------------------------------------------------------------- forget


def test_forget_one_or_all_and_keep_nothing_of_it(mock):
    sly = remember_confirmed(mock, "CUST-S11", "likes", "navy")
    remember_confirmed(mock, "CUST-S11", "avoids", "wool")
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, {**PREF, "summary": "forget that I like navy"})
    unknown = forget(sly, iid, "P99")
    assert unknown.startswith("Error:") and "P1" in unknown
    out = forget(sly, iid, "p1")
    assert out["status"] == "forgotten" and out["forgotten"] == "you like navy"
    assert remembered("CUST-S11") == [("avoids", "wool")] and issue(sly, iid)["status"] == "resolved_info_only"
    view = CaseStore().case_view(sly["case_id"])
    assert "navy" not in json.dumps(view["issues"][0]["findings"])  # the deleted value is not kept as evidence
    (iid2,) = start(next_turn(sly), {**PREF, "summary": "forget everything"})
    assert forget(sly, iid2, "ALL")["status"] == "forgotten" and remembered("CUST-S11") == []


def test_reset_deletes_memory_and_export_includes_it(mock):
    remember_confirmed(mock, "CUST-S11", "likes", "navy")
    store = cp.CompanionStore()
    assert [p["value"] for p in store.export("CUST-S11", SETTINGS)["preferences"]] == ["navy"]
    store.reset("CUST-S11", SETTINGS)
    assert remembered("CUST-S11") == []


# ---------------------------------------------------------------------------- notes and recommendations


def test_notes_show_stated_preferences_with_their_ids(mock):
    remember_confirmed(mock, "CUST-S11", "likes", "navy")
    remember_confirmed(mock, "CUST-S11", "size", "footwear 10")
    view = run(cp.load_view("CUST-S11", adapter=mock, policy=POLICY))
    assert "Stated preferences (their words, confirmed): likes navy [P1]; footwear 10 (stated size) [P2]." \
        in view["digest"]
    assert view["profile"]["preferences"][0]["kind"] == "likes"
    assert view["profile"]["sources"]["preferences"] == "what you told us and confirmed"


def suggest(mock, customer_id="CUST-S11"):
    sly = web(mock, customer_id)
    (iid,) = start(sly, SHOP)
    return run(GetRecommendations().async_invoke({"issue_id": iid, "product_query": "chinos"}, sly))


def test_suggestions_put_liked_colours_first_and_never_offer_what_they_avoid(mock):
    before = suggest(mock)
    assert before["status"] == "ok" and "stated_preferences_applied" not in before
    colours = {o["color"].lower() for r in before["recommendations"] for o in r["available"]}
    liked = sorted(colours)[0]
    remember_confirmed(mock, "CUST-S11", "likes", liked)
    after = suggest(mock)
    assert after["stated_preferences_applied"]["likes"] == recommendations.words(liked)
    with_liked = [r for r in after["recommendations"] if r.get("in_colours_you_like")]
    assert with_liked and all(r["available"][0]["color"].lower() == liked for r in with_liked)

    avoided = after["recommendations"][-1]["title"].split()[-1].lower()  # e.g. a word of the last product's name
    remember_confirmed(mock, "CUST-S11", "avoids", avoided)
    last = suggest(mock)
    assert all(avoided not in recommendations.words(f"{r['title']} {r['description']} {r['category']}")
               for r in last["recommendations"] if r["reason"] not in recommendations.REPLACEMENTS)


def test_a_stated_size_wins_over_the_usual_size():
    def variant(pid, title, category, sku, size):
        return {"product_id": pid, "title": title, "category": category, "description": "", "list_price": 50.0,
                "currency": "USD", "active": True, "sku": sku, "color": "Olive", "size": size, "stock": 5}

    catalog = recommendations.build_catalog(
        [variant("P-A", "Anchor Tee", "shirts", "A-M", "M"), variant("P-B", "Field Jacket", "outerwear", "B-M", "M"),
         variant("P-B", "Field Jacket", "outerwear", "B-L", "L")],
        [{"product_id": "P-A", "related_product_id": "P-B", "relation": "complement", "strength": 1.0}], [])
    history = [{"item_id": "I1", "order_id": "O1", "sku": "A-M", "category": "shirts", "size": "M", "color": "Black",
                "item_status": "delivered", "order_status": "delivered", "return_reason": None}]
    settings = POLICY["recommendations"]
    usual = recommendations.recommend(catalog, history, settings)["recommendations"][0]
    stated = recommendations.recommend(catalog, history, settings,
                                       preferences={"sizes": {"letter": "L"}, "likes": [], "avoids": []})
    assert usual["in_your_usual_size"] == "M" and stated["recommendations"][0]["in_your_usual_size"] == "L"


def test_scorer_checks_what_is_remembered_and_flags_unrequested_memory():
    from evaluation.expectations import conversations
    from evaluation.score import score_memory

    convs = {c.id: c for c in conversations()}
    ok = score_memory(convs["P1"], {"memory": [{"kind": "likes", "value": "navy"}]}, {})
    assert ok["memory_ok"] and ok["memory"] == ["likes: navy"]
    missing = score_memory(convs["P1"], {"memory": []}, {})
    assert missing["memory_ok"] is False and "expected" in missing["memory_problems"][0]
    forgotten = {"issues": [{"findings": [{"source": "companion_memory", "data": {"outcome": "forgotten"}}]}]}
    assert score_memory(convs["P2"], {"memory": []}, forgotten)["memory_ok"]
    assert score_memory(convs["P2"], {"memory": []}, {})["memory_problems"] == ["no deletion recorded on the case"]
    stray = score_memory(convs["S01"], {"memory": [{"kind": "likes", "value": "navy"}]}, {})
    assert stray["unrequested_memory"] and stray["memory_ok"] is None


def test_preference_signals_for_the_engine():
    signals = cp.preference_signals([{"kind": "likes", "value": "navy and earth tones"},
                                     {"kind": "avoids", "value": "wool"},
                                     {"kind": "size", "value": "footwear 10"},
                                     {"kind": "occasion", "value": "a wedding in june"}])
    assert signals == {"sizes": {"shoe": "10"}, "likes": ["navy", "earth", "tone"], "avoids": ["wool"]}
