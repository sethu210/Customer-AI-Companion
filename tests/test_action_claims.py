"""
Review findings P4 (idempotency across processes) and D3 (cumulative refund limits): the claims table in the action
store's actions.db, and how the action service and the review console use it.
"""

import asyncio
import json
import threading

import pytest

from coded_tools.customer_service.action_service import request_action
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.action_store import OverLimit
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.policy import ALLOW
from coded_tools.customer_service.policy import NEEDS_APPROVAL
from coded_tools.customer_service.policy import PolicyDecision
from coded_tools.customer_service.policy import decide
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.policy import over_cumulative_limit


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


@pytest.fixture()
def store(tmp_path) -> ActionStore:
    return ActionStore(tmp_path / "actions")


def refund(target: str, amount: float) -> PolicyDecision:
    return PolicyDecision("initiate_refund", target, ALLOW, "ok", amount)


def test_only_one_of_many_concurrent_claims_wins(tmp_path):
    results: list = []
    start = threading.Barrier(8)

    def worker() -> None:
        own = ActionStore(tmp_path / "actions")  # its own connection, as another process would have
        start.wait()
        results.append(own.claim("K1", reference="ACT-1", customer_id="C1", decision=refund("I1", 10),
                                 outcome="executed"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is None for r in results) == 1
    assert all(r["reference"] == "ACT-1" for r in results if r is not None)


def test_a_second_request_for_the_same_action_never_executes_again(mock, tmp_path, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    store, policy = ActionStore(tmp_path / "actions"), load_policy()
    args = dict(customer_id="CUST-S08", channel="web_session", auth_level="strong", action="initiate_refund",
                target_id="ITM-S08-1-1")
    first = run(request_action(mock, store, policy, **args))
    second = run(request_action(mock, ActionStore(tmp_path / "actions"), policy, **args))
    assert first["outcome"] == "executed" and second["outcome"] == "already_handled"
    assert second["reference"] == first["reference"] and len(store.read("executed.jsonl")) == 1


def test_cumulative_limits_in_the_claim(store):
    limits = (100.0, 150.0)
    assert store.claim("A", reference="R-A", customer_id="C1", decision=refund("I1", 60), outcome="executed",
                       limits=limits) is None
    with pytest.raises(OverLimit):  # 60 + 50 > 100 today
        store.claim("B", reference="R-B", customer_id="C1", decision=refund("I2", 50), outcome="executed",
                    limits=limits)
    assert store.find_final("B") is None  # nothing was claimed
    assert store.claim("C", reference="R-C", customer_id="C2", decision=refund("I3", 50), outcome="executed",
                       limits=limits) is None  # another customer has their own total
    assert store.auto_refunds("C1") == (60.0, 60.0)


def test_only_automatic_refunds_count(store):
    store.claim("A", reference="R-A", customer_id="C1", decision=PolicyDecision("initiate_refund", "I1", NEEDS_APPROVAL,
                                                                                "above_auto_limit", 500),
                outcome="queued_for_approval")
    store.claim("B", reference="R-B", customer_id="C1", decision=PolicyDecision("cancel_order", "O1", ALLOW, "ok", 90),
                outcome="executed")
    assert store.auto_refunds("C1") == (0.0, 0.0)


def test_policy_sends_a_refund_over_the_cumulative_limit_to_a_person(mock):
    policy = load_policy()
    context = run(mock.get_customer_context("CUST-S08"))
    stuck = run(mock.get_stuck_cases("CUST-S08"))
    signals = (run(mock.get_policy_signals("CUST-S08")) or [{}])[0]
    fine = decide("initiate_refund", "ITM-S08-1-1", "strong", context, stuck, signals, policy)
    assert fine.decision == ALLOW
    near = (float(policy["auto_refund_daily_max"]) - fine.amount + 0.01, 0.0)
    over = decide("initiate_refund", "ITM-S08-1-1", "strong", context, stuck, signals, policy,
                  recent_auto_refunds=near)
    assert (over.decision, over.reason_code) == (NEEDS_APPROVAL, "above_cumulative_limit")
    assert not over_cumulative_limit("cancel_order", 1000, (10**6, 10**6), policy)  # not a refund


def test_request_over_the_limit_is_queued_for_approval(mock, tmp_path, monkeypatch):
    monkeypatch.setenv("ACTION_MODE", "live")
    policy = {**load_policy(), "auto_refund_daily_max": 1.0}
    result = run(request_action(mock, ActionStore(tmp_path / "actions"), policy, customer_id="CUST-S08",
                                channel="web_session", auth_level="strong", action="initiate_refund",
                                target_id="ITM-S08-1-1"))
    assert (result["outcome"], result["reason"]) == ("queued_for_approval", "above_cumulative_limit")


def test_actions_of_an_older_store_are_claimed_when_the_claims_table_is_created(tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    record = {"at": "2026-10-20T10:00:00Z", "key": "K-OLD", "reference": "ACT-OLD", "customer_id": "C1",
              "action": "initiate_refund", "target_id": "I1", "amount": 20.0, "decision": ALLOW,
              "outcome": "executed", "case_id": None}
    (directory / "actions.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert ActionStore(directory).find_final("K-OLD")["reference"] == "ACT-OLD"


def test_a_review_item_is_decided_once(store):
    assert store.claim_review("REV-1", "Sam") is True
    assert ActionStore(store.dir).claim_review("REV-1", "Alex") is False
