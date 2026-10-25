"""
Multi-agent Phase 3 tests (no LLM): Payments & Billing and Loyalty & Account specialists, the billing-review
action, and the network wiring for the two new agents.
"""

import asyncio
import json

import pytest
from pyhocon import ConfigFactory

from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.specialist_tools import CheckPaymentConsistency
from coded_tools.customer_service.specialist_tools import FindItems
from coded_tools.customer_service.specialist_tools import GetLoyaltyProfile
from coded_tools.customer_service.specialist_tools import GetPaymentSummary
from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
from coded_tools.customer_service.specialist_tools import RecordFindings
from coded_tools.customer_service.specialist_tools import expected_payment_status
from coded_tools.customer_service.specialist_tools import payment_consistency
from test_multi_agent_foundation import NETWORK
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import issue
from test_multi_agent_foundation import mock  # noqa: F401 - module fixture used by isolated
from test_multi_agent_foundation import propose
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

PAY = {"domain": "payments_billing", "kind": "duplicate_charge", "summary": "Customer says they were charged twice."}
LOY = {"domain": "loyalty_account", "kind": "points_balance", "summary": "How many points do I have?"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------- payments

def test_payment_summary_s03_completed_refund(mock):
    sly = web(mock, "CUST-S03")
    (iid,) = start(sly, PAY)
    result = run(GetPaymentSummary().async_invoke({"issue_id": iid}, sly))
    (order,) = result["orders"]
    assert order["payment_last4"] == "4242" and order["refunds_completed_total"] == 55.0
    assert "method_token" not in json.dumps(result) and "neither confirmed nor ruled out" in result["notice"]
    assert issue(sly, iid)["evidence_sources"] == ["get_payment_summary"]


def test_payment_summary_for_an_order_not_on_the_account(mock):
    sly = web(mock, "CUST-S19A")
    (iid,) = start(sly, PAY)
    assert "not found" in run(GetPaymentSummary().async_invoke({"issue_id": iid, "order_id": "ORD-S19B-1"}, sly))


def test_every_order_in_the_data_is_payment_consistent(mock):
    # The generator keeps payment status in line with refunds, so any "inconsistent" here would be a rule bug.
    bad = []
    for customer_id in mock.customers:
        for row in run(mock.get_payment_summary(customer_id)):
            check = payment_consistency(row)
            if check["verdict"] != "consistent":
                bad.append(check)
    assert bad == []


@pytest.mark.parametrize("change, verdict, problem", [
    ({"payment_status": "refunded", "refunds_completed_total": 0.0}, "inconsistent",
     "payment_status_refunded_but_records_imply_captured"),
    ({"payments_on_order": 2}, "possible_duplicate", "multiple_payment_records"),
    ({"payment_id": None}, "insufficient_evidence", "no_payment_record"),
    ({"refunds_completed_total": 500.0, "payment_status": "refunded"}, "inconsistent",
     "completed_refunds_exceed_order_total"),
])
def test_consistency_rule_flags_bad_records(mock, change, verdict, problem):
    row = {**run(mock.get_payment_summary("CUST-S02"))[0], **change}
    check = payment_consistency(row)
    assert check["verdict"] == verdict and problem in check["problems"]


@pytest.mark.parametrize("order_status, total, refunded, expected", [
    ("processing", 50.0, 0.0, "authorized"), ("cancelled", 50.0, 0.0, "authorized"),
    ("delivered", 50.0, 0.0, "captured"), ("delivered", 50.0, 20.0, "partially_refunded"),
    ("delivered", 50.0, 50.0, "refunded"),
])
def test_expected_payment_status(order_status, total, refunded, expected):
    assert expected_payment_status(order_status, total, refunded) == expected


def test_consistency_tool_reports_and_guides(mock):
    sly = web(mock, "CUST-S09")
    (iid,) = start(sly, PAY)
    result = run(CheckPaymentConsistency().async_invoke({"issue_id": iid}, sly))
    assert result["verdicts"] == ["consistent"] and "do not disprove" in result["guidance"]


def test_double_charge_claim_goes_to_billing_review_s03(mock):
    sly = web(mock, "CUST-S03")
    (iid,) = start(sly, PAY)
    run(CheckPaymentConsistency().async_invoke({"issue_id": iid}, sly))
    propose(sly, iid, "open_billing_review", "ORD-S03-1")
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["reason"], result["issue_status"]) == \
        ("escalated", "billing_review", "queued_for_review")
    assert result["reference"].startswith("ACT-") and "billing team" in result["message_hint"]


def test_billing_review_never_executes_even_in_live_mode(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, PAY)
    propose(sly, iid, "open_billing_review", "ORD-S02-1")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "escalated"


def test_billing_review_for_risky_customer_keeps_reason_private(mock):
    sly = web(mock, "CUST-S15")
    order_id = run(mock.get_payment_summary("CUST-S15"))[0]["order_id"]
    (iid,) = start(sly, {**PAY, "order_id": order_id})  # the customer named the order
    propose(sly, iid, "open_billing_review", order_id)
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["reason"]) == ("escalated", "needs_specialist_review")
    assert "fraud" not in json.dumps(result).lower() and "risk" not in json.dumps(result).lower()


def test_weak_session_billing_review_needs_step_up(mock):
    sly = {"customer_id": "CUST-S03", "channel": "phone", "auth_level": "weak"}
    (iid,) = start(sly, PAY)
    propose(sly, iid, "open_billing_review", "ORD-S03-1")
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["reason"]) == ("denied", "step_up_required")


def test_payments_agent_can_find_the_order_but_not_use_returns_tools(mock):
    sly = web(mock, "CUST-S03")
    (iid,) = start(sly, PAY)
    assert run(FindItems().async_invoke({"issue_id": iid, "item_query": "shirt"}, sly))["result"] == "unique"
    assert run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly)).startswith("Error: issue")


def test_payments_agent_cannot_propose_refunds(mock):
    sly = web(mock, "CUST-S03")
    (iid,) = start(sly, PAY)
    result = run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "x", "proposed_action": {
        "action": "initiate_refund", "target_id": "ITM-S03-1-1"}}, sly))
    assert "not an action for payments_billing" in result and "open_billing_review" in result


# ---------------------------------------------------------------------------- loyalty

def test_loyalty_profile_s13(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, LOY)
    result = run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly))
    assert (result["points"], result["tier"], result["tier_records_agree"]) == (8800, "platinum", True)
    assert "cannot be traced" in result["notice"] and "data_inconsistency" not in result["notice"]


def test_loyalty_tier_mismatch_is_flagged_for_a_person(mock, monkeypatch):
    original = MockAdapter.get_loyalty_profile

    async def mismatched(self, customer_id):
        return [{**r, "customer_tier": "standard"} for r in await original(self, customer_id)]

    monkeypatch.setattr(MockAdapter, "get_loyalty_profile", mismatched)
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, LOY)
    result = run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly))
    assert result["tier"] == "platinum" and not result["tier_records_agree"]
    assert "data_inconsistency" in result["notice"]


def test_loyalty_agent_can_propose_nothing(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, LOY)
    result = run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "x", "proposed_action": {
        "action": "open_billing_review", "target_id": "ORD-S13-1"}}, sly))
    assert "allowed: none" in result
    ok = run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "8,800 points, platinum."}, sly))
    assert ok["status"] == "investigating" and ok["proposal"] is None


def answer(sly: dict, iid: str) -> dict:
    return run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "8,800 points, platinum.",
                                              "answered": True}, sly))


def test_loyalty_specialist_closes_a_balance_question_itself(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, LOY)
    run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly))
    assert answer(sly, iid) == {"issue_id": iid, "status": "resolved_info_only", "proposal": None, "closed": True}


def test_loyalty_specialist_cannot_close_missing_points(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, {**LOY, "kind": "missing_points", "summary": "My points from last order are missing."})
    run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly))
    result = answer(sly, iid)
    assert not result["closed"] and "cannot be traced" in result["not_closed_because"]
    assert issue(sly, iid)["status"] == "investigating"


def test_loyalty_specialist_cannot_close_a_tier_mismatch(mock, monkeypatch):
    original = MockAdapter.get_loyalty_profile

    async def mismatched(self, customer_id):
        return [{**r, "customer_tier": "standard"} for r in await original(self, customer_id)]

    monkeypatch.setattr(MockAdapter, "get_loyalty_profile", mismatched)
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, LOY)
    run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly))
    result = answer(sly, iid)
    assert not result["closed"] and "data_inconsistency" in result["not_closed_because"]


def test_loyalty_tool_rejects_other_domains(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, PAY)
    assert run(GetLoyaltyProfile().async_invoke({"issue_id": iid}, sly)).startswith("Error: issue")


# ---------------------------------------------------------------------------- multi-issue flow (M1 shape)

def test_charged_twice_and_return_one_item_keeps_separate_statuses(mock):
    sly = web(mock, "CUST-S03")
    pay, ret = start(sly, PAY, {"domain": "returns_refunds", "kind": "return_request",
                                "summary": "Wants to return the shirt", "item_query": "shirt"})
    run(CheckPaymentConsistency().async_invoke({"issue_id": pay}, sly))
    propose(sly, pay, "open_billing_review", "ORD-S03-1")
    assert run(SubmitAction().async_invoke({"issue_id": pay}, sly))["issue_status"] == "queued_for_review"
    run(FindItems().async_invoke({"issue_id": ret}, sly))
    # The shirt was already returned and refunded, so a new return is refused and the issue stays open.
    propose(sly, ret, "create_return", "ITM-S03-1-1")
    denied = run(SubmitAction().async_invoke({"issue_id": ret, "customer_confirmed": True}, sly))
    assert (denied["outcome"], denied["reason"]) == ("denied", "return_exists")
    assert issue(sly, pay)["status"] == "queued_for_review" and issue(sly, ret)["status"] == "proposed"


# ---------------------------------------------------------------------------- network wiring

def test_network_has_all_four_specialists_and_routes_to_them():
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    tools = {t["name"]: t for t in conf["tools"]}
    front = conf["tools"][0]
    for agent in ("order_delivery", "returns_refunds", "payments_billing", "loyalty_account"):
        assert agent in front["tools"] and "instructions" in tools[agent]
    assert list(tools["loyalty_account"]["tools"]) == ["get_loyalty_profile", "record_findings"]
    assert "submit_action" not in tools["payments_billing"]["tools"]


def test_billing_review_is_policy_configured():
    policy = load_policy()
    assert policy["action_domains"]["payments_billing"] == ["open_billing_review"]
    assert policy["always_review_actions"] == ["open_billing_review"]
