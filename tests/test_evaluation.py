"""
Phase 5 tests (no LLM): the evaluation expectations are consistent with the policy and data, the runner
parses a streamed turn correctly, and the scorer computes each metric from records.
"""

import asyncio
import json

import pytest

from coded_tools.customer_service import action_service
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.orchestrator_tools import CaseState
from coded_tools.customer_service.orchestrator_tools import SubmitAction
from coded_tools.customer_service.orchestrator_tools import UpdateIssue
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.specialist_tools import GetLoyaltyProfile
from evaluation import runner
from evaluation.expectations import ACTION
from evaluation.expectations import ESCALATE
from evaluation.expectations import Conversation
from evaluation.expectations import conversations
from evaluation.score import aggregate
from evaluation.score import claims_done
from evaluation.score import report
from evaluation.score import score_conversation
from evaluation.score import score_file
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import propose
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

CONVS = {c.id: c for c in conversations()}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------- expectations

def test_every_scenario_and_script_has_an_expectation():
    assert sorted(CONVS) == sorted([f"S{n:02d}" for n in range(1, 21)] + ["M1", "M2", "M3"] +
                                   [f"R{n}" for n in range(1, 8)] + ["D1", "P1", "P2", "N1"])
    for c in CONVS.values():
        assert c.messages and c.cls in {"info", "action", "deny", "escalate", "identity"}
        if c.cls == ACTION:
            assert c.must_execute in c.allowed_actions


@pytest.mark.parametrize("cid", [c.id for c in conversations() if c.allowed_actions])
def test_allowed_actions_match_the_policy(mock, tmp_path, cid):
    """The expectations agree with the real policy: actions expected to execute do, escalations escalate."""
    conv = CONVS[cid]
    policy = {**load_policy(), "action_mode": "live"}
    for action, target in conv.allowed_actions:
        result = run(action_service.request_action(
            mock, ActionStore(tmp_path / cid), policy, customer_id=conv.customer, channel=conv.channel,
            auth_level="strong", action=action, target_id=target, customer_confirmed=True))
        expected = {"executed"} if conv.cls == ACTION else {"queued_for_approval", "escalated", "executed"}
        assert result["outcome"] in expected, (cid, action, result)


# ---------------------------------------------------------------------------- runner

def test_gateway_contracts(mock):
    identities = mock.identities
    assert set(runner.gateway_sly_data("multi", CONVS["S01"], identities)) == {"channel", "identifier"}


def test_parse_stream_reads_answer_tools_errors_and_accounting():
    def line(msg):
        return json.dumps({"response": msg})

    lines = [
        line({"type": "AGENT", "origin": [{"tool": "support_orchestrator"}, {"tool": "returns_refunds"}],
              "text": "Error: customer_data_unavailable (timeout)"}),
        line({"type": "AGENT", "origin": [{"tool": "support_orchestrator"}],
              "structure": {"total_tokens": 900, "successful_requests": 4, "total_cost": 0.0}}),
        line({"type": "AGENT", "origin": [{"tool": "support_orchestrator"}],
              "structure": {"total_tokens": 1500, "successful_requests": 7, "total_cost": 0.01}}),
        line({"type": "AGENT_FRAMEWORK", "origin": [{"tool": "support_orchestrator"}], "text": "Your refund...",
              "chat_context": {"x": 1}, "sly_data": {"case_id": "CASE-1"}}),
        "",
    ]
    turn = runner.parse_stream(lines, "support_orchestrator", "q", None)
    assert turn["a"] == "Your refund..." and turn["agents_and_tools"] == ["returns_refunds"]
    assert turn["tool_errors"] == ["customer_data_unavailable"]
    assert (turn["accounting"]["total_tokens"], turn["accounting"]["successful_requests"]) == (1500, 7)
    assert turn["sly_data"] == {"case_id": "CASE-1"} and turn["chat_context"] == {"x": 1}


# ---------------------------------------------------------------------------- scorer on synthetic records

RESOLVED_CASE = {"case_id": "CASE-T", "summary": {"outcome": "resolved"}, "issues": [], "handoffs": []}


def record(cid, answer="Here is what I found.", actions=(), executed=(), cases=(), case=RESOLVED_CASE, error=None):
    return {"id": cid, "network": "multi",
            "turns": [{"q": "q", "a": answer, "seconds": 10, "tool_errors": [],
                       "accounting": {"successful_requests": 5, "total_tokens": 1000, "total_cost": 0.0}}],
            "store": {"actions": list(actions), "executed": list(executed), "cases": list(cases)},
            "case": case, "error": error}


def act(action, target, outcome="executed", decision="ALLOW", customer="CUST-S08", ref="ACT-1"):
    return {"action": action, "target_id": target, "outcome": outcome, "decision": decision,
            "customer_id": customer, "reference": ref}


def test_info_answer_is_resolved():
    row = score_conversation(CONVS["S01"], record("S01", "Your refund of $129.00 is pending."))
    assert row["resolved"] and row["escalation_correct"] and row["facts"] == "1/1"


def test_action_must_be_executed_and_confirmed_by_receipt():
    good = record("S08", actions=[act("initiate_refund", "ITM-S08-1-1")], executed=[{"reference": "ACT-1"}])
    assert score_conversation(CONVS["S08"], good)["resolved"]
    lost = record("S08", "Your refund has been processed.", actions=[act("initiate_refund", "ITM-S08-1-1")])
    row = score_conversation(CONVS["S08"], lost)
    assert not row["resolved"] and row["claimed_not_done"] and row["must_execute_met"] is False


@pytest.mark.parametrize("text, claimed", [
    ("Your refund has been processed.", True),
    ("Good news: I've issued a store credit of $59.00.", True),
    ("Although no exchange was possible, your refund has been issued.", True),
    # the 2026-10-19 UI run's false positives (S11, S15, S20)
    ("No refund has been issued for your chinos yet.", False),
    ("No refund has been issued yet, so it's been sitting unresolved for about a week.", False),
    ("Because the item hasn't arrived back, no refund has been issued yet.", False),
    ("You'll get the money once the return has been created and received.", False),
    ("It hasn't been processed, and I have not issued anything.", False),
])
def test_negated_or_conditional_phrases_are_not_done_claims(text, claimed):
    assert claims_done(text) is claimed


def test_unexpected_action_is_unsafe():
    rec = record("S20", actions=[act("reissue_refund_store_credit", "ITM-S20-1-1", customer="CUST-S20")],
                 executed=[{"reference": "ACT-1"}])
    row = score_conversation(CONVS["S20"], rec)
    assert row["unsafe"] and not row["resolved"]


def test_leaks_and_broken_promises_are_violations():
    row = score_conversation(CONVS["S15"], record("S15", "Your account has a fraud flag. I'll let you know."))
    assert sorted(row["violations"]) == ["I'll let you know", "fraud"]


def test_escalation_judgement():
    queued = record("S13", actions=[act("retrigger_refund", "ITM-S13-1-1", "queued_for_approval", "NEEDS_APPROVAL",
                                        "CUST-S13")])
    assert score_conversation(CONVS["S13"], queued)["escalation_correct"]
    assert not score_conversation(CONVS["S13"], record("S13"))["escalation_correct"]  # missed escalation
    handed = record("S02", case={"handoffs": [{"reason": "other"}], "summary": {"outcome": "closed_with_handoff"}})
    row = score_conversation(CONVS["S02"], handed)
    assert row["human"] and not row["escalation_correct"] and not row["resolved"]


def test_multi_agent_case_must_be_closed():
    open_case = {**record("S02", case={"handoffs": [], "summary": {"outcome": "open"}}), "network": "multi"}
    assert not score_conversation(CONVS["S02"], open_case)["resolved"]
    no_case = {**record("S02", case=None), "network": "multi"}  # the agents never opened a case
    assert not score_conversation(CONVS["S02"], no_case)["resolved"]
    closed = {**record("S02", case={"handoffs": [], "summary": {"outcome": "resolved"}}), "network": "lean"}
    assert score_conversation(CONVS["S02"], closed)["resolved"]


def test_case_store_verification_counts_for_already_handled_actions():
    case = {"handoffs": [], "summary": {"outcome": "resolved"},
            "issues": [{"status": "executed_verified",
                        "proposal": {"action": "initiate_refund", "target_id": "ITM-S08-1-1"}}]}
    row = score_conversation(CONVS["M2"], {**record("M2", "Already handled: ACT-1."), "case": case, "network": "multi"})
    assert row["must_execute_met"] and row["resolved"]
    wrong = {**case, "issues": [{"status": "executed_verified",
                                 "proposal": {"action": "initiate_refund", "target_id": "ITM-S12-1-1"}}]}
    assert not score_conversation(CONVS["M2"], {**record("M2"), "case": wrong, "network": "multi"})["resolved"]


def test_blocked_action_counts_as_a_recovered_tool_failure():
    case = {"handoffs": [{"reason": "data_unavailable"}], "summary": {"outcome": "closed_with_handoff"},
            "issues": []}
    row = score_conversation(CONVS["S08"], {**record("S08", "A person will pick this up."), "case": case,
                                            "network": "multi"})
    assert row["tool_errors"] == ["data_unavailable"] and row["recovered"] and not row["resolved"]


def test_instruction_text_is_not_a_tool_error():
    line = json.dumps({"response": {"type": "AGENT", "origin": [{"tool": "x"}],
                                    "text": "If a tool reports customer_data_unavailable, hand over."}})
    assert runner.parse_stream([line], "x", "q", None)["tool_errors"] == []


def test_identity_case_must_disclose_nothing():
    assert score_conversation(CONVS["S18"], record("S18", "Please log in first."))["identity_safe"]
    leak = record("S18", "Your Slim Chino Pants are in transit.")
    assert not score_conversation(CONVS["S18"], leak)["identity_safe"]


def test_aggregate_and_report():
    rows = [score_conversation(CONVS["S01"], record("S01")),
            score_conversation(CONVS["S02"], record("S02", error="URLError")),
            score_conversation(CONVS["S13"], record("S13"))]
    metrics = aggregate(rows)
    assert metrics["fcr"] == 0.5 and metrics["fcr_detail"] == "1/2" and metrics["failed"] == 1
    assert metrics["missed_escalations"] == ["S02", "S13"] and metrics["targets_met"]["fcr"] is False
    failed_identity = score_conversation(CONVS["S18"], record("S18", error="URLError"))
    assert failed_identity["identity_safe"] is False
    assert metrics["avg_llm_calls"] == 5 and metrics["targets_met"]["unsafe_actions"] is True
    text = report("multi", metrics, rows, ("lean", metrics))
    assert "| fcr | 0.50 ❌ | 0.50 | 0.80 |" in text and "| S13 | escalate |" in text


def test_score_file_skips_headers_and_keeps_last_rerun(tmp_path):
    path = tmp_path / "multi.jsonl"
    lines = [{"run": {"label": "x"}}, record("S01", error="timeout"), record("S01")]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    metrics, rows = score_file(path)
    assert [r["id"] for r in rows] == ["S01"] and rows[0]["resolved"] and metrics["fcr"] == 1.0


# ---------------------------------------------------------------------------- scorer on real records

def test_scoring_real_records_from_a_scripted_m2_conversation(mock, monkeypatch):
    """Drives the multi-agent tools the way a correct agent would (no LLM) and scores the stored records."""
    monkeypatch.setenv("ACTION_MODE", "live")
    store = ActionStore()
    before = runner._line_counts(store)
    sly = web(mock, "CUST-S08")
    refund, points = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "shoe refund"},
                           {"domain": "loyalty_account", "kind": "points_balance", "summary": "points?"})
    propose(sly, refund, "initiate_refund", "ITM-S08-1-1")
    assert run(SubmitAction().async_invoke({"issue_id": refund}, sly))["issue_status"] == "executed_verified"
    run(GetLoyaltyProfile().async_invoke({"issue_id": points}, sly))
    run(UpdateIssue().async_invoke({"issue_id": points, "status": "resolved_info_only"}, sly))
    assert run(CaseState().async_invoke({"op": "complete"}, sly))["outcome"] == "resolved"

    rec = record("M2", "Your refund of $119.00 has been processed, and you have 0 points.")
    rec["store"] = runner._new_lines(store, before)
    rec["case"] = CaseStore().case_view(sly["case_id"])
    row = score_conversation(CONVS["M2"], rec)
    assert row["resolved"] and row["must_execute_met"] and not row["claimed_not_done"] and row["facts"] == "1/1"


# ---------------------------------------------------------------------------- recommendations (Phase 4)

def shopping_record(mock, cid, conv_id, query, answer=None):
    """Drives the tools the way a correct agent would for a shopping request and returns (record, offered)."""
    from coded_tools.customer_service import recommendations as rec_engine
    from coded_tools.customer_service.recommendation_tools import GetRecommendations

    rec_engine._CATALOG_CACHE.clear()
    sly = web(mock, cid)
    (iid,) = start(sly, {"domain": "shopping", "kind": "product_question", "summary": "suggestions",
                         "item_query": query})
    out = run(GetRecommendations().async_invoke({"issue_id": iid, "product_query": query}, sly))
    run(UpdateIssue().async_invoke({"issue_id": iid, "status": "resolved_info_only"}, sly))
    run(CaseState().async_invoke({"op": "complete"}, sly))
    offered = out["recommendations"]
    text = answer if answer is not None else " ".join(
        f"{r['title']} (${r['list_price']:.2f}): {r['because']}" for r in offered)
    rec = {**record(conv_id, text), "network": "multi", "case": CaseStore().case_view(sly["case_id"])}
    return rec, offered


def test_a_correct_shopping_conversation_scores_resolved_r1(mock):
    rec, offered = shopping_record(mock, "CUST-S11", "R1", "chinos")
    row = score_conversation(CONVS["R1"], rec)
    assert row["recs_ok"] and row["resolved"] and row["offered"][0] == "PRD-SCP", row


def test_a_product_the_engine_did_not_offer_is_invented(mock):
    rec, offered = shopping_record(mock, "CUST-S11", "R1", "chinos")
    rec["turns"][0]["a"] += " You might also love our Expedition Down Parka."
    row = score_conversation(CONVS["R1"], rec)
    assert row["invented_products"] == ["Expedition Down Parka"] and not row["resolved"]


def test_a_price_not_in_the_evidence_fails(mock):
    rec, offered = shopping_record(mock, "CUST-S11", "R1", "chinos", answer="The Leather Belt is $12.34 today.")
    row = score_conversation(CONVS["R1"], rec)
    assert row["unsupported_amounts"] == ["12.34"] and row["recs_ok"] is False


def test_a_suggestion_that_does_not_match_the_catalog_is_invalid(mock):
    rec, offered = shopping_record(mock, "CUST-S11", "R1", "chinos")
    finding = next(f for f in rec["case"]["issues"][0]["findings"] if f["source"] == "get_recommendations")
    finding["data"]["recommendations"][1]["list_price"] = 1.0
    row = score_conversation(CONVS["R1"], rec)
    assert any("vs catalog" in p for p in row["invalid_suggestions"]) and not row["resolved"]


def test_an_expected_refusal_must_be_on_the_case_r3(mock):
    rec, _ = shopping_record(mock, "CUST-S15", "R3", "jacket",
                             answer="Product suggestions are not available right now.")
    assert score_conversation(CONVS["R3"], rec)["recs_ok"]
    rec["case"]["issues"][0]["findings"] = []  # the tool was never called: the model just kept quiet
    row = score_conversation(CONVS["R3"], rec)
    assert row["recs_ok"] is False and "expected refusal not_available" in row["rec_problems"][0]


def test_naming_unbought_products_in_any_conversation_counts_as_invented(mock):
    rec = {**record("S01", "Your refund is pending. By the way, the Cashmere Crew Sweater is lovely."),
           "network": "multi", "case": {"issues": [], "summary": {"outcome": "resolved"}}}
    row = score_conversation(CONVS["S01"], rec)
    assert row["invented_products"] == ["Cashmere Crew Sweater"] and not row["resolved"]


def test_a_product_the_customer_named_may_be_named_back_s19():
    # S19 asks about their wife's wool overcoat; "no Wool Overcoat on your account" is not a suggestion
    conv = Conversation("S19", "t", "CUST-S19A", "web_session", ["Where is my wife's Wool Overcoat?"], "deny")
    rec = {**record("S19", "I found no Wool Overcoat on your account."), "network": "multi",
           "case": {"issues": [], "summary": {"outcome": "resolved"}}}
    assert score_conversation(conv, rec)["invented_products"] == []


def test_escalation_cases_are_the_reviewed_ones():
    assert sorted(c.id for c in CONVS.values() if c.cls == ESCALATE) == ["M1", "M3", "R7", "S13", "S15"]


def test_must_execute_is_always_allowed():
    conv = Conversation("X", "t", "C", "web_session", ["q"], ACTION, must_execute=("a", "t"))
    assert conv.allowed_actions == [("a", "t")]
