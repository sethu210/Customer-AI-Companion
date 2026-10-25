"""
Multi-agent Phase 4 tests (no LLM): the Controlled Action & Verification Layer, the Policy agent's dry run,
the reference guard, issue authorisation, idempotency across issues, and failure handling via fault injection.
"""

import asyncio
import json

import pytest
from pyhocon import ConfigFactory

import coded_tools.customer_service.data_adapter as data_adapter
import coded_tools.customer_service.tool_support as tool_support
from coded_tools.customer_service import action_layer
from coded_tools.customer_service import confirmation
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.fault_injection import FaultInjectingAdapter
from coded_tools.customer_service.fault_injection import parse_faults
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.policy import DENY
from coded_tools.customer_service.policy import ESCALATE
from coded_tools.customer_service.policy import NEEDS_APPROVAL
from coded_tools.customer_service.policy import PolicyDecision
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.policy import recommendation
from coded_tools.customer_service.policy_tools import EvaluatePolicy
from coded_tools.customer_service.policy_tools import GetPolicyConfig
from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
from test_multi_agent_foundation import NETWORK
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


@pytest.fixture(autouse=True)
def fast_policy(monkeypatch):
    """No retry back-off in tests, and a fresh reference-guard cache for each test."""
    real = load_policy
    monkeypatch.setattr(tool_support, "load_policy", lambda: {**real(), "retry_backoff_s": 0.0})
    action_layer._GUARD_CACHE.clear()


def with_faults(monkeypatch, mock, spec: str) -> FaultInjectingAdapter:
    adapter = FaultInjectingAdapter(mock, parse_faults(spec))
    monkeypatch.setattr(data_adapter, "_ADAPTER", adapter)
    return adapter


def submit(sly, iid, confirmed=False):
    return run(SubmitAction().async_invoke({"issue_id": iid, "customer_confirmed": confirmed}, sly))


def evaluate(sly, iid, confirmed=False):
    return run(EvaluatePolicy().async_invoke({"issue_id": iid, "customer_confirmed": confirmed}, sly))


def store_lines(name: str) -> list[dict]:
    path = ActionStore().dir / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# ---------------------------------------------------------------------------- reference guard (step 4)

def test_policy_and_data_disagreeing_blocks_every_change(mock, monkeypatch):
    real = load_policy
    monkeypatch.setattr(tool_support, "load_policy",
                        lambda: {**real(), "reference_date": "2026-11-01", "version": "other", "retry_backoff_s": 0})
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    result = submit(sly, iid)
    assert (result["outcome"], result["reason"], result["issue_status"]) == \
        ("blocked", "policy_data_mismatch", "handed_off")
    assert store_lines("actions.jsonl") == [] and store_lines("cases.jsonl") == []
    assert [r["outcome"] for r in store_lines("decisions.jsonl")] == ["blocked"]
    packet = CaseStore().handoff_packet(result["handoff_id"])
    assert "reference_date" in packet["detail"]


def test_unreadable_data_config_also_blocks(mock, monkeypatch):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    with_faults(monkeypatch, mock, "get_reference_config:permission")
    assert submit(sly, iid)["reason"] == "policy_data_mismatch"


# ---------------------------------------------------------------------------- issue authorisation (step 4)

def test_target_must_belong_to_the_issue(mock):
    sly = web(mock, "CUST-S05")
    jackets = [r["item_id"] for r in run(mock.find_order_items("CUST-S05", "blue jacket"))]
    assert len(jackets) == 2
    (iid,) = start(sly, {**RET, "kind": "return_request", "item_id": jackets[0]})
    propose(sly, iid, "create_return", jackets[1])  # a real item of this customer, but not this issue's
    result = submit(sly, iid, confirmed=True)
    assert (result["outcome"], result["reason"], result["issue_status"]) == \
        ("blocked", "not_authorised_for_issue", "proposed")
    assert "handoff_id" not in result and store_lines("actions.jsonl") == []


def test_order_reference_admits_items_of_that_order(mock):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, {**RET, "order_id": "ORD-S08-1"})
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    assert submit(sly, iid)["outcome"] == "queued_for_review"


def test_domain_is_rechecked_even_if_a_proposal_bypassed_record_findings(mock):
    sly = web(mock, "CUST-S10")
    (iid,) = start(sly, {"domain": "order_delivery", "kind": "where_is_order", "summary": "late"})
    CaseStore().set_status(sly["case_id"], iid, "proposed", by="system",
                           proposal={"action": "initiate_refund", "target_id": "ITM-S10-1-1"})
    result = submit(sly, iid)
    assert (result["outcome"], result["reason"]) == ("blocked", "not_authorised_for_issue")


# ---------------------------------------------------------------------------- idempotency (step 5)

def test_two_issues_cannot_repeat_the_same_refund(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    sly = web(mock, "CUST-S12")
    first, second = start(sly, RET, {**RET, "summary": "Belt money still missing", "item_query": "belt"})
    propose(sly, first, "retrigger_refund", "ITM-S12-1-1")
    propose(sly, second, "retrigger_refund", "ITM-S12-1-1")
    one, two = submit(sly, first), submit(sly, second)
    assert (one["outcome"], one["issue_status"]) == ("executed", "executed_verified")
    assert (two["outcome"], two["issue_status"]) == ("already_handled", "executed_verified")
    assert two["reference"] == one["reference"] and len(store_lines("executed.jsonl")) == 1


# ---------------------------------------------------------------------------- verification and audit (7, 8)

def test_lost_execution_is_caught_by_read_back(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    monkeypatch.setenv("SYSTEM_OF_RECORD_FAULT", "drop_receipt")
    sly = web(mock, "CUST-S12")
    (iid,) = start(sly, RET)
    propose(sly, iid, "retrigger_refund", "ITM-S12-1-1")
    result = submit(sly, iid)
    assert (result["outcome"], result["issue_status"]) == ("executed_unverified", "handed_off")
    assert "Do not say it was done" in result["message_hint"]
    view = CaseStore().case_view(sly["case_id"])
    assert view["handoffs"][0]["reason"] == "unverified_outcome"


def test_audit_links_every_record_to_case_and_issue(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, RET)
    propose(sly, iid, "retrigger_refund", "ITM-S13-1-1")
    assert submit(sly, iid)["outcome"] == "queued_for_approval"
    (decision,) = store_lines("decisions.jsonl")
    assert (decision["case_id"], decision["issue_id"]) == (sly["case_id"], iid)
    assert decision["facts"]["policy_version"]
    (action,) = store_lines("actions.jsonl")
    assert (action["support_case_id"], action["support_issue_id"]) == (sly["case_id"], iid)
    (review,) = store_lines("cases.jsonl")
    assert review["proposal"]["support_issue_id"] == iid


# ---------------------------------------------------------------------------- failures (bounded retries)

def test_transient_failure_recovers_within_retries(mock, monkeypatch):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    adapter = with_faults(monkeypatch, mock, "get_stuck_cases:timeout:1")
    assert submit(sly, iid)["outcome"] == "queued_for_review"
    assert adapter.calls["get_stuck_cases"] == 2


def test_backend_down_blocks_and_hands_over(mock, monkeypatch):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    adapter = with_faults(monkeypatch, mock, "get_policy_signals:error")
    result = submit(sly, iid)
    assert (result["outcome"], result["reason"], result["issue_status"]) == \
        ("blocked", "data_unavailable", "handed_off")
    assert adapter.calls["get_policy_signals"] == 3  # 1 + max_retries, then stop
    assert store_lines("actions.jsonl") == []


def test_permanent_read_failure_is_not_retried_and_not_guessed(mock, monkeypatch):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    adapter = with_faults(monkeypatch, mock, "get_customer_context:broken")
    result = run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    assert result.startswith("Error: customer_data_unavailable (backend_error)") and "Do not guess" in result
    assert adapter.calls["get_customer_context"] == 1
    assert issue(sly, iid)["evidence_sources"] == []


def test_fault_spec_parsing():
    assert parse_faults("*:error, get_stuck_cases:timeout:2") == {"*": ["error", -1],
                                                                   "get_stuck_cases": ["timeout", 2]}
    with pytest.raises(ValueError):
        parse_faults("get_stuck_cases:explode")


def test_factory_wraps_backend_when_faults_configured(monkeypatch):
    monkeypatch.setattr(data_adapter, "_ADAPTER", None)
    monkeypatch.setenv("CUSTOMER_DATA_BACKEND", "mock")
    monkeypatch.setenv("CUSTOMER_DATA_FAULTS", "*:slow")
    adapter = data_adapter.get_adapter()
    assert isinstance(adapter, FaultInjectingAdapter) and adapter.name == "mock+faults"


# ---------------------------------------------------------------------------- policy agent dry run

@pytest.mark.parametrize("customer, action, target, confirmed, rec, kind, missing", [
    ("CUST-S09", "reissue_refund_store_credit", "ITM-S09-1-1", False, "NEEDS_MORE_INFORMATION", None,
     "customer_confirmation"),
    ("CUST-S09", "reissue_refund_store_credit", "ITM-S09-1-1", True, "ALLOW", None, None),
    ("CUST-S13", "retrigger_refund", "ITM-S13-1-1", False, "HUMAN_APPROVAL_REQUIRED", "approval", None),
    ("CUST-S15", "initiate_refund", "ITM-S15-4-1", False, "HUMAN_APPROVAL_REQUIRED", "specialist_review", None),
    ("CUST-S14", "create_return", "ITM-S14-1-1", True, "DENY", None, None),
    ("CUST-S03", "open_billing_review", "ORD-S03-1", False, "HUMAN_APPROVAL_REQUIRED", "billing_review", None),
])
def test_evaluate_recommendations(mock, customer, action, target, confirmed, rec, kind, missing):
    sly = web(mock, customer)
    billing = action == "open_billing_review"
    (iid,) = start(sly, {**RET, **({"domain": "payments_billing", "kind": "duplicate_charge"} if billing else {}),
                         **ref(action, target)})
    propose(sly, iid, action, target)
    if confirmed:  # a confirmation only counts in the turn after the customer was asked (G1)
        evaluate(sly, iid)
        sly = {**web(mock, customer), "case_id": sly["case_id"], "customer_message": "Yes."}
    result = evaluate(sly, iid, confirmed)
    assert (result["recommendation"], result["kind"], result["missing"]) == (rec, kind, missing), result
    text = json.dumps(result).lower()
    assert "fraud" not in text and "risk" not in text and "chargeback" not in text


# ---------------------------------------------------------------------------- G1: confirmation is checked in code

def s09_proposed(mock) -> tuple[dict, str]:
    sly = web(mock, "CUST-S09")
    (iid,) = start(sly, RET)
    propose(sly, iid, "reissue_refund_store_credit", "ITM-S09-1-1")
    return sly, iid


def next_turn(mock, sly: dict) -> dict:
    """The gateway's next request: fresh sly_data with only the case_id carried over."""
    return {**web(mock, "CUST-S09"), "case_id": sly["case_id"], "customer_message": "Yes, please."}


def test_confirmation_without_asking_first_is_not_accepted(mock):
    sly, iid = s09_proposed(mock)
    result = submit(sly, iid, confirmed=True)
    assert (result["outcome"], result["issue_status"]) == ("awaiting_confirmation", "proposed")
    assert result["confirmation_not_accepted"] == "the customer was not asked to confirm this action yet"
    assert not [a for a in store_lines("actions.jsonl") if a["action"] == "reissue_refund_store_credit"]


def test_confirmation_in_the_same_turn_is_not_accepted(mock):
    sly, iid = s09_proposed(mock)
    assert submit(sly, iid)["outcome"] == "awaiting_confirmation"
    result = submit(sly, iid, confirmed=True)  # asked and "answered" within one request
    assert result["outcome"] == "awaiting_confirmation" and "same turn" in result["confirmation_not_accepted"]
    assert evaluate(sly, iid, confirmed=True)["recommendation"] == "NEEDS_MORE_INFORMATION"


def test_dry_run_records_the_question_so_the_next_turn_can_confirm(mock):
    sly, iid = s09_proposed(mock)
    assert evaluate(sly, iid)["missing"] == "customer_confirmation"  # policy agent: ask the customer
    waiting = issue(sly, iid)
    assert waiting["status"] == "proposed" and waiting["outcome"]["outcome"] == "awaiting_confirmation"
    later = next_turn(mock, sly)
    result = submit(later, iid, confirmed=True)
    assert (result["outcome"], result["issue_status"]) == ("queued_for_review", "queued_for_review"), result


def test_confirmation_must_be_for_the_action_that_was_asked(mock):
    sly, iid = s09_proposed(mock)
    tool_support.case_store().set_status(sly["case_id"], iid, "proposed", by="system", outcome={
        "outcome": "awaiting_confirmation", "action": "reissue_refund_store_credit", "target_id": "ITM-S09-9-9",
        "turn_id": "an-earlier-turn"})
    result = submit(next_turn(mock, sly), iid, confirmed=True)
    assert result["confirmation_not_accepted"] == "the customer was asked to confirm a different action"


def test_evaluate_weak_sign_in_asks_for_step_up(mock):
    sly = {"customer_id": "CUST-S08", "channel": "phone", "auth_level": "weak"}
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    result = evaluate(sly, iid)
    assert (result["recommendation"], result["missing"]) == ("NEEDS_MORE_INFORMATION", "step_up_auth")
    assert next(c for c in result["checks"] if c["check"] == "sign_in_strength")["passed"] is False


def test_evaluate_changes_nothing(mock, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    sly = web(mock, "CUST-S12")
    (iid,) = start(sly, RET)
    propose(sly, iid, "retrigger_refund", "ITM-S12-1-1")
    assert evaluate(sly, iid)["recommendation"] == "ALLOW"
    assert store_lines("actions.jsonl") == [] and store_lines("decisions.jsonl") == []
    state = issue(sly, iid)
    assert state["status"] == "proposed" and "evaluate_policy" in state["evidence_sources"]


def test_evaluate_reports_blocks(mock, monkeypatch):
    sly = web(mock, "CUST-S08")
    (iid,) = start(sly, RET)
    propose(sly, iid, "initiate_refund", "ITM-S08-1-1")
    with_faults(monkeypatch, mock, "get_policy_signals:error")
    result = evaluate(sly, iid)
    assert (result["recommendation"], result["kind"]) == ("HUMAN_APPROVAL_REQUIRED", "tool_unavailable")
    assert issue(sly, iid)["status"] == "proposed"  # a dry run never hands over


def test_policy_config_shares_no_limits_or_risk_settings(mock):
    config = run(GetPolicyConfig().async_invoke({}, web(mock, "CUST-S01")))
    assert config["return_window_days"] == 30 and config["reference_date"] == "2026-10-16"
    assert "auto_refund_max" not in config and "escalate_risk_flags" not in config


@pytest.mark.parametrize("decision, reason, confirm, expected", [
    ("ALLOW", "ok", False, ("ALLOW", None, None)),
    ("ALLOW", "needs_customer_confirmation", True, ("NEEDS_MORE_INFORMATION", None, "customer_confirmation")),
    (DENY, "step_up_required", False, ("NEEDS_MORE_INFORMATION", None, "step_up_auth")),
    (DENY, "already_shipped", False, ("DENY", None, None)),
    (NEEDS_APPROVAL, "above_auto_limit", False, ("HUMAN_APPROVAL_REQUIRED", "approval", None)),
    (ESCALATE, "needs_specialist_review", False, ("HUMAN_APPROVAL_REQUIRED", "specialist_review", None)),
    (ESCALATE, "billing_review", False, ("HUMAN_APPROVAL_REQUIRED", "billing_review", None)),
])
def test_recommendation_mapping(decision, reason, confirm, expected):
    rec = recommendation(PolicyDecision("a", "t", decision, reason, requires_confirmation=confirm))
    assert (rec["recommendation"], rec["kind"], rec["missing"]) == expected


# ---------------------------------------------------------------------------- network wiring

def test_policy_agent_is_wired_and_advisory_only():
    conf = ConfigFactory.parse_file(str(NETWORK), resolve=False)
    tools = {t["name"]: t for t in conf["tools"]}
    assert "policy_risk_escalation" in conf["tools"][0]["tools"]
    assert list(tools["policy_risk_escalation"]["tools"]) == ["evaluate_policy", "get_policy_config"]
    assert tools["evaluate_policy"]["class"] == "customer_service.policy_tools.EvaluatePolicy"


# ---------------------------------------------------------------------------- D1: the customer's own yes, in code

@pytest.mark.parametrize("text", ["Yes.", "Yes, please.", "yes please issue it as store credit", "OK, go ahead",
                                  "Sure", "Great, yes please!", "That's fine.", "Please do", "Confirmed"])
def test_clear_yes(text):
    assert confirmation.is_clear_yes(text)


@pytest.mark.parametrize("text", ["No thanks", "Yes but only the shoes", "yes?", "Maybe later", "Don't do that",
                                  "I'd rather not", "Not yet", "Hmm, what does that mean", "Yes, no, wait", "",
                                  "Where is my other order?", "ok " + "x" * 400])
def test_not_a_clear_yes(text):
    assert not confirmation.is_clear_yes(text)


def test_a_no_is_never_a_confirmation_whatever_the_model_says(mock):
    sly, iid = s09_proposed(mock)
    assert evaluate(sly, iid)["missing"] == "customer_confirmation"
    later = {**next_turn(mock, sly), "customer_message": "No thanks, I'll wait for the card refund."}
    result = submit(later, iid, confirmed=True)  # the model claims a yes
    assert result["outcome"] == "awaiting_confirmation" and "clear yes" in result["confirmation_not_accepted"]
    assert not ActionStore().actions()  # nothing was queued or done


def test_a_confirmation_without_the_channels_message_does_not_count(mock):
    sly, iid = s09_proposed(mock)
    evaluate(sly, iid)
    later = next_turn(mock, sly)
    later.pop("customer_message")
    assert submit(later, iid, confirmed=True)["outcome"] == "awaiting_confirmation"
