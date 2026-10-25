"""
Phase D tests for the customer-data layer.

Always run (free, local):   mock adapter vs scenario expectations.
Opt-in (uses Databricks):   set RUN_DATABRICKS_TESTS=1 plus the DATABRICKS_* variables (see databricks_adapter.py)
                            to compare all five functions, mock vs Databricks, for every scenario customer.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from coded_tools.customer_service.mock_adapter import MockAdapter

EXPECTATIONS = json.loads((Path(__file__).parents[1] / "test_data" / "out" / "scenario_expectations.json").read_text())
# How the agent would describe the item in each scenario (S16/S18 are about the whole order).
ITEM_QUERIES = {
    "S01": "blue jacket", "S02": "sweater", "S03": "shirt", "S04": "rain jacket", "S05": "blue jacket",
    "S06": "jacket", "S07": "running shoes", "S08": "running shoes", "S09": "red sweater", "S10": "shoes",
    "S11": "chinos", "S12": "belt", "S13": "overcoat", "S14": "grey rain jacket", "S15": "shirt",
    "S17": "blue jacket", "S19": "wool overcoat", "S20": "sweater",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


@pytest.mark.parametrize("exp", EXPECTATIONS, ids=[e["id"] for e in EXPECTATIONS])
def test_mock_stuck_cases_match_scenario(mock, exp):
    got = sorted({r["diagnosis"] for r in run(mock.get_stuck_cases(exp["customer"]))})
    assert got == sorted(exp.get("diagnosis", []))


@pytest.mark.parametrize("sid", sorted(ITEM_QUERIES))
def test_mock_find_order_items_match_scenario(mock, sid):
    exp = next(e for e in EXPECTATIONS if e["id"] == sid)
    got = sorted(r["item_id"] for r in run(mock.find_order_items(exp["customer"], ITEM_QUERIES[sid])))
    assert got == sorted(exp["matching_items"])


# ---------------------------------------------------------------------------- mock vs Databricks parity

databricks = pytest.mark.skipif(os.environ.get("RUN_DATABRICKS_TESTS") != "1",
                                reason="set RUN_DATABRICKS_TESTS=1 to compare against Databricks")


def _canonical(rows):
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@databricks
def test_mock_matches_databricks(mock):
    from coded_tools.customer_service.databricks_adapter import DatabricksAdapter

    remote = DatabricksAdapter.from_env()

    async def compare() -> list[str]:
        diffs = []
        customers = sorted({e["customer"] for e in EXPECTATIONS} | {"CUST-B0001", "CUST-B0042", "CUST-B0250"})
        for cid in customers:
            orders = {r["order_id"] for r in await mock.get_customer_context(cid)}
            calls = [("get_customer_profile", (cid,)), ("get_customer_context", (cid,)),
                     ("get_stuck_cases", (cid,)), ("find_order_items", (cid, "blue jacket")),
                     ("get_policy_signals", (cid,))]
            calls += [("get_case_timeline", (cid, oid)) for oid in sorted(orders)[:2]]
            for fn, fargs in calls:
                local = await getattr(mock, fn)(*fargs)
                other = await getattr(remote, fn)(*fargs, prefer_sql=True)
                if _canonical(local) != _canonical(other):
                    diffs.append(f"{fn}{fargs}: mock {len(local)} rows vs databricks {len(other)} rows")
        return diffs

    assert run(compare()) == []
