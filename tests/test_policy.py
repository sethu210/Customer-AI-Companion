"""Phase 4 tests: deterministic policy, action outcomes, idempotency, audit, and identity guards (mock data)."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from coded_tools.customer_service import action_service
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.policy import load_policy

ROOT = Path(__file__).parents[1]
EXPECTATIONS = {e["id"]: e for e in json.loads((ROOT / "test_data/out/scenario_expectations.json").read_text())}
SCENARIO_ACTION_NAMES = {"reissue_refund": "reissue_refund_store_credit"}
# Scenario -> the target the agent would act on.
TARGETS = {"S08": "ITM-S08-1-1", "S09": "ITM-S09-1-1", "S10": "ORD-S10-1", "S12": "ITM-S12-1-1",
           "S13": "ITM-S13-1-1", "S14": "ITM-S14-1-1", "S15": "ITM-S15-4-1", "S16": "ORD-S16-1"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


@pytest.fixture()
def store(tmp_path) -> ActionStore:
    return ActionStore(tmp_path)


def policy(mode: str = "shadow") -> dict:
    p = load_policy()
    p["action_mode"] = mode
    return p


def ask(mock, store, sid_or_customer, action, target, *, mode="shadow", auth="strong", confirmed=False):
    customer = EXPECTATIONS[sid_or_customer]["customer"] if sid_or_customer in EXPECTATIONS else sid_or_customer
    return run(action_service.request_action(mock, store, policy(mode), customer_id=customer, channel="web_session",
                                             auth_level=auth, action=action, target_id=target,
                                             customer_confirmed=confirmed))


def test_policy_thresholds_match_test_data_settings():
    settings = yaml.safe_load((ROOT / "test_data/generator/settings.yaml").read_text())["policy"]
    p = load_policy()
    for key, value in settings.items():
        assert float(p[key]) == float(value), key


@pytest.mark.parametrize("sid", sorted(TARGETS))
def test_scenario_decisions(mock, store, sid):
    expected = EXPECTATIONS[sid]["action"]
    action = SCENARIO_ACTION_NAMES.get(expected["type"], expected["type"])
    result = ask(mock, store, sid, action, TARGETS[sid], confirmed=True)
    outcome_for = {"ALLOW": "queued_for_review", "NEEDS_APPROVAL": "queued_for_approval",
                   "ESCALATE": "escalated", "DENY": "denied"}
    assert result["outcome"] == outcome_for[expected["decision"]], result
    if "amount" in expected:
        assert result["amount"] == expected["amount"]


@pytest.mark.parametrize("sid, action, target, reason", [
    ("S01", "initiate_refund", "ITM-S01-1-1", "refund_exists"),       # refund already pending
    ("S01", "retrigger_refund", "ITM-S01-1-1", "refund_not_stuck"),   # pending only 2 days
    ("S11", "initiate_refund", "ITM-S11-1-1", "return_not_received"), # label made, parcel never sent
    ("S02", "open_carrier_trace", "ORD-S02-1", "shipment_not_stuck"),
    ("S19", "initiate_refund", "ITM-S19B-1-1", "not_found"),          # spouse's item, other account
    ("S08", "refund_everything", "ITM-S08-1-1", "unknown_action"),
])
def test_not_applicable_requests_are_denied(mock, store, sid, action, target, reason):
    result = ask(mock, store, sid, action, target)
    assert (result["outcome"], result["reason"]) == ("denied", reason)
    assert "reference" not in result


def test_weak_auth_cannot_write(mock, store):
    result = ask(mock, store, "S08", "initiate_refund", "ITM-S08-1-1", auth="weak")
    assert (result["outcome"], result["reason"]) == ("denied", "step_up_required")


def test_missing_auth_level_is_treated_as_weak(mock, store):
    result = ask(mock, store, "S08", "initiate_refund", "ITM-S08-1-1", auth=None)
    assert result["reason"] == "step_up_required"


def test_shadow_mode_queues_and_is_idempotent(mock, store):
    first = ask(mock, store, "S08", "initiate_refund", "ITM-S08-1-1")
    second = ask(mock, store, "S08", "initiate_refund", "ITM-S08-1-1")
    assert first["outcome"] == "queued_for_review"
    assert second["outcome"] == "already_handled" and second["reference"] == first["reference"]
    assert len(store.cases()) == 1 and store.cases()[0]["kind"] == "shadow_review"
    assert not (store.dir / "executed.jsonl").exists()


def test_live_mode_executes_once(mock, store):
    first = ask(mock, store, "S12", "retrigger_refund", "ITM-S12-1-1", mode="live")
    again = ask(mock, store, "S12", "retrigger_refund", "ITM-S12-1-1", mode="live")
    assert first["outcome"] == "executed" and first["amount"] == 36.0
    assert again["outcome"] == "already_handled"
    executed = (store.dir / "executed.jsonl").read_text().splitlines()
    assert len(executed) == 1


def test_confirmation_flow(mock, store):
    pending = ask(mock, store, "S09", "reissue_refund_store_credit", "ITM-S09-1-1", mode="live")
    assert pending["outcome"] == "awaiting_confirmation" and "reference" not in pending
    assert not (store.dir / "executed.jsonl").exists()
    done = ask(mock, store, "S09", "reissue_refund_store_credit", "ITM-S09-1-1", mode="live", confirmed=True)
    assert done["outcome"] == "executed" and done["amount"] == 68.0


def test_escalation_does_not_reveal_risk_flags(mock, store):
    result = ask(mock, store, "S15", "initiate_refund", "ITM-S15-4-1", mode="live")
    assert result["outcome"] == "escalated"
    text = json.dumps(result).lower()
    assert "fraud" not in text and "risk" not in text and "high_refund_rate" not in text


def test_every_decision_is_audited(mock, store):
    ask(mock, store, "S14", "create_return", "ITM-S14-1-1")
    ask(mock, store, "S13", "retrigger_refund", "ITM-S13-1-1")
    lines = [json.loads(x) for x in (store.dir / "decisions.jsonl").read_text().splitlines()]
    assert [(r["decision"], r["outcome"]) for r in lines] == [("DENY", "denied"),
                                                              ("NEEDS_APPROVAL", "queued_for_approval")]
    assert lines[1]["facts"]["amount"] == 289.0 and lines[1]["facts"]["policy_version"]
