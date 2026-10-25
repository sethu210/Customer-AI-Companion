"""
The gateway's checks after each reply (no LLM): an unusable reply is replaced by a fixed message, and after it
nothing counts as waiting for the customer, so every open issue is handed to a person.
"""

import asyncio

import pytest

from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from coded_tools.customer_service.gateway import reply_problem
from coded_tools.customer_service.orchestrator_tools import CaseState
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import propose
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

S09_TURN_2 = '{{\n  "issue_id": "CASE-F2D77CDDEE-I1",\n  "customer_confirmed": true\n}}'  # 2026-10-19 UI run


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("answer, problem", [
    (S09_TURN_2, "json"),
    ('```json\n{"issue_id": "X"}\n```', "json"),
    ("[1, 2]", "json"),
    ("", "empty"),
    (None, "empty"),
    ("   ", "empty"),
    ("Your refund of $68.00 was reissued as store credit (reference ACT-1).", ""),
    ("{Note} Your parcel arrives on Friday.", ""),  # starts with a brace but is a sentence
])
def test_reply_problem(answer, problem):
    assert reply_problem(answer) == problem


def test_unusable_reply_after_a_confirmation_question_hands_off_and_says_so_s09(mock):
    sly = web(mock, "CUST-S09")
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "red sweater refund"})
    propose(sly, iid, "reissue_refund_store_credit", "ITM-S09-1-1")
    since = turn_start()
    assert run(SubmitAction().async_invoke({"issue_id": iid}, sly))["outcome"] == "awaiting_confirmation"
    # the question was recorded in this turn, but the customer never saw it: nothing may count as waiting
    shown, checked = finish_turn(sly["case_id"], since, S09_TURN_2)
    assert checked["reply_problem"] == "json" and checked["left_open"] == [iid] and checked["completed"]
    assert "A colleague will pick it up" in shown and sly["case_id"] in shown
    view = run(CaseState().async_invoke({}, sly))
    assert view["handoffs"][0]["reason"] == "reply_failed" and view["case_status"] == "closed_with_handoff"


def test_unusable_reply_without_a_case_only_asks_to_send_again():
    shown, checked = finish_turn(None, turn_start(), "")
    assert checked["reply_problem"] == "empty" and "send your message again" in shown and "colleague" not in shown


def test_a_good_reply_is_passed_through(mock):
    sly = web(mock, "CUST-S09")
    start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "red sweater refund"})
    shown, checked = finish_turn(sly["case_id"], turn_start(), "Here is what I found.")
    assert shown == "Here is what I found." and checked["reply_problem"] == ""
