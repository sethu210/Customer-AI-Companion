"""Review finding D4: the reply is checked against the records in code before the customer sees it."""

import asyncio

from coded_tools.customer_service import reply_guard
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import propose
from test_multi_agent_foundation import ref
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

RET = {"domain": "returns_refunds", "kind": "refund_status", "summary": "Where is my refund?"}


def run(coro):
    return asyncio.run(coro)


def view(issues=(), numbers=()):
    return {"issues": [{"status": s, "findings": [{"data": {"amount": n}} for n in numbers]} for s in issues] or
            [{"status": "resolved_info_only", "findings": [{"data": {"amount": n}} for n in numbers]}]}


def test_references_must_be_the_customers_own():
    assert reply_guard.problems("Your case is CASE-AB12CD34EF.", view(), {"CASE-AB12CD34EF"}) == []
    found = reply_guard.problems("Reference ACT-0011223344.", view(), {"CASE-AB12CD34EF"})
    assert found and found[0].startswith("unknown_reference")


def test_amounts_must_be_in_the_records_or_a_sum_of_two():
    records = view(numbers=(129.0, 45.5))
    assert reply_guard.problems("A refund of $129.00 and one of $45.50, $174.50 in all.", records, set()) == []
    found = reply_guard.problems("A refund of $139.00.", records, set())
    assert found == ["unsupported_amount: 139.00"]


def test_done_claims_need_a_verified_execution():
    assert reply_guard.problems("Your refund has been processed.", view(), set()) == ["claims_done_without_execution"]
    assert reply_guard.problems("Your refund has been processed.", view(["executed_verified"]), set()) == []
    assert reply_guard.problems("No refund has been issued yet.", view(), set()) == []


def test_internal_words_and_promises_are_never_shown():
    found = reply_guard.problems("I'll let you know when the fraud check is done.", view(), set())
    assert found and found[0].startswith("forbidden")


def test_an_unsafe_reply_is_replaced_by_what_the_records_say(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, {**RET, **ref("retrigger_refund", "ITM-S13-1-1")})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    propose(sly, iid, "retrigger_refund", "ITM-S13-1-1")
    since = turn_start()
    queued = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    assert queued["outcome"] == "queued_for_approval"
    shown, checked = finish_turn(sly["case_id"], since, "Great news, your refund has been processed!")
    assert checked["reply_problem"] == "unsafe: claims_done_without_execution"
    assert "has been processed" not in shown and queued["reference"] in shown and "review" in shown
    assert sly["case_id"] in shown


def test_a_safe_reply_is_shown_unchanged(mock):
    sly = web(mock, "CUST-S13")
    (iid,) = start(sly, {**RET, **ref("retrigger_refund", "ITM-S13-1-1")})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    propose(sly, iid, "retrigger_refund", "ITM-S13-1-1")
    since = turn_start()
    queued = run(SubmitAction().async_invoke({"issue_id": iid}, sly))
    reply = f"Your refund was sent to a colleague for review. Your reference is {queued['reference']}."
    shown, checked = finish_turn(sly["case_id"], since, reply)
    assert shown == reply and checked["reply_problem"] == ""
    assert CaseStore().get_case(sly["case_id"])["customer_id"] == "CUST-S13"
