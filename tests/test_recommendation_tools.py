"""
Recommendations in the network (no LLM): the get_recommendations tool (used by the product advisor agent, E1) and its
wiring.
On request only (shopping issues), the customer's other requests first, eligibility in code, SKUs kept as evidence
but never shown to the model.
"""

import asyncio
import json

from pyhocon import ConfigFactory

from coded_tools.customer_service import recommendations
from coded_tools.customer_service.case_store import ISSUE_KINDS
from coded_tools.customer_service.orchestrator_tools import HandoffToHuman
from coded_tools.customer_service.orchestrator_tools import UpdateIssue
from coded_tools.customer_service.recommendation_tools import GetRecommendations
from coded_tools.customer_service.specialist_tools import GetOrders
from test_multi_agent_foundation import NETWORK
from test_multi_agent_foundation import S18_PHONE
from test_multi_agent_foundation import issue
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

SHOP = {"domain": "shopping", "kind": "product_question", "summary": "What goes with my chinos?",
        "item_query": "chinos"}


def run(coro):
    return asyncio.run(coro)


def suggest(sly, iid, query="chinos"):
    return run(GetRecommendations().async_invoke({"issue_id": iid, "product_query": query}, sly))


def setup_function():
    recommendations._CATALOG_CACHE.clear()


def test_suggestions_for_a_shopping_issue_then_the_issue_closes(mock):
    sly = web(mock, "CUST-S11")  # returned the olive chinos for size
    (iid,) = start(sly, SHOP)
    out = suggest(sly, iid)
    assert out["status"] == "ok" and out["issue_id"] == iid and 1 <= len(out["recommendations"]) <= 3
    assert "SKU-" not in json.dumps(out)  # the model names colours and sizes, never SKUs
    assert all(set(o) == {"color", "size"} for r in out["recommendations"] for o in r["available"])
    stored = issue(sly, iid)
    assert stored["status"] == "investigating" and stored["evidence_sources"] == ["get_recommendations"]
    closed = run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    assert closed["status"] == "resolved_info_only"


def test_evidence_keeps_the_skus_for_checking(mock):
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, SHOP)
    suggest(sly, iid)
    from coded_tools.customer_service.tool_support import case_store

    finding = case_store().case_view(sly["case_id"])["issues"][0]["findings"][0]
    assert finding["source"] == "get_recommendations" and finding["author"] == "tool"
    assert all(o["sku"].startswith("SKU-") for r in finding["data"]["recommendations"] for o in r["available"])


def test_only_a_shopping_issue_can_get_suggestions(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "Where is my order?"})
    assert "belongs to order_delivery, not to this agent" in suggest(sly, iid)


def test_the_customers_other_requests_come_first(mock):
    sly = web(mock, "CUST-S02")
    order, shop = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "Where is it?"}, SHOP)
    refused = suggest(sly, shop)
    assert "other requests come first" in refused and order in refused
    run(GetOrders().async_invoke({"issue_id": order}, sly))
    run(UpdateIssue().async_invoke({"issue_id": order, "status": "resolved_info_only"}, sly))
    assert suggest(sly, shop)["status"] in ("ok", "none_suitable", "no_anchor")


def test_weak_sign_in_gets_no_suggestions(mock):
    sly = {"customer_id": "CUST-S10", "channel": "phone", "auth_level": "weak"}
    (iid,) = start(sly, SHOP)
    out = suggest(sly, iid, "shoes")
    assert (out["status"], out["reason"], out["recommendations"]) == ("not_offered", "sign_in_too_weak", [])


def test_unidentified_contact_gets_no_suggestions_s18(mock):
    sly = {"channel": "phone", "identifier": S18_PHONE}  # shared household phone: ambiguous
    (iid,) = start(sly, SHOP)
    out = suggest(sly, iid)
    assert (out["status"], out["reason"]) == ("not_offered", "not_identified")


def test_no_suggestions_while_a_person_handles_the_case(mock):
    sly = web(mock, "CUST-S02")
    person, shop = start(sly, {"domain": "other", "kind": "human_requested", "summary": "A person please"}, SHOP)
    run(HandoffToHuman().async_invoke({"issue_ids": [person], "reason": "customer_requested",
                                       "summary": "Wants a person."}, sly))
    out = suggest(sly, shop)
    assert (out["status"], out["reason"]) == ("not_offered", "case_with_a_person")


def test_risk_flags_refuse_without_saying_why_s15(mock):
    sly = web(mock, "CUST-S15")
    (iid,) = start(sly, SHOP)
    out = suggest(sly, iid, "jacket")
    assert out["reason"] == "not_available"
    assert not any(w in json.dumps(out).lower() for w in ("risk", "fraud", "refund_rate", "chargeback"))


def test_network_wiring():
    """E1: the product advisor agent owns get_recommendations; the orchestrator delegates shopping issues to it."""
    network = ConfigFactory.parse_file(str(NETWORK))
    tools = {t["name"]: t for t in network["tools"]}
    orchestrator = tools["support_orchestrator"]
    assert "product_advisor" in orchestrator["tools"] and "get_recommendations" not in orchestrator["tools"]
    assert tools["product_advisor"]["tools"] == ["get_recommendations"]
    assert tools["get_recommendations"]["class"] == "customer_service.recommendation_tools.GetRecommendations"
    assert ISSUE_KINDS["shopping"] == ("product_question",)
    assert "shopping           -> product_advisor" in orchestrator["instructions"]
    assert "shopping" in tools["intake_identity"]["instructions"]


def test_intake_can_record_every_domain_the_code_knows():
    """The add_issues schema the model sees lists exactly case_store.DOMAINS (it once missed shopping)."""
    from coded_tools.customer_service.case_store import DOMAINS

    network = ConfigFactory.parse_file(str(NETWORK))
    add_issues = next(t for t in network["tools"] if t["name"] == "add_issues")
    schema = add_issues["function"]["parameters"]["properties"]["issues"]["items"]["properties"]["domain"]
    assert list(schema["enum"]) == list(DOMAINS)
