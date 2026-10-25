"""
Phase 6 tests (no LLM): the human review loop (queue, approve, reject, resolve, notifications), the review
console API, and the tracing attribute allow-list.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from coded_tools.customer_service.action_service import request_action
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.action_tools import EscalateToHuman
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.orchestrator_tools import HandoffToHuman
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.orchestrator_tools import UpdateIssue
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.review_service import ReviewError
from coded_tools.customer_service.review_service import ReviewService
from coded_tools.customer_service.tracing import safe_attributes
from coded_tools.customer_service.tracing import span
from review_console.app import app
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import issue
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import propose
from test_multi_agent_foundation import ref
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

RET = {"domain": "returns_refunds", "kind": "refund_status", "summary": "Where is my refund?"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def svc(mock) -> ReviewService:
    return ReviewService(ActionStore(), CaseStore(), mock, load_policy())


def queued(mock, customer, action, target, domain="returns_refunds"):
    """Drives the multi-agent tools to put an action in the review queue; returns (sly, issue_id, result)."""
    sly = web(mock, customer)
    kind = "duplicate_charge" if domain == "payments_billing" else RET["kind"]
    (iid,) = start(sly, {**RET, "domain": domain, "kind": kind, **ref(action, target)})
    propose(sly, iid, action, target)
    result = run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": True}, sly))
    return sly, iid, result


def review_id_for(svc, reference):
    return next(i["review_id"] for i in svc.queue() if i.get("reference") == reference)


# ---------------------------------------------------------------------------- approve

def test_approval_above_limit_executes_verifies_and_notifies_s13(mock, svc):
    sly, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    assert result["outcome"] == "queued_for_approval"
    rid = review_id_for(svc, result["reference"])
    item = svc.detail(rid)
    assert (item["type"], item["amount"], item["support_issue_id"]) == ("action_review", 289.0, iid)
    assert item["case"]["case_id"] == sly["case_id"]

    approved = run(svc.approve(rid, reviewer="Sam Patel", note="Checked with the warehouse"))
    assert (approved["outcome"], approved["amount"]) == ("executed", 289.0)
    assert issue(sly, iid)["status"] == "executed_verified"
    assert rid not in [i["review_id"] for i in svc.queue()]
    (note,) = svc.outbox()
    assert "289.00" in note["message"] and result["reference"] in note["message"]
    audit = [r for r in svc.actions.read("decisions.jsonl") if r["outcome"] == "approved_executed"]
    assert audit[0]["reviewer"] == "Sam Patel" and audit[0]["facts"]["original_decision"] == "NEEDS_APPROVAL"
    with pytest.raises(ReviewError, match="already approved"):
        run(svc.approve(rid, reviewer="Sam Patel"))


def test_approved_action_is_not_repeated_by_a_later_request(mock, svc):
    _, _, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    run(svc.approve(review_id_for(svc, result["reference"]), reviewer="Sam"))
    again = run(request_action(mock, ActionStore(), load_policy(), customer_id="CUST-S13", channel="web_session",
                               auth_level="strong", action="retrigger_refund", target_id="ITM-S13-1-1"))
    assert (again["outcome"], again["previous_outcome"]) == ("already_handled", "executed")
    assert len(svc.actions.read("executed.jsonl")) == 1


def test_shadow_mode_review_is_the_path_to_execution(mock, svc):
    _, iid, result = queued(mock, "CUST-S08", "initiate_refund", "ITM-S08-1-1")
    assert result["outcome"] == "queued_for_review"  # shadow mode: an allowed action waits for a person
    rid = review_id_for(svc, result["reference"])
    assert svc.item(rid)["kind"] == "shadow_review"
    assert run(svc.approve(rid, reviewer="Sam"))["outcome"] == "executed"


def test_specialist_review_can_be_approved_without_revealing_why(mock, svc):
    _, _, result = queued(mock, "CUST-S15", "initiate_refund", "ITM-S15-4-1")
    rid = review_id_for(svc, result["reference"])
    item = svc.item(rid)
    assert item["kind"] == "specialist_review" and "fraud" not in json.dumps(item).lower()
    assert run(svc.approve(rid, reviewer="Risk team"))["outcome"] == "executed"


def test_approval_rechecks_fresh_data(mock, svc, monkeypatch):
    _, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    rid = review_id_for(svc, result["reference"])
    original = MockAdapter.get_customer_context

    async def refund_completed(self, customer_id):
        return [{**r, "refund_status": "completed"} for r in await original(self, customer_id)]

    monkeypatch.setattr(MockAdapter, "get_customer_context", refund_completed)
    outcome = run(svc.approve(rid, reviewer="Sam"))
    assert (outcome["outcome"], outcome["reason"]) == ("not_possible", "refund_not_stuck")
    assert svc.item(rid)["status"] == "open" and svc.actions.read("executed.jsonl") == []


def test_lost_execution_on_approval_is_not_reported_done(mock, svc, monkeypatch):
    _, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    monkeypatch.setenv("SYSTEM_OF_RECORD_FAULT", "drop_receipt")
    outcome = run(svc.approve(review_id_for(svc, result["reference"]), reviewer="Sam"))
    assert outcome["outcome"] == "executed_unverified" and svc.outbox() == []


def test_reviewer_name_is_required(mock, svc):
    _, _, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    with pytest.raises(ReviewError, match="reviewer name"):
        run(svc.approve(review_id_for(svc, result["reference"]), reviewer="  "))


# ---------------------------------------------------------------------------- reject and resolve

def test_reject_closes_the_issue_and_queues_a_message(mock, svc):
    sly, iid, result = queued(mock, "CUST-S03", "open_billing_review", "ORD-S03-1", domain="payments_billing")
    rid = review_id_for(svc, result["reference"])
    with pytest.raises(ReviewError, match="reason"):
        svc.reject(rid, reviewer="Billing", reason="")
    svc.reject(rid, reviewer="Billing", reason="Processor shows one charge",
               customer_message="We checked with our payment provider: there was only one charge.")
    assert issue(sly, iid)["status"] == "review_rejected"
    assert svc.outbox()[0]["message"].startswith("We checked with our payment provider")
    assert svc.item(rid)["status"] == "rejected"


def test_handoff_is_resolved_not_approved(mock, svc):
    sly = web(mock, "CUST-S02")
    (iid,) = start(sly, {"domain": "other", "kind": "human_requested", "summary": "Wants a person"})
    handed = run(HandoffToHuman().async_invoke({"reason": "customer_requested", "summary": "Wants to talk"}, sly))
    rid = handed["handoff_id"]
    assert svc.item(rid)["type"] == "handoff" and svc.detail(rid)["packet"]["reason"] == "customer_requested"
    with pytest.raises(ReviewError, match="use resolve"):
        run(svc.approve(rid, reviewer="Sam"))
    svc.resolve(rid, reviewer="Sam", note="Called the customer back", customer_message="Thanks for talking to us.")
    assert svc.item(rid)["status"] == "resolved" and issue(sly, iid)["status"] == "handed_off"
    with pytest.raises(ReviewError, match="already resolved"):
        svc.resolve(rid, reviewer="Sam", note="again")


def test_single_agent_customer_requests_are_in_the_queue_too(mock, svc):
    result = run(EscalateToHuman().async_invoke({"reason": "customer_requested", "summary": "Wants a call"},
                                                {"customer_id": "CUST-S01"}))
    item = svc.item(result["case_id"])
    assert item["type"] == "customer_request"
    svc.resolve(result["case_id"], reviewer="Sam", note="Called back")
    assert result["case_id"] not in [i["review_id"] for i in svc.queue()]


def test_queued_issue_can_only_be_closed_by_review(mock):
    sly, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    with pytest.raises(CaseError, match="waiting for a person"):
        CaseStore().set_status(sly["case_id"], iid, "executed_verified", by="system")
    assert run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly)).startswith("Error")


# ---------------------------------------------------------------------------- console API

def test_console_api_round_trip(mock):
    _, _, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    client = TestClient(app)
    assert "Review queue" in client.get("/").text
    (row,) = client.get("/api/queue").json()
    assert row["reference"] == result["reference"]
    rid = row["review_id"]
    assert client.post(f"/api/items/{rid}/approve", json={"reviewer": ""}).status_code == 400
    assert client.post(f"/api/items/{rid}/approve", json={"reviewer": "Sam"}).json()["outcome"] == "executed"
    assert client.get("/api/queue").json() == []
    assert client.get("/api/queue?all=true").json()[0]["status"] == "approved"
    assert client.get("/api/items/NOPE").status_code == 404
    assert len(client.get("/api/outbox").json()) == 1


# ---------------------------------------------------------------------------- tracing

def test_tracing_records_only_allow_listed_scalars():
    attrs = safe_attributes({"case_id": "CASE-1", "outcome": "executed", "customer_id": "CUST-S01",
                             "risk_flags": "fraud_review", "summary": "free text", "amount": 12.5, "facts": {"a": 1}})
    assert attrs == {"cs.case_id": "CASE-1", "cs.outcome": "executed"}


def test_spans_are_safe_without_a_tracing_backend():
    with span("test", case_id="CASE-1", customer_id="CUST-1") as current:
        current.set_attribute("cs.outcome", "ok")  # no-op without an SDK; must not raise
