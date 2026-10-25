"""
Companion plan P9 (offline): the journey evaluation's own rules. The journeys themselves run against the agent server
(python -m evaluation.journey_eval); here: the journey list, the promise pattern and the invariants every journey
is checked for, on hand-made records.
"""

from datetime import datetime
from datetime import timezone

import pytest

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.stylist import StylistStore
from evaluation import journey_eval as je
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401

DAY = datetime(2026, 10, 17, tzinfo=timezone.utc)


def ctx(journey_id: str = "J4") -> je.Ctx:
    return je.Ctx(next(j for j in je.JOURNEYS if j.id == journey_id), 1, False)


def invite(customer: str, product: str, score: int, *, hour: int = 10, day: int = 0, owned: str | None = "navy coat",
           outcome: str = "invite") -> None:
    store = StylistStore()
    at = DAY.replace(hour=hour, day=DAY.day + day)
    store.save_score({"product_id": product, "customer_id": customer, "score": score, "outcome": outcome,
                      "owned_item": owned}, at.strftime("%Y-%m-%dT%H:%M:%SZ"))
    row = store.add_invite(customer, product, score, "New in.", at, 3)
    store.set_invite(row["invite_id"], "sent", sent_at=at.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_ten_journeys_on_customers_that_exist():
    customers = MockAdapter.from_env().customers
    assert [j.id for j in je.JOURNEYS] == [f"J{n}" for n in range(1, 11)]
    assert all(j.customer in customers for j in je.JOURNEYS)
    assert {j.id for j in je.JOURNEYS if j.memory} == {"J2", "J3"}


@pytest.mark.parametrize("text, promised", [
    ("Its price is within what you usually spend.", False),
    ("Your refund will arrive within 3 days.", True),
    ("We'll sort it within a few business days.", True),
    ("Your refund will be completed soon.", True),
    ("The refund of $129.00 has been completed.", False),
])
def test_a_promise_is_a_time_or_an_outcome(text, promised):
    assert bool(je.PROMISES.search(text)) is promised


def test_invariants_hold_for_a_clean_record():
    invite("CUST-B0196", "PRD-CSC", 92)
    assert all(je.invariants(ctx()).values())


def test_an_invite_below_80_or_without_an_owned_item_breaks_the_rule():
    invite("CUST-B0196", "PRD-CSC", 79)
    assert je.invariants(ctx())["invites"] is False
    invite("CUST-B0025", "PRD-CSC", 90, owned=None)
    assert je.invariants(ctx())["invites"] is False


def test_a_message_in_quiet_hours_breaks_the_limits():
    invite("CUST-B0196", "PRD-CSC", 92, hour=22)
    assert je.invariants(ctx())["limits"] is False


def test_more_messages_a_day_than_the_limit_break_the_limits():
    for n, product in enumerate(("PRD-CSC", "PRD-MPL", "PRD-SLF")):
        invite(f"CUST-B019{n}", product, 92)  # one each: within every limit
    assert je.invariants(ctx())["limits"] is True
    for hour, product in ((9, "PRD-CSC"), (11, "PRD-MPL"), (12, "PRD-SLF")):
        invite("CUST-B0196", product, 92, hour=hour)
    c = ctx()
    assert je.invariants(c)["limits"] is False and c.facts["invariant_detail"]["max_per_day"] == 3


def test_two_invites_in_a_week_break_the_limits():
    invite("CUST-B0196", "PRD-CSC", 92)
    invite("CUST-B0196", "PRD-MPL", 90, day=3)
    rules = je.invariants(ctx())
    assert rules["limits"] is False and rules["invites"] is True


def test_memory_only_in_journeys_that_ask_for_it():
    store = cp.CompanionStore()
    pending = store.propose_memory("CUST-B0196", "likes", "navy", case_id=None, issue_id=None, turn_id=None,
                                   channel="web", pending_hours=24)
    store.confirm_memory("CUST-B0196", pending["memory_id"], keep_days=30)
    assert je.invariants(ctx("J4"))["memory"] is False
    j2 = ctx("J2")
    j2.customer = "CUST-B0196"
    assert je.invariants(j2)["memory"] is True


def test_a_promise_in_a_reply_breaks_the_rule():
    c = ctx()
    c.answers.append("Your refund will be completed by tomorrow.")
    assert je.invariants(c)["promises"] is False
