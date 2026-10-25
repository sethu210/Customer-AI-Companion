"""
Companion plan P5 (no LLM): Personal Stylist Mode. A launch scores the customers who switched Stylist Mode on;
an invite needs every gate passed, a score of at least 80 and an item they own to name. Invites share the customer's
daily limit with the Guardian's service updates (service first), wait for quiet hours to end, and expire.
Journeys J4-J8 of the plan.
"""

import asyncio

import pytest

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import messaging
from coded_tools.customer_service import recommendations
from coded_tools.customer_service import stylist as sty
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.guardian import OUTBOX
from coded_tools.customer_service.guardian import GuardianStore
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.results import TIMEOUT
from coded_tools.customer_service.results import ToolResult
from coded_tools.customer_service.simulator import Simulator
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401

POLICY = load_policy()
FAN = "CUST-B0196"  # keeps a navy wool overcoat: the cashmere scarf complements it
SCARF, POLO, LOAFERS = "PRD-CSC", "PRD-MPL", "PRD-SLF"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sim(monkeypatch, tmp_path):
    path = tmp_path / "simulator.json"
    monkeypatch.setenv("SIMULATOR_STATE", str(path))
    return Simulator(path)


@pytest.fixture()
def stylist(mock, sim):
    cp._DATA_CACHE.clear()
    cp._STYLE_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()
    return sty.Stylist(adapter=mock, simulator=sim)


def opt_in(*customers, on=True):
    for c in customers:
        cp.CompanionStore().set_consent(c, cp.STYLIST_MODE, on, channel="test")


def score_of(stylist, customer, product=SCARF):
    return next(r for r in stylist.store.scores(product_id=product) if r["customer_id"] == customer)


def stylist_outbox(customer=None):
    return [r for r in ActionStore().read(OUTBOX) if r.get("source") == "stylist"
            and (customer is None or r["customer_id"] == customer)]


def service_update(customer, sim, status="sent"):
    """A Guardian update for the customer on the simulator's clock (sent, or held waiting)."""
    store = GuardianStore()
    watch = {"watch_id": f"W-{customer}-{len(store.notices(customer_id=customer))}", "customer_id": customer,
             "case_id": "CASE-X"}
    notice = store.add_notice(dedupe=f"{watch['watch_id']}:test", watch=watch, kind="refund_completed", final=True,
                              message="Your refund has been completed.", now=sim.now_ts())
    store.set_notice(notice["notice_id"], status, sent_at=sim.now_ts() if status == "sent" else None)


# ---------------------------------------------------------------------------- J4: a strong match is invited


def test_a_strong_match_is_invited_with_the_owned_item_named(stylist):
    opt_in(FAN)
    summary = run(stylist.launch(SCARF))
    assert summary["candidates"] == 1 and summary["invited"][0]["customer_id"] == FAN
    result = score_of(stylist, FAN)
    b = result["breakdown"]
    assert result["outcome"] == "invite" and result["score"] >= 80 and b["owned_item"] == "navy wool overcoat"
    assert round(sum(b["points"].values()) - b["penalty"]) == result["score"]  # the breakdown adds up
    (sent,) = stylist_outbox(FAN)
    assert sent["message"].startswith("New in at ACME Retail: the Cashmere Scarf, $87.99. I picked it for you "
                                      "because it goes with your navy wool overcoat")
    assert "Not for you? Just say so" in sent["message"]
    thread = cp.CompanionStore().thread(FAN, since=None)  # the invite is in the customer's thread
    assert thread[-1]["role"] == "update" and thread[-1]["source"] == "stylist"


def test_no_invite_below_80_or_without_an_owned_item(stylist, mock):
    opt_in(*sorted(mock.customers)[:120])
    run(stylist.launch(SCARF))
    rows = stylist.store.scores(product_id=SCARF)
    invited = {i["customer_id"] for i in stylist.store.invites()}
    assert invited and all(r["score"] >= 80 and r["breakdown"]["owned_item"] for r in rows
                           if r["customer_id"] in invited)
    assert {r["outcome"] for r in rows} >= {"invite", "below_threshold"}
    assert all(r["customer_id"] not in invited for r in rows if r["outcome"] != "invite")


# ---------------------------------------------------------------------------- J5: no consent, nothing sent


def test_without_stylist_mode_nothing_is_scored_or_sent(stylist):
    assert run(stylist.launch(SCARF))["candidates"] == 0 and stylist_outbox() == []
    opt_in(FAN)
    opt_in(FAN, on=False)  # switched off again: the latest choice counts
    assert cp.CompanionStore().customers_with(cp.STYLIST_MODE, POLICY["companion"]) == []


def test_switching_off_while_an_invite_waits_suppresses_it(stylist, sim):
    opt_in(FAN)
    sim.advance(4)  # 22:00: quiet hours
    run(stylist.launch(SCARF))
    assert stylist.store.invites()[0]["held_reason"] == "quiet_hours"
    opt_in(FAN, on=False)
    sim.advance(10)
    assert run(stylist.run_once())["suppressed"] and stylist_outbox() == []


# ---------------------------------------------------------------------------- J6: service before selling


def test_an_open_case_with_a_person_blocks_invites(stylist):
    opt_in(FAN)
    case, _ = CaseStore().open_case(customer_id=FAN, channel="web_session", auth_level="strong",
                                    identity_status="verified", policy_version="t", reference_date="2026-10-16")
    CaseStore().add_handoff(case["case_id"], [], "customer_requested", {})
    run(stylist.launch(SCARF))
    result = score_of(stylist, FAN)
    assert result["outcome"] == "gated" and "open_trouble" in result["breakdown"]["gates"]
    assert stylist_outbox() == []


# ---------------------------------------------------------------------------- J7: one daily budget, service first


def test_a_service_update_sent_today_holds_the_invite_until_tomorrow(stylist, sim):
    opt_in(FAN)
    service_update(FAN, sim)
    run(stylist.launch(SCARF))
    (invite,) = stylist.store.invites()
    assert invite["status"] == "held" and invite["held_reason"] == "daily_limit"  # one slot stays for service
    sim.advance(16)  # 10:00 the next day
    assert run(stylist.run_once())["sent"] == [invite["invite_id"]]


def test_a_waiting_service_update_goes_first(stylist, sim):
    opt_in(FAN)
    service_update(FAN, sim, status="held")
    run(stylist.launch(SCARF))
    assert stylist.store.invites()[0]["held_reason"] == "service_first"


def test_invites_count_toward_the_guardians_daily_limit(stylist, sim):
    opt_in(FAN)
    run(stylist.launch(SCARF))  # invite sent: 1 of 2 today
    path = CaseStore().path
    assert messaging.service_hold_reason(path, FAN, sim.now(), POLICY) is None  # a service update still fits
    service_update(FAN, sim)
    assert messaging.service_hold_reason(path, FAN, sim.now(), POLICY) == "daily_limit"


def test_quiet_hours_then_sent_in_the_morning_and_expiry(stylist, sim):
    opt_in(FAN)
    sim.advance(4)
    run(stylist.launch(SCARF))
    sim.advance(10)  # 08:00
    assert len(run(stylist.run_once())["sent"]) == 1
    # A second customer (polo score 82) whose invite waits behind a service update that never goes out: dropped
    # after invite_valid_days.
    opt_in("CUST-B0013")
    service_update("CUST-B0013", sim, status="held")
    run(stylist.launch(POLO))
    (waiting,) = stylist.store.invites(customer_id="CUST-B0013")
    assert waiting["held_reason"] == "service_first"
    sim.advance(24 * 2)
    assert run(stylist.run_once())["expired"] == []  # still waiting
    sim.advance(24 * 2)
    assert run(stylist.run_once())["expired"] == [waiting["invite_id"]]


# ---------------------------------------------------------------------------- J8: "not for me" lowers similar picks


def test_not_for_me_lowers_the_score_of_similar_products(stylist, mock):
    data = run(stylist._data())
    facts = run(stylist._facts(FAN, stylist.clock.now()))
    common = {"month": 10, "skip_category_after_returns": 2}
    before = sty.score(facts, SCARF, data, POLICY["stylist"], **common)
    facts.not_for_me = ["PRD-MWS"]  # the merino scarf: same category, two shared styles (cosy, classic)
    after = sty.score(facts, SCARF, data, POLICY["stylist"], **common)
    assert before["outcome"] == "invite" and after["penalty"] == 40
    assert after["score"] == before["score"] - 40 and after["outcome"] == "below_threshold"


def test_feedback_is_only_for_a_pick_that_was_sent(stylist):
    with pytest.raises(ValueError):
        stylist.feedback(FAN, SCARF, "not_for_me")
    opt_in(FAN)
    run(stylist.launch(SCARF))
    stylist.feedback(FAN, SCARF, "not_for_me")
    assert stylist.store.feedback(FAN)[0]["kind"] == "not_for_me"


# ---------------------------------------------------------------------------- gates


def test_one_invite_a_week_and_never_twice_for_a_product(stylist):
    opt_in(FAN)
    run(stylist.launch(SCARF))
    again = run(stylist.launch(SCARF))
    assert again["already_launched"] and len(stylist.store.invites()) == 1
    run(stylist.launch(POLO))
    assert "weekly_limit" in score_of(stylist, FAN, POLO)["breakdown"]["gates"]


def test_stated_avoids_and_size_gates(stylist, mock):
    store = cp.CompanionStore()
    opt_in(FAN)
    p = store.propose_memory(FAN, "avoids", "cashmere", case_id="C", issue_id="I", turn_id="T", channel="web",
                             pending_hours=1)
    store.confirm_memory(FAN, p["memory_id"], keep_days=30)
    run(stylist.launch(SCARF))
    assert score_of(stylist, FAN)["breakdown"]["gates"] == ["avoided"]
    data, facts = run(stylist._data()), run(stylist._facts("CUST-B0013", stylist.clock.now()))
    assert not any(i["category"] == "footwear" for i in facts.kept)  # no shoe size to go on
    loafers = sty.score(facts, LOAFERS, data, POLICY["stylist"], month=10, skip_category_after_returns=2)
    assert loafers["gates"] == ["size_unknown"]
    facts.memories = [{"kind": "size", "value": "footwear 10"}]  # a stated, confirmed size makes it known
    stated = sty.score(facts, LOAFERS, data, POLICY["stylist"], month=10, skip_category_after_returns=2)
    assert stated["gates"] == [] and stated["size"] == "10" and stated["outcome"] == "invite"


def test_risk_flags_and_failed_reads_never_invite(stylist, mock, monkeypatch):
    opt_in("CUST-S15", FAN)  # S15 has risk flags: never said why
    real = mock.fetch

    async def fetch(fn, *args, policy=None):
        if fn == "get_purchase_history" and args and args[0] == FAN:
            return ToolResult(status=TIMEOUT)
        return await real(fn, *args, policy=policy)

    monkeypatch.setattr(mock, "fetch", fetch)
    summary = run(stylist.launch(SCARF))
    assert summary["data_unavailable"] == [FAN] and summary["invited"] == []
    assert score_of(stylist, "CUST-S15")["breakdown"]["gates"][0] == "not_available"


def test_only_staged_products_can_be_launched(stylist):
    with pytest.raises(ValueError):
        run(stylist.launch("PRD-WO"))
    staged = run(stylist.staged())
    assert [s["product_id"] for s in staged][0] == SCARF and staged[0]["launched_at"] is None
