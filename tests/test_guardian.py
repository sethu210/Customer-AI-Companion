"""
E3 tests (no LLM): the local event simulator and the Resolution Guardian. Watches are started by the gateway at the
end of a turn; the watcher follows them on the simulator's clock, sends template updates to the outbox, hands off
to people and closes only what a second read confirms. It never changes or re-tries anything.
"""

import asyncio
import re

import pytest

from coded_tools.customer_service import guardian as g
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from coded_tools.customer_service.intake_tools import OpenCase
from coded_tools.customer_service.orchestrator_tools import HandoffToHuman
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.review_service import ReviewService
from coded_tools.customer_service.simulator import SimulatedAdapter
from coded_tools.customer_service.simulator import Simulator
from coded_tools.customer_service.specialist_tools import GetOrders
from coded_tools.customer_service.specialist_tools import GetReturnsRefunds
from test_multi_agent_foundation import answered
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web
from test_review_console import queued

S01_ITEM = "ITM-S01-1-1"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sim(monkeypatch, tmp_path, mock):
    path = tmp_path / "simulator.json"
    monkeypatch.setenv("SIMULATOR_STATE", str(path))
    return Simulator(path)


@pytest.fixture()
def guard(sim, mock):
    return g.Guardian(adapter=SimulatedAdapter(mock), simulator=sim)


def answered_turn(mock, customer, domain, kind, item, tool):
    """A conversation that answered a question about an item: the issue closes as information only."""
    sly = web(mock, customer)
    since = turn_start()
    (iid,) = start(sly, {"domain": domain, "kind": kind, "summary": "question", "item_id": item})
    run(tool().async_invoke({"issue_id": iid}, sly))
    assert answered(sly, iid)["closed"]
    _, checked = finish_turn(sly["case_id"], since, "Here is what I found.")
    return sly, iid, checked


def s01(mock):
    return answered_turn(mock, "CUST-S01", "returns_refunds", "refund_status", S01_ITEM, GetReturnsRefunds)


def s02(mock):
    return answered_turn(mock, "CUST-S02", "order_delivery", "where_is_order", "ITM-S02-1-1", GetOrders)


def watch_of(iid):
    (w,) = [w for w in g.GuardianStore().watches() if w["issue_id"] == iid]
    return w


def sent(customer=None):
    return [n for n in g.GuardianStore().notices(customer_id=customer) if n["status"] == "sent"]


def outbox():
    return [r for r in ActionStore().read("outbox.jsonl") if r.get("source") == "guardian"]


# ---------------------------------------------------------------------------- simulator


def test_simulator_shows_events_on_top_and_never_changes_the_data(mock, sim):
    adapter = SimulatedAdapter(mock)
    before = run(mock.get_customer_context("CUST-S01"))
    sim.advance(5)
    sim.add_event("refund_completed", target=S01_ITEM)
    rows = run(adapter.get_customer_context("CUST-S01"))
    row = next(r for r in rows if r["item_id"] == S01_ITEM)
    assert row["refund_status"] == "completed" and row["refund_completed_at"] == sim.now_ts()
    assert run(mock.get_customer_context("CUST-S01")) == before  # the base data is untouched
    timeline = run(adapter.get_case_timeline("CUST-S01", row["order_id"]))
    assert any(e["event_type"] == "refund_completed" and e["item_id"] == S01_ITEM for e in timeline)
    sim.reset()
    assert run(adapter.get_customer_context("CUST-S01")) == before


def test_unconfirmed_event_is_not_on_the_timeline_and_resolved_cases_leave_the_stuck_list(mock, sim):
    adapter = SimulatedAdapter(mock)
    sim.add_event("refund_completed", target="ITM-S08-1-1", confirmed=False)
    rows = run(adapter.get_customer_context("CUST-S08"))
    assert next(r for r in rows if r["item_id"] == "ITM-S08-1-1")["refund_status"] == "completed"
    timeline = run(adapter.get_case_timeline("CUST-S08", "ORD-S08-1"))
    assert not any(e["event_type"] == "refund_completed" and e["item_id"] == "ITM-S08-1-1" for e in timeline)
    assert not [c for c in run(adapter.get_stuck_cases("CUST-S08")) if c["item_id"] == "ITM-S08-1-1"]
    sim.add_event("parcel_scanned", target="ORD-S10-1")
    assert not run(adapter.get_stuck_cases("CUST-S10"))


def test_a_foreign_order_gets_no_simulated_timeline(mock, sim):
    sim.add_event("parcel_delivered", target="ORD-S10-1")
    assert run(SimulatedAdapter(mock).get_case_timeline("CUST-S01", "ORD-S10-1")) == []


def test_clock_starts_after_the_data_and_only_moves_when_advanced(sim):
    start_ts = sim.now_ts()
    assert start_ts == "2026-10-16T18:00:00Z"
    assert sim.advance(30) == "2026-10-18T00:00:00Z"
    with pytest.raises(ValueError):
        sim.advance(0)


# ---------------------------------------------------------------------------- watches start at the end of a turn


def test_answered_refund_question_starts_a_watch_and_the_first_check_decides_what_to_wait_for(mock, guard):
    sly, iid, checked = s01(mock)
    assert checked["watching"] == [watch_of(iid)["watch_id"]]
    assert watch_of(iid)["state"] == "watching" and watch_of(iid)["milestones"] is None
    run(guard.run_once())
    w = watch_of(iid)
    assert (w["state"], w["milestones"], w["subject"]["items"]) == ("waiting_on_business", ["refund_completed"],
                                                                    [S01_ITEM])
    assert sent() == []  # nothing new to tell yet


def test_nothing_in_progress_closes_the_watch_quietly_s03(mock, guard):
    sly = web(mock, "CUST-S03")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "did it go through",
                         "order_id": "ORD-S03-1"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    answered(sly, iid)
    finish_turn(sly["case_id"], since, "Yes, it was completed.")
    run(guard.run_once())
    assert (watch_of(iid)["state"], watch_of(iid)["closed_reason"]) == ("closed", "nothing_pending")


def test_the_same_refund_is_not_watched_twice_and_a_returning_customer_is_told(mock, guard):
    _, first, _ = s01(mock)
    sly2, second, checked = s01(mock)
    assert checked["watching"] == [] and [w["issue_id"] for w in g.GuardianStore().watches()] == [first]
    opened = run(OpenCase().async_invoke({}, web(mock, "CUST-S01")))
    (watching,) = opened["being_watched"]
    assert watching["issue_id"] == first and watching["state"] == "watching"
    assert "CUST-" not in str(watching)


def test_an_item_known_only_by_the_customers_words_is_found_in_code(mock, guard):
    sly = web(mock, "CUST-S01")
    since = turn_start()
    (iid,) = start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "refund?",
                         "item_query": "blue jacket"})
    run(GetReturnsRefunds().async_invoke({"issue_id": iid}, sly))
    answered(sly, iid)
    finish_turn(sly["case_id"], since, "It is pending.")
    run(guard.run_once())
    w = watch_of(iid)
    assert (w["subject"]["item_id"], w["milestones"], w["subject_key"]) == (S01_ITEM, ["refund_completed"],
                                                                             f"item:{S01_ITEM}")
    _, again, _ = answered_turn(mock, "CUST-S01", "returns_refunds", "refund_status", S01_ITEM, GetReturnsRefunds)
    assert [w["issue_id"] for w in g.GuardianStore().watches()] == [iid]  # the same item: not watched twice


def test_unidentified_contacts_are_not_watched(mock, guard):
    assert g.watch_case("CASE-NOPE") == []


# ---------------------------------------------------------------------------- the rules


def test_refund_completes_one_update_verified_close_and_no_repeat(mock, guard, sim):
    sly, iid, _ = s01(mock)
    run(guard.run_once())
    sim.advance(2)
    sim.add_event("refund_completed", target=S01_ITEM)
    summary = run(guard.run_once())
    assert summary["checked"] == []  # not due yet: the next check is 6 hours after the last one
    sim.advance(12)  # 08:00, after the quiet hours
    run(guard.run_once())
    (notice,) = sent()
    assert notice["kind"] == "refund_completed" and "$129.00" in notice["message"] and sly["case_id"] in notice["message"]
    assert (watch_of(iid)["state"], watch_of(iid)["closed_reason"]) == ("closed", "resolved_verified")
    assert [o["message"] for o in outbox()] == [notice["message"]]
    sim.advance(24)
    run(guard.run_once())
    run(g.Guardian(adapter=SimulatedAdapter(mock), simulator=sim).run_once())  # a restarted worker
    assert len(sent()) == 1 and len(outbox()) == 1
    assert "guardian_resolved" in [e["kind"] for e in CaseStore().events(sly["case_id"])]


def test_completion_the_timeline_does_not_confirm_is_never_closed(mock, guard, sim):
    sly, iid, _ = s01(mock)
    run(guard.run_once())
    sim.add_event("refund_completed", target=S01_ITEM, confirmed=False)
    for _ in range(3):
        sim.advance(6)
        run(guard.run_once())
    w = watch_of(iid)
    assert w["state"] == "escalated" and w["closed_reason"] is None and w["unconfirmed"] >= 2
    (handoff,) = CaseStore().handoffs()
    assert handoff["reason"] == "guardian_unconfirmed" and "does not confirm" in handoff["packet"]["agent_summary"]
    assert [n["kind"] for n in sent()] == ["escalated"]  # never "completed"


def test_delay_is_explained_once_then_a_person_takes_over_at_the_target(mock, guard, sim):
    _, iid, _ = s01(mock)
    run(guard.run_once())
    actions_before = ActionStore().actions()
    sim.advance(24 * 3)  # 5 days since the refund was started
    run(guard.run_once())
    assert [n["kind"] for n in sent()] == ["refund_delayed"]
    assert "5 days" in sent()[0]["message"]
    sim.advance(24)
    run(guard.run_once())
    assert len(sent()) == 1  # explained once
    sim.advance(24 * 4)  # past the 168-hour target
    run(guard.run_once())
    assert [n["kind"] for n in sent()] == ["refund_delayed", "escalated"]
    assert watch_of(iid)["state"] == "escalated"
    assert CaseStore().handoffs()[0]["reason"] == "guardian_sla_breached"
    assert ActionStore().actions() == actions_before  # nothing was re-tried or changed (decision 5)


def test_failed_refund_is_handed_off_not_retried(mock, guard, sim):
    _, iid, _ = s01(mock)
    run(guard.run_once())
    actions_before = ActionStore().actions()
    sim.add_event("refund_failed", target=S01_ITEM, reason="card_expired")
    run(guard.run_once(customer_id="CUST-S01"))
    assert [n["kind"] for n in sent()] == ["refund_failed"]
    (handoff,) = CaseStore().handoffs()
    assert handoff["reason"] == "guardian_refund_failed" and "card_expired" in handoff["packet"]["agent_summary"]
    assert watch_of(iid)["state"] == "escalated" and ActionStore().actions() == actions_before
    # the colleague resolves it: the watch closes and the customer hears once
    ReviewService(ActionStore(), CaseStore(), mock, load_policy()).resolve(
        handoff["handoff_id"], reviewer="Sam", note="reissued by hand")
    sim.advance(14)  # 08:00
    run(guard.run_once())
    assert (watch_of(iid)["state"], watch_of(iid)["closed_reason"]) == ("closed", "resolved_by_person")
    assert [n["kind"] for n in sent()] == ["refund_failed", "handoff_resolved"]


def test_parcel_progress_then_delivery_s02(mock, guard, sim):
    _, iid, _ = s02(mock)
    run(guard.run_once())
    assert watch_of(iid)["milestones"] == ["parcel_delivered"]
    sim.advance(14)  # 08:00 next day
    sim.add_event("parcel_scanned", target="ORD-S02-1", location="Columbus, OH")
    run(guard.run_once(customer_id="CUST-S02"))
    assert [n["kind"] for n in sent()] == ["parcel_scanned"] and "Columbus, OH" in sent()[0]["message"]
    sim.advance(4)
    sim.add_event("parcel_delivered", target="ORD-S02-1")
    run(guard.run_once(customer_id="CUST-S02"))
    assert [n["kind"] for n in sent()] == ["parcel_scanned", "parcel_delivered"]
    assert "UPS reports your navy Merino Crew Sweater as delivered" in sent()[1]["message"]
    assert watch_of(iid)["state"] == "closed"


def test_daily_limit_holds_updates_and_a_newer_one_replaces_a_waiting_one(mock, guard, sim):
    _, iid, _ = s02(mock)
    run(guard.run_once())
    sim.advance(14)  # 08:00
    for _ in range(4):
        sim.add_event("parcel_scanned", target="ORD-S02-1")
        run(guard.run_once(customer_id="CUST-S02"))
        sim.advance(1)
    statuses = [n["status"] for n in g.GuardianStore().notices()]
    assert statuses == ["sent", "sent", "superseded", "held"]
    sim.advance(24)
    run(guard.run_once())
    days = [n["sent_at"][:10] for n in sent()]
    assert max(days.count(d) for d in days) <= load_policy()["guardian"]["max_updates_per_day"]
    assert len(sent()) == 3


def test_quiet_hours_hold_the_closing_update_until_morning(mock, guard, sim):
    _, iid, _ = s02(mock)
    run(guard.run_once())
    sim.advance(4)  # 22:00
    sim.add_event("parcel_delivered", target="ORD-S02-1")
    run(guard.run_once(customer_id="CUST-S02"))
    assert watch_of(iid)["state"] == "resolved" and sent() == []
    assert g.GuardianStore().notices()[0]["held_reason"] == "quiet_hours"
    sim.advance(10)  # 08:00
    run(guard.run_once())
    (notice,) = sent()
    assert notice["sent_at"].endswith("T08:00:00Z") and watch_of(iid)["state"] == "closed"


def test_opt_out_suppresses_updates_but_not_hand_offs(mock, guard, sim):
    _, iid, _ = s01(mock)
    guard.set_opt_out("CUST-S01", True)
    run(guard.run_once())
    sim.add_event("refund_failed", target=S01_ITEM)
    run(guard.run_once(customer_id="CUST-S01"))
    assert outbox() == [] and g.GuardianStore().notices()[0]["status"] == "suppressed"
    assert CaseStore().handoffs()[0]["reason"] == "guardian_refund_failed"


def test_review_approved_then_refund_completes_s13(mock, guard, sim):
    sly, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    finish_turn(sly["case_id"], turn_start(), "A colleague will review it.")
    run(guard.run_once())
    assert watch_of(iid)["milestones"] == ["review_decided", "refund_completed"]
    svc = ReviewService(ActionStore(), CaseStore(), mock, load_policy())
    rid = next(i["review_id"] for i in svc.queue() if i.get("reference") == result["reference"])
    run(svc.approve(rid, reviewer="Sam Patel"))
    assert "approved and started" in svc.outbox()[0]["message"]  # not "completed": the money has not moved yet
    sim.advance(14)  # 08:00
    run(guard.run_once())
    w = watch_of(iid)
    assert (w["step"], w["state"]) == (1, "waiting_on_business") and sent() == []  # the review console told them
    sim.add_event("refund_completed", target="ITM-S13-1-1")
    run(guard.run_once(customer_id="CUST-S13"))
    assert [n["kind"] for n in sent()] == ["refund_completed"] and watch_of(iid)["state"] == "closed"


def test_review_rejected_closes_without_a_second_message(mock, guard, sim):
    sly, iid, result = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    finish_turn(sly["case_id"], turn_start(), "A colleague will review it.")
    svc = ReviewService(ActionStore(), CaseStore(), mock, load_policy())
    rid = next(i["review_id"] for i in svc.queue() if i.get("reference") == result["reference"])
    svc.reject(rid, reviewer="Sam", reason="already refunded by hand")
    run(guard.run_once())
    assert (watch_of(iid)["state"], watch_of(iid)["closed_reason"]) == ("closed", "review_rejected")
    assert sent() == [] and len(svc.outbox()) == 1


def test_review_waiting_too_long_is_escalated(mock, guard, sim):
    sly, iid, _ = queued(mock, "CUST-S13", "retrigger_refund", "ITM-S13-1-1")
    finish_turn(sly["case_id"], turn_start(), "A colleague will review it.")
    run(guard.run_once())
    sim.advance(49)
    run(guard.run_once())
    assert watch_of(iid)["state"] == "escalated" and [n["kind"] for n in sent()] == ["escalated"]


def test_hand_off_resolved_with_a_message_from_the_colleague_is_not_repeated(mock, guard, sim):
    sly = web(mock, "CUST-S10")
    since = turn_start()
    start(sly, {"domain": "order_delivery", "kind": "delivery_problem", "summary": "shoes not moving"})
    run(HandoffToHuman().async_invoke({"reason": "customer_requested", "summary": "wants a person"}, sly))
    finish_turn(sly["case_id"], since, "A colleague will follow up.")
    run(guard.run_once())
    (w,) = g.GuardianStore().watches()
    assert w["milestones"] == ["handoff_resolved"] and w["state"] == "waiting_on_business"
    ReviewService(ActionStore(), CaseStore(), mock, load_policy()).resolve(
        w["subject"]["handoff_id"], reviewer="Sam", note="called the customer", customer_message="We called you.")
    sim.advance(6)
    run(guard.run_once())
    assert g.GuardianStore().get(w["watch_id"])["state"] == "closed" and sent() == []


def test_data_unavailable_postpones_the_check_and_decides_nothing(mock, sim):
    class Down(SimulatedAdapter):
        async def get_customer_context(self, customer_id):
            raise ConnectionError("down")

    _, iid, _ = s01(mock)
    policy = {**load_policy(), "max_retries": 0, "retry_backoff_s": 0}
    summary = run(g.Guardian(adapter=Down(mock), simulator=sim, policy=policy).run_once())
    assert summary["postponed"] == [watch_of(iid)["watch_id"]] and watch_of(iid)["state"] == "watching"


# ---------------------------------------------------------------------------- wording and settings

PROMISES = re.compile(r"\b(within|by (tomorrow|monday|friday)|guarantee|will (arrive|be refunded|be delivered|be "
                      r"completed)|business days|hours)\b", re.I)


def test_templates_make_no_promises_of_time_or_outcome():
    for kind, text in g.TEMPLATES.items():
        assert not PROMISES.search(text), kind


def test_delay_thresholds_follow_the_stuck_rules():
    policy = load_policy()
    assert policy["guardian"]["delay_hours"] == {"refund_completed": policy["stuck_refund_days"] * 24,
                                                 "parcel_delivered": policy["stuck_shipment_days"] * 24}
    assert set(policy["guardian"]["sla_hours"]) == set(g.WAITING_WORDS)
