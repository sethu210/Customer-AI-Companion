"""
Multi-agent Phase 2 tests (no LLM): identity resolution in code, the SQLite case store, and every coded tool
of the support_multi_agent network, driven in the order the agents would call them.
"""

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from neuro_san.interfaces.coded_tool import CodedTool
from pyhocon import ConfigFactory

from coded_tools.customer_service.action_store import ActionStore
import coded_tools.customer_service.tool_support as tool_support
from coded_tools.customer_service.case_store import DOMAIN_OF_KIND
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.identity import ensure_identity
from coded_tools.customer_service.intake_tools import AddIssues
from coded_tools.customer_service.intake_tools import GetContactHistory
from coded_tools.customer_service.intake_tools import OpenCase
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.orchestrator_tools import ACTION_KINDS
from coded_tools.customer_service.orchestrator_tools import CaseState
from coded_tools.customer_service.orchestrator_tools import HandoffToHuman
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.orchestrator_tools import UpdateIssue
from coded_tools.customer_service.policy import ITEM_ACTIONS
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.specialist_tools import CheckReturnEligibility
from coded_tools.customer_service.specialist_tools import FindItems
from coded_tools.customer_service.specialist_tools import GetOrders
from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
from coded_tools.customer_service.specialist_tools import OrderTimeline
from coded_tools.customer_service.specialist_tools import RecordFindings
from coded_tools.customer_service.specialist_tools import StuckCases

ROOT = Path(__file__).parents[1]
NETWORK = ROOT / "registries" / "customer_service" / "support_multi_agent.hocon"
S18_PHONE = "+1-299-555-0150"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, mock):
    import coded_tools.customer_service.data_adapter as data_adapter

    monkeypatch.setattr(data_adapter, "_ADAPTER", mock)
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "cases.db"))
    monkeypatch.setenv("ACTION_STORE_DIR", str(tmp_path / "actions"))
    monkeypatch.setenv("ACTION_MODE", "shadow")
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))  # the Guardian's clock (E3)


def web(mock: MockAdapter, customer_id: str) -> dict:
    """sly_data as the gateway sends it for a logged-in web session (raw identity, resolved in code)."""
    ident = next(i["identifier"] for i in mock.identities
                 if i["customer_id"] == customer_id and i["channel"] == "web_session")
    return {"channel": "web_session", "identifier": ident}


def start(sly: dict, *issues: dict) -> list[str]:
    """Intake: open the case and add issues; returns the issue ids."""
    opened = run(OpenCase().async_invoke({}, sly))
    assert opened["case_id"] == sly["case_id"]
    added = run(AddIssues().async_invoke({"issues": list(issues)}, sly))
    return [i["issue_id"] for i in added["issues"]]


def issue(sly: dict, issue_id: str) -> dict:
    view = run(CaseState().async_invoke({"op": "get"}, sly))
    return next(i for i in view["issues"] if i["issue_id"] == issue_id)


# ---------------------------------------------------------------------------- identity in code

def test_web_session_identity_is_resolved_in_code(mock):
    sly = web(mock, "CUST-S01")
    summary = run(ensure_identity(sly, mock, load_policy()))
    assert summary["identity_status"] == "verified" and summary["can_request_changes"]
    assert sly["customer_id"] == "CUST-S01" and sly["auth_level"] == "strong"
    assert "CUST-S01" not in json.dumps(summary)  # nothing the LLM sees carries the id


def test_shared_phone_is_ambiguous_and_opens_no_account(mock):
    sly = {"channel": "phone", "identifier": S18_PHONE}
    summary = run(ensure_identity(sly, mock, load_policy()))
    assert summary["identity_status"] == "ambiguous" and not summary["can_read_account"]
    assert "customer_id" not in sly


def test_gateway_authenticated_session_is_trusted(mock):
    sly = {"customer_id": "CUST-S08", "channel": "phone"}
    summary = run(ensure_identity(sly, mock, load_policy()))
    assert summary["identity_status"] == "gateway_authenticated"
    assert sly["auth_level"] == "weak" and not summary["can_request_changes"]  # missing level = weak


@pytest.mark.parametrize("sly, status", [
    ({"channel": "email", "identifier": "nobody@example.invalid"}, "not_found"),
    ({"channel": "email"}, "missing"),
    ({}, "missing"),
])
def test_unidentified_contacts(mock, sly, status):
    assert run(ensure_identity(sly, mock, load_policy()))["identity_status"] == status
    assert "customer_id" not in sly


def test_unverified_identity_opens_no_account(mock):
    class Unverified(MockAdapter):
        def __init__(self, base):
            self.__dict__.update(base.__dict__)

        async def resolve_channel_identity(self, channel, identifier):
            rows = await super().resolve_channel_identity(channel, identifier)
            return [{**r, "verified": False} for r in rows]

    sly = web(mock, "CUST-S01")
    assert run(ensure_identity(sly, Unverified(mock), load_policy()))["identity_status"] == "unverified"
    assert "customer_id" not in sly


def test_identity_lookup_failure_is_unavailable(mock):
    class Down(MockAdapter):
        def __init__(self, base):
            self.__dict__.update(base.__dict__)

        async def resolve_channel_identity(self, channel, identifier):
            raise TimeoutError()

    policy = {**load_policy(), "max_retries": 0, "retry_backoff_s": 0}
    sly = web(mock, "CUST-S01")
    assert run(ensure_identity(sly, Down(mock), policy))["identity_status"] == "unavailable"


# ---------------------------------------------------------------------------- intake

def test_case_is_opened_once_and_continued(mock):
    sly = web(mock, "CUST-S01")
    first = run(OpenCase().async_invoke({}, sly))
    assert first["created"] and first["identity_status"] == "verified" and sly["case_id"] == first["case_id"]
    second_turn = {**web(mock, "CUST-S01"), "case_id": first["case_id"]}  # gateway sends the case id back
    again = run(OpenCase().async_invoke({}, second_turn))
    assert again["case_id"] == first["case_id"] and not again["created"]


def test_forged_case_id_from_another_customer_is_not_reused(mock):
    other = web(mock, "CUST-S08")
    other_case = run(OpenCase().async_invoke({}, other))["case_id"]
    sly = {**web(mock, "CUST-S01"), "case_id": other_case}
    mine = run(OpenCase().async_invoke({}, sly))
    assert mine["created"] and mine["case_id"] != other_case


def test_two_needs_become_two_issues_and_restating_is_not_duplicated(mock):
    sly = web(mock, "CUST-S09")
    ids = start(sly, {"domain": "payments_billing", "kind": "duplicate_charge", "summary": "Charged twice."},
                {"domain": "returns_refunds", "kind": "refund_status", "summary": "Refund for red sweater",
                 "item_query": "red sweater"})
    assert len(ids) == 2
    again = run(AddIssues().async_invoke({"issues": [{"domain": "returns_refunds", "kind": "refund_status",
                                                      "summary": "Where is my refund", "item_query": "red sweater"}]},
                                         sly))
    assert [i["issue_id"] for i in again["issues"]] == [ids[1]]


def test_references_not_on_the_account_are_not_recorded(mock):
    sly = web(mock, "CUST-S19A")
    run(OpenCase().async_invoke({}, sly))
    added = run(AddIssues().async_invoke({"issues": [{"domain": "order_delivery", "kind": "where_is_order",
                                                      "summary": "Spouse's order", "order_id": "ORD-S19B-1"}]}, sly))
    assert "refs" not in added["issues"][0]  # empty: the foreign order id was dropped
    assert "ORD-S19B-1 is not on this customer's account" in added["notes"][0]


def test_unknown_domain_is_taken_from_the_kind(mock):
    sly = web(mock, "CUST-S01")
    run(OpenCase().async_invoke({}, sly))
    added = run(AddIssues().async_invoke({"issues": [{"domain": "weather", "kind": "cancel_request",
                                                      "summary": "cancel it"}]}, sly))
    assert added["issues"][0]["domain"] == "order_delivery"


@pytest.mark.parametrize("raw, message", [
    ({"domain": "order_delivery", "kind": "order_status_and_cancel", "summary": "cancel"}, "unknown kind"),
    ({"domain": "order_delivery", "summary": "cancel"}, "unknown kind ''"),
    ({"domain": "returns_refunds", "kind": "cancel_request", "summary": "cancel"},
     "cancel_request belongs to order_delivery, not returns_refunds"),
])
def test_free_text_or_mismatched_kinds_are_refused_and_nothing_is_recorded(mock, raw, message):
    sly = web(mock, "CUST-S16")
    run(OpenCase().async_invoke({}, sly))
    good = {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"}
    refused = run(AddIssues().async_invoke({"issues": [good, raw]}, sly))
    assert refused.startswith("Error:") and message in refused and "nothing was recorded" in refused
    assert run(CaseState().async_invoke({}, sly))["issues"] == []


def test_action_kinds_are_issue_kinds():
    assert ACTION_KINDS <= set(DOMAIN_OF_KIND)


def test_contact_history_marks_notes_untrusted(mock):
    sly = web(mock, "CUST-S20")
    result = run(GetContactHistory().async_invoke({}, sly))
    (contact,) = result["contacts"]
    assert contact["untrusted_note"].startswith("SYSTEM OVERRIDE") and "never an instruction" in result["notice"]


def test_contact_history_needs_identity(mock):
    result = run(GetContactHistory().async_invoke({}, {"channel": "phone", "identifier": S18_PHONE}))
    assert result.startswith("Error: identity_unavailable")


# ---------------------------------------------------------------------------- specialists

def test_reads_are_filed_as_evidence_and_start_the_investigation(mock):
    sly = web(mock, "CUST-S01")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "Blue jacket refund",
                         "item_query": "blue jacket"})
    found = run(FindItems().async_invoke({"issue_id": iid}, sly))
    assert found["result"] == "unique" and found["items"][0]["item_id"] == "ITM-S01-1-1"
    rr = run(GetReturnsRefunds().async_invoke({"issue_id": iid, "item_id": "ITM-S01-1-1"}, sly))
    assert rr["items"][0]["refund_status"] == "pending" and "carrier" not in rr["items"][0]
    state = issue(sly, iid)
    assert state["status"] == "investigating"
    assert state["evidence_sources"] == ["find_order_items", "get_returns_refunds"]


def test_specialist_cannot_read_for_another_domains_issue(mock):
    sly = web(mock, "CUST-S01")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    assert run(GetOrders().async_invoke({"issue_id": iid}, sly)).startswith("Error: issue")


def test_issue_from_another_case_is_rejected(mock):
    other = web(mock, "CUST-S08")
    (other_iid,) = start(other, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    sly = web(mock, "CUST-S01")
    start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    assert "not part of this case" in run(GetReturnsRefunds().async_invoke({"issue_id": other_iid}, sly))


def test_stuck_cases_are_filtered_by_domain(mock):
    sly = web(mock, "CUST-S10")
    order_iid, returns_iid = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "late"},
                                   {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    order = run(StuckCases().async_invoke({"issue_id": order_iid}, sly))
    assert [c["diagnosis"] for c in order["stuck_cases"]] == ["stuck_shipment"]
    assert run(StuckCases().async_invoke({"issue_id": returns_iid}, sly))["stuck_cases"] == []


def test_timeline_of_another_customers_order_is_not_found(mock):
    sly = web(mock, "CUST-S19A")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "order"})
    assert "not found" in run(OrderTimeline().async_invoke({"issue_id": iid, "order_id": "ORD-S19B-1"}, sly))


def test_timeline_uses_the_order_of_a_unique_item_match(mock):
    sly = web(mock, "CUST-S01")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "Blue jacket refund",
                         "item_query": "blue jacket"})
    assert "find_order_items first" in run(OrderTimeline().async_invoke({"issue_id": iid}, sly))
    run(FindItems().async_invoke({"issue_id": iid}, sly))
    timeline = run(OrderTimeline().async_invoke({"issue_id": iid}, sly))
    assert timeline["order_id"] == "ORD-S01-1"


def test_return_eligibility_outside_window_s14(mock):
    sly = web(mock, "CUST-S14")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "return_request", "summary": "return jacket"})
    checks = run(CheckReturnEligibility().async_invoke({"issue_id": iid, "item_id": "ITM-S14-1-1"}, sly))["checks"]
    assert checks["create_return"] == {"applicable": False, "reason": "outside_return_window", "amount": None}


def test_record_findings_accepts_only_own_domain_actions_and_own_targets(mock):
    sly = web(mock, "CUST-S10")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "parcel stuck"})
    wrong_domain = {"issue_id": iid, "conclusion": "x", "proposed_action": {"action": "initiate_refund",
                                                                            "target_id": "ITM-S10-1-1"}}
    assert "not an action for order_delivery" in run(RecordFindings().async_invoke(wrong_domain, sly))
    foreign = {"issue_id": iid, "conclusion": "x", "proposed_action": {"action": "open_carrier_trace",
                                                                       "target_id": "ORD-S02-1"}}
    assert "not on this customer's account" in run(RecordFindings().async_invoke(foreign, sly))
    ok = {"issue_id": iid, "conclusion": "No carrier scan for 6 days.",
          "proposed_action": {"action": "open_carrier_trace", "target_id": "ORD-S10-1"}}
    result = run(RecordFindings().async_invoke(ok, sly))
    assert result["status"] == "proposed" and result["proposal"]["target_id"] == "ORD-S10-1"


# A specialist closes an info-only answer itself (record_findings answered=true), saving the orchestrator a call;
# the same guard as update_issue applies, and a refused close keeps the findings.

def answered(sly: dict, iid: str, **extra) -> dict:
    return run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "the facts", "answered": True,
                                              **extra}, sly))


def test_specialist_closes_an_answered_question_in_the_same_step(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"})
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    result = answered(sly, iid)
    assert result["closed"] and result["status"] == "resolved_info_only"
    assert issue(sly, iid)["conclusions"] == ["the facts"]


def test_specialist_close_without_evidence_is_refused_but_findings_are_kept(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"})
    result = answered(sly, iid)
    assert not result["closed"] and "no evidence" in result["not_closed_because"]
    assert issue(sly, iid)["status"] == "investigating" and issue(sly, iid)["conclusions"] == ["the facts"]


def test_specialist_cannot_close_a_stuck_refund_s08(mock):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "where is my money",
                         "item_query": "running shoes"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    run(StuckCases().async_invoke({"issue_id": iid}, sly))
    result = answered(sly, iid)
    assert not result["closed"] and "stuck_refund on ITM-S08-1-1" in result["not_closed_because"]
    assert issue(sly, iid)["status"] == "investigating"


def test_specialist_cannot_close_an_action_request_s16(mock):
    sly = web(mock, "CUST-S16")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "cancel_request", "summary": "cancel my order"})
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    result = answered(sly, iid)
    assert not result["closed"] and "cancel_request" in result["not_closed_because"]


def test_specialist_cannot_close_while_proposing(mock):
    sly = web(mock, "CUST-S10")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "parcel stuck"})
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    result = answered(sly, iid, proposed_action={"action": "open_carrier_trace", "target_id": "ORD-S10-1"})
    assert not result["closed"] and result["status"] == "proposed"
    assert "orchestrator submits it" in result["not_closed_because"]


# ---------------------------------------------------------------------------- orchestrator

def test_answered_question_needs_tool_evidence_before_resolving(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"})
    refused = run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    assert "no evidence" in refused
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))["status"] == \
        "resolved_info_only"


def test_llm_cannot_set_action_or_handoff_statuses(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"})
    for status in ("queued_for_review", "executed_verified", "handed_off"):
        assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": status}, sly)).startswith("Error:")


def test_submit_needs_a_recorded_proposal(mock):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    assert "no proposed action" in run(SubmitAction().async_invoke({"issue_id": iid}, sly))


def ref(action: str, target: str) -> dict:
    """The reference the customer typed for the target (issue refs): an item id or an order id."""
    return {"item_id": target} if action in ITEM_ACTIONS else {"order_id": target}


def propose(sly: dict, iid: str, action: str, target: str) -> None:
    result = run(RecordFindings().async_invoke({"issue_id": iid, "conclusion": "evidence summary",
                                                "proposed_action": {"action": action, "target_id": target}}, sly))
    assert result["status"] == "proposed", result


@pytest.mark.parametrize("customer, domain, action, target, outcome, status", [
    ("CUST-S08", "returns_refunds", "initiate_refund", "ITM-S08-1-1", "queued_for_review", "queued_for_review"),
    ("CUST-S13", "returns_refunds", "retrigger_refund", "ITM-S13-1-1", "queued_for_approval", "queued_for_review"),
    ("CUST-S15", "returns_refunds", "initiate_refund", "ITM-S15-4-1", "escalated", "queued_for_review"),
    ("CUST-S16", "order_delivery", "cancel_order", "ORD-S16-1", "denied", "proposed"),
    ("CUST-S14", "returns_refunds", "create_return", "ITM-S14-1-1", "denied", "proposed"),
    ("CUST-S10", "order_delivery", "open_carrier_trace", "ORD-S10-1", "queued_for_review", "queued_for_review"),
])
def test_submit_maps_policy_outcomes_to_issue_status(mock, customer, domain, action, target, outcome, status):
    sly = web(mock, customer)
    kind = {"cancel_order": "cancel_request", "create_return": "return_request",
            "open_carrier_trace": "delivery_problem"}.get(action, "refund_status")
    (iid,) = start(sly, {"domain": domain, "kind": kind, "summary": "customer request", **ref(action, target)})
    propose(sly, iid, action, target)
    result = run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": True}, sly))
    assert (result["outcome"], result["issue_status"]) == (outcome, status), result
    assert issue(sly, iid)["status"] == status


def test_stuck_refund_cannot_be_closed_as_information_only_s08(mock):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "where is my money",
                         "item_query": "running shoes"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    run(StuckCases().async_invoke({"issue_id": iid}, sly))
    refused = run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    assert "cannot be closed as information only" in refused and "stuck_refund on ITM-S08-1-1" in refused


def test_stuck_case_on_another_item_does_not_block_closing(mock):
    sly = web(mock, "CUST-S15")  # the stuck refund is on the shirt (ITM-S15-4-1); the customer asks about the jacket
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "jacket refund",
                         "item_id": "ITM-S15-3-1"})
    run(StuckCases().async_invoke({"issue_id": iid}, sly))
    assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))["status"] == \
        "resolved_info_only"


def test_action_request_closes_only_after_a_recorded_decision_s16(mock):
    sly = web(mock, "CUST-S16")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "cancel_request", "summary": "cancel my order"})
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    refused = run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    assert "asked for something to be done (cancel_request)" in refused
    propose(sly, iid, "cancel_order", "ORD-S16-1")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "denied"
    assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))["status"] == \
        "resolved_info_only"


def close_info_only(sly: dict, iid: str, note: str = "") -> dict | str:
    return run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only", "note": note}, sly))


def test_a_proposed_action_cannot_be_dropped_by_closing_as_information_only(mock):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "where is my money"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    refused = close_info_only(sly, iid)
    assert "initiate_refund on ITM-S08-1-1 was proposed but not submitted" in refused


def test_a_pending_confirmation_closes_only_after_the_customer_declined(mock):
    sly = web(mock, "CUST-S09")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "red sweater refund"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    propose(sly, iid, "reissue_refund_store_credit", "ITM-S09-1-1")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "awaiting_confirmation"
    assert "has not answered yet" in close_info_only(sly, iid, "customer declined")  # same turn: no answer yet
    next_turn = {**web(mock, "CUST-S09"), "case_id": sly["case_id"]}
    assert "with a note saying so" in close_info_only(next_turn, iid)
    assert close_info_only(next_turn, iid, "Customer prefers to wait for the card refund.")["status"] == \
        "resolved_info_only"
    assert issue(sly, iid)["outcome"]["outcome"] == "declined_by_customer"


def test_a_person_can_be_asked_for_after_the_case_was_completed_m3(mock):
    sly = web(mock, "CUST-S01")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "jacket refund"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    assert close_info_only(sly, iid)["status"] == "resolved_info_only"
    assert run(CaseState().async_invoke({"op": "complete"}, sly))["outcome"] == "resolved"
    next_turn = {**web(mock, "CUST-S01"), "case_id": sly["case_id"]}  # "Actually, I'd like to talk to a person"
    handed = run(HandoffToHuman().async_invoke({"reason": "customer_requested", "issue_ids": [iid],
                                                "summary": "Wants a person after the refund answer."}, next_turn))
    assert handed["case_id"] == sly["case_id"] and handed["issues_handed_off"] != [iid], handed
    view = run(CaseState().async_invoke({}, next_turn))
    assert view["case_status"] == "closed_with_handoff" and view["handoffs"][0]["reason"] == "customer_requested"
    assert [i["status"] for i in view["issues"]] == ["resolved_info_only", "handed_off"]


# ---------------------------------------------------------------------------- F4: requests are decided in code

def test_a_request_with_no_proposal_is_submitted_as_asked_and_refused_s05(mock):
    sly = web(mock, "CUST-S05")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "return_request", "summary": "return blue jacket",
                         "item_query": "blue jacket"})
    assert run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue jacket"}, sly))["result"] == "ambiguous"
    unknown = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert "not known which item" in unknown  # never guesses between two jackets
    later = {**web(mock, "CUST-S05"), "case_id": sly["case_id"]}  # "The puffer one."
    run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue puffer jacket"}, later))
    assert "Call submit_action" in close_info_only(later, iid)  # the guard points to the code path
    result = run(SubmitAction().async_invoke({"issue_id": iid}, later))
    assert (result["outcome"], result["reason"]) == ("denied", "outside_return_window"), result
    assert issue(sly, iid)["proposal"] == {"action": "create_return", "target_id": "ITM-S05-1-1"}
    assert close_info_only(later, iid)["status"] == "resolved_info_only"  # the refusal is the recorded decision


def test_an_allowed_request_still_waits_for_confirmation(mock):
    sly = web(mock, "CUST-S05")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "return_request", "summary": "return rain shell",
                         "item_query": "blue rain shell"})
    run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue rain shell"}, sly))
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "awaiting_confirmation"


def test_a_cancel_request_targets_the_order_s16(mock):
    sly = web(mock, "CUST-S16")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "cancel_request", "summary": "cancel my order",
                         "order_id": "ORD-S16-1"})
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["action"], result["target_id"]) == ("denied", "cancel_order", "ORD-S16-1")


# ---------------------------------------------------------------------------- G4: the gateway's end-of-turn check

LATER_TURN = "2999-01-01T00:00:00Z"  # a turn that started after every update made in the test


def test_an_answered_issue_left_open_is_handed_off_s06_s07(mock):
    sly = web(mock, "CUST-S01")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "jacket refund"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))  # answered, but update_issue never called
    checked = tool_support.case_store().end_of_turn(sly["case_id"], since)
    assert checked["left_open"] == [iid] and checked["completed"]
    view = run(CaseState().async_invoke({}, sly))
    assert view["case_status"] == "closed_with_handoff" and view["handoffs"][0]["reason"] == "left_open"


def test_closed_issues_complete_the_case_even_if_the_agent_forgot(mock):
    sly = web(mock, "CUST-S01")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "jacket refund"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    close_info_only(sly, iid)
    checked = tool_support.case_store().end_of_turn(sly["case_id"], since)
    assert checked == {"left_open": [], "handoff_id": None, "completed": True}
    assert run(CaseState().async_invoke({}, sly))["case_status"] == "resolved"


def test_a_question_to_the_customer_keeps_the_issue_open_for_one_turn_s05(mock):
    sly = web(mock, "CUST-S05")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "blue jacket refund",
                         "item_query": "blue jacket"})
    asked = run(UpdateIssue().async_invoke({"issue_id": iid, "status": "awaiting_customer"}, sly))
    assert asked["status"] == "awaiting_customer"
    store = tool_support.case_store()
    assert store.end_of_turn(sly["case_id"], since) == {"left_open": [], "handoff_id": None, "completed": False}
    # the customer answers, but the agents never come back to the issue: now it is left open
    assert store.end_of_turn(sly["case_id"], LATER_TURN)["left_open"] == [iid]


def test_a_pending_confirmation_keeps_the_issue_open_for_one_turn_s09(mock):
    sly = web(mock, "CUST-S09")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "red sweater refund"})
    propose(sly, iid, "reissue_refund_store_credit", "ITM-S09-1-1")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "awaiting_confirmation"
    store = tool_support.case_store()
    assert store.end_of_turn(sly["case_id"], since)["left_open"] == []
    next_turn = {**web(mock, "CUST-S09"), "case_id": sly["case_id"], "customer_message": "Yes, please."}
    assert run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": True}, next_turn))[
        "issue_status"] == "queued_for_review"
    assert store.end_of_turn(sly["case_id"], LATER_TURN)["completed"]


def test_confirmation_flow_across_turns_s09(mock):
    sly = web(mock, "CUST-S09")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "red sweater refund"})
    propose(sly, iid, "reissue_refund_store_credit", "ITM-S09-1-1")
    first = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (first["outcome"], first["issue_status"]) == ("awaiting_confirmation", "proposed")
    next_turn = {**web(mock, "CUST-S09"), "case_id": sly["case_id"], "customer_message": "Yes."}  # "yes" next turn
    second = run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": True}, next_turn))
    assert (second["outcome"], second["issue_status"]) == ("queued_for_review", "queued_for_review")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, next_turn)).startswith("Error: issue")


def test_weak_phone_session_cannot_write(mock):
    sly = {"customer_id": "CUST-S08", "channel": "phone", "auth_level": "weak"}
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    result = run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": True}, sly))
    assert (result["outcome"], result["reason"]) == ("denied", "step_up_required")


def test_live_execution_is_verified_by_read_back(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    sly = web(mock, "CUST-S12")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "belt refund"})
    propose(sly, iid, "retrigger_refund", "ITM-S12-1-1")
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["issue_status"], result["amount"]) == ("executed", "executed_verified", 36.0)


def test_unconfirmed_execution_is_never_reported_as_done(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    monkeypatch.setattr(ActionStore, "executed_receipt", lambda self, reference: None)
    sly = web(mock, "CUST-S12")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "belt refund"})
    propose(sly, iid, "retrigger_refund", "ITM-S12-1-1")
    result = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (result["outcome"], result["issue_status"]) == ("executed_unverified", "handed_off")
    assert issue(sly, iid)["status"] == "handed_off"


def test_handoff_packet_comes_from_the_case_record(mock):
    sly = web(mock, "CUST-S09")
    pay, ret = start(sly, {"domain": "payments_billing", "kind": "duplicate_charge", "summary": "Charged twice"},
                     {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    run(GetReturnsRefunds().async_invoke({"issue_id": ret}, sly))
    result = run(HandoffToHuman().async_invoke({"issue_ids": [pay], "reason": "not_supported",
                                                "summary": "Customer reports a double charge on the sweater order."},
                                               sly))
    assert result["issues_handed_off"] == [pay]
    packet = CaseStore().handoff_packet(result["handoff_id"])
    assert packet["customer_id"] == "CUST-S09" and packet["channel_identifier"] is None
    assert packet["issues"][0]["kind"] == "duplicate_charge" and packet["policy_version"]
    assert issue(sly, pay)["status"] == "handed_off" and issue(sly, ret)["status"] == "investigating"


def test_identity_failure_handoff_s18(mock):
    sly = {"channel": "phone", "identifier": S18_PHONE}
    opened = run(OpenCase().async_invoke({}, sly))
    assert not opened["can_read_account"] and "do not say how many" in opened["message_hint"]
    result = run(HandoffToHuman().async_invoke({"reason": "identity_failed",
                                                "summary": "Caller wants order status; phone matches no single "
                                                           "account."}, sly))
    assert "share no account details" in result["message_hint"]
    packet = CaseStore().handoff_packet(result["handoff_id"])
    assert packet["customer_id"] is None and packet["channel_identifier"] == S18_PHONE
    assert packet["issues"][0]["domain"] == "account_identity"


def test_case_cannot_complete_while_an_issue_is_open(mock):
    sly = web(mock, "CUST-S09")
    pay, ret = start(sly, {"domain": "payments_billing", "kind": "duplicate_charge", "summary": "Charged twice"},
                     {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund"})
    run(HandoffToHuman().async_invoke({"issue_ids": [pay], "reason": "not_supported", "summary": "x"}, sly))
    refused = run(CaseState().async_invoke({"op": "complete"}, sly))
    assert refused.startswith("Error: case cannot be completed") and ret in refused
    run(GetReturnsRefunds().async_invoke({"issue_id": ret}, sly))
    run(UpdateIssue().async_invoke({"issue_id": ret, "status": "resolved_info_only"}, sly))
    done = run(CaseState().async_invoke({"op": "complete"}, sly))
    assert done["outcome"] == "closed_with_handoff" and done["resolved"] == [ret] and done["with_people"] == [pay]


def test_case_events_are_an_audit_trail(mock):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "where is it"})
    run(GetOrders().async_invoke({"issue_id": iid}, sly))
    run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    assert run(CaseState().async_invoke({"op": "complete"}, sly))["outcome"] == "resolved"
    kinds = [e["kind"] for e in CaseStore().events(sly["case_id"])]
    assert kinds == ["case_opened", "issue_added", "status", "status", "case_completed"]


def test_case_store_rejects_terminal_transitions(tmp_path):
    store = CaseStore(tmp_path / "c.db")
    case, _ = store.open_case(customer_id="C1", channel="web_session", auth_level="strong", identity_status="verified",
                              policy_version="v", reference_date="2026-10-16")
    i = store.add_issue(case["case_id"], domain="other", kind="x", summary="y", refs={})
    store.set_status(case["case_id"], i["issue_id"], "handed_off", by="system")
    with pytest.raises(CaseError):
        store.set_status(case["case_id"], i["issue_id"], "investigating", by="system")


# ---------------------------------------------------------------------------- network definition

def test_every_coded_tool_in_the_network_imports():
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    tools = conf["tools"]
    names = {t["name"] for t in tools}
    for tool in tools:
        for sub in tool.get("tools", []):
            assert sub in names, f"{tool['name']} references unknown tool {sub}"
        if "class" in tool:
            module, cls = tool["class"].rsplit(".", 1)
            loaded = getattr(importlib.import_module(f"coded_tools.{module}"), cls)
            assert issubclass(loaded, CodedTool), tool["class"]


def test_every_function_declares_at_least_one_argument():
    # Neuro SAN refuses to call a function whose parameters have no properties (ToolSpecError at run time);
    # ns validate does not check this, the scripted smoke run found it.
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    for tool in conf["tools"][1:]:
        props = tool["function"].get("parameters", {}).get("properties", {})
        assert len(props) >= 1, f"{tool['name']} declares no arguments"


def test_only_case_id_is_returned_to_the_gateway():
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    front = conf["tools"][0]
    assert dict(front["allow"]["to_upstream"]["sly_data"]) == {"case_id": True}
    assert all("allow" not in t for t in conf["tools"][1:])


def test_policy_action_domains_cover_every_action():
    from coded_tools.customer_service.policy import ACTIONS

    domains = load_policy()["action_domains"]
    assigned = [a for actions in domains.values() for a in actions]
    assert sorted(assigned) == sorted(ACTIONS) and len(assigned) == len(set(assigned))


# ---------------------------------------------------------------------------- D2: the target is pinned by the records

def test_a_description_only_target_must_be_the_unique_match_s05(mock):
    sly = web(mock, "CUST-S05")  # two blue jackets on the account
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "return_request", "summary": "return blue jacket",
                         "item_query": "blue jacket"})
    assert run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue jacket"}, sly))["result"] == "ambiguous"
    propose(sly, iid, "create_return", "ITM-S05-1-1")  # the specialist picked one of the two itself
    blocked = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert (blocked["outcome"], blocked["reason"]) == ("blocked", "target_unclear"), blocked
    assert blocked["issue_status"] == "proposed" and "handoff_id" not in blocked  # stays with the agents: ask
    assert not ActionStore().actions()
    later = {**web(mock, "CUST-S05"), "case_id": sly["case_id"]}  # "The puffer one."
    assert run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue puffer jacket"}, later))[
        "result"] == "unique"
    propose(later, iid, "create_return", "ITM-S05-1-1")
    assert run(SubmitAction().async_invoke({"issue_id": iid}, later))["reason"] != "target_unclear"


def test_a_description_only_target_other_than_the_match_is_refused(mock):
    sly = web(mock, "CUST-S05")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "return_request", "summary": "return rain shell",
                         "item_query": "blue rain shell"})
    found = run(FindItems().async_invoke({"issue_id": iid, "item_query": "blue rain shell"}, sly))
    assert found["result"] == "unique" and found["items"][0]["item_id"] != "ITM-S05-1-1"
    propose(sly, iid, "create_return", "ITM-S05-1-1")  # the puffer jacket, not the shell the customer meant
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["reason"] == "target_unclear"


def test_identified_targets_rules():
    from coded_tools.customer_service.action_layer import identified_targets

    rows = [{"item_id": "I1", "order_id": "O1"}, {"item_id": "I2", "order_id": "O2"}]
    unique = {"source": "find_order_items", "data": {"result": "unique", "items": [{"item_id": "I2", "order_id": "O2"}]}}
    ambiguous = {"source": "find_order_items", "data": {"result": "ambiguous", "items": []}}
    stuck = {"source": "get_stuck_cases", "data": {"stuck_cases": [{"item_id": "I1", "order_id": "O1"}]}}
    assert identified_targets({"findings": [unique]}, rows) == {"I2", "O2"}
    assert identified_targets({"findings": [unique, ambiguous]}, rows) == set()  # the latest search decides
    assert identified_targets({"findings": [stuck]}, rows) == {"I1", "O1"}  # the only stuck case
    assert identified_targets({"findings": []}, rows[:1]) == {"I1", "O1"}  # the only item on the account
    assert identified_targets({"findings": []}, rows) == set()
