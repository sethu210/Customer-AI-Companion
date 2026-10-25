"""
Phase D tests for the customer-data layer.

Always run (free, local):   mock adapter vs scenario expectations; identity guard on the coded tools.
Opt-in (uses Databricks):   set RUN_DATABRICKS_TESTS=1 plus the DATABRICKS_* variables (see databricks_adapter.py)
                            to compare all five functions, mock vs Databricks, for every scenario customer.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from coded_tools.customer_service.customer_tools import OVERVIEW_CACHE
from coded_tools.customer_service.customer_tools import CustomerOverview
from coded_tools.customer_service.customer_tools import FindOrderItems
from coded_tools.customer_service.customer_tools import GetOrderTimeline
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


@pytest.fixture(autouse=True)
def use_mock_backend(monkeypatch, mock):
    import coded_tools.customer_service.data_adapter as data_adapter

    monkeypatch.setattr(data_adapter, "_ADAPTER", mock)


@pytest.mark.parametrize("exp", EXPECTATIONS, ids=[e["id"] for e in EXPECTATIONS])
def test_mock_stuck_cases_match_scenario(mock, exp):
    got = sorted({r["diagnosis"] for r in run(mock.get_stuck_cases(exp["customer"]))})
    assert got == sorted(exp.get("diagnosis", []))


@pytest.mark.parametrize("sid", sorted(ITEM_QUERIES))
def test_mock_find_order_items_match_scenario(mock, sid):
    exp = next(e for e in EXPECTATIONS if e["id"] == sid)
    got = sorted(r["item_id"] for r in run(mock.find_order_items(exp["customer"], ITEM_QUERIES[sid])))
    assert got == sorted(exp["matching_items"])


def test_tools_refuse_without_identity():
    for tool, args in ((CustomerOverview(), {}), (FindOrderItems(), {"item_query": "jacket"}),
                       (GetOrderTimeline(), {"order_id": "ORD-S01-1"})):
        result = run(tool.async_invoke(args, {}))
        assert isinstance(result, str) and result.startswith("Error: identity_unavailable")


def test_customer_id_in_tool_args_is_ignored():
    # Session belongs to CUST-S17A; the model tries to look up the other Alex Morgan.
    result = run(FindOrderItems().async_invoke({"item_query": "blue jacket", "customer_id": "CUST-S17B"},
                                               {"customer_id": "CUST-S17A"}))
    assert [i["item_id"] for i in result["items"]] == ["ITM-S17A-1-1"]


def test_other_customers_order_is_not_found():
    result = run(GetOrderTimeline().async_invoke({"order_id": "ORD-S19B-1"}, {"customer_id": "CUST-S19A"}))
    assert result.startswith("Error:") and "not found" in result


def test_overview_s01_headline_case_and_cache():
    sly = {"customer_id": "CUST-S01"}
    overview = run(CustomerOverview().async_invoke({}, sly))
    jacket = next(i for i in overview["items"] if i["item_id"] == "ITM-S01-1-1")
    assert jacket["return_warehouse_scanned_at"] == "2026-10-14T13:00:00Z"
    assert jacket["refund_status"] == "pending" and jacket["refund_amount"] == 129.0
    assert overview["stuck_cases"] == [] and overview["as_of"] == "2026-10-16"
    assert "risk_flags" not in json.dumps(overview)
    assert sly[OVERVIEW_CACHE]["customer_id"] == "CUST-S01"
    assert run(CustomerOverview().async_invoke({}, sly)) is overview  # served from sly_data cache


def test_find_reports_ambiguity_and_no_match():
    sly5 = {"customer_id": "CUST-S05"}
    assert run(FindOrderItems().async_invoke({"item_query": "blue jacket"}, sly5))["result"] == "ambiguous"
    sly7 = {"customer_id": "CUST-S07"}
    assert run(FindOrderItems().async_invoke({"item_query": "running shoes"}, sly7))["result"] == "no_match"


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
