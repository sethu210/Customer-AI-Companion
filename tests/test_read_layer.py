"""
Multi-agent Phase 1 tests: the read layer (identity, payments, loyalty, contact history, config), the typed
result envelope with bounded retries, and the policy-vs-data reference guard.

Always run (free, local):   mock adapter against the existing scenarios.
Opt-in (uses Databricks):   RUN_DATABRICKS_TESTS=1 compares every new read, mock vs Databricks (read-only SQL).
"""

import asyncio
import json
import os
import urllib.error
from pathlib import Path

import pytest
import yaml

from coded_tools.customer_service import results
from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.policy import DATA_REFERENCE_KEYS
from coded_tools.customer_service.policy import check_data_reference
from coded_tools.customer_service.policy import compare_data_reference
from coded_tools.customer_service.policy import load_policy

ROOT = Path(__file__).parents[1]
EXPECTATIONS = json.loads((ROOT / "test_data" / "out" / "scenario_expectations.json").read_text())
FAST = {"max_retries": 2, "retry_backoff_s": 0.0, "tool_timeout_s": 5}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


def identifier(mock: MockAdapter, customer_id: str, channel: str) -> str:
    return next(i["identifier"] for i in mock.identities if i["customer_id"] == customer_id and i["channel"] == channel)


# ---------------------------------------------------------------------------- identity

def test_identity_unique_match(mock):
    ident = identifier(mock, "CUST-S01", "web_session")
    rows = run(mock.resolve_channel_identity("web_session", ident))
    assert [(r["customer_id"], r["auth_level"], r["verified"]) for r in rows] == [("CUST-S01", "strong", True)]


def test_identity_email_is_case_and_space_insensitive(mock):
    ident = identifier(mock, "CUST-S09", "email")
    rows = run(mock.resolve_channel_identity("email", f"  {ident.upper()} "))
    assert [(r["customer_id"], r["auth_level"]) for r in rows] == [("CUST-S09", "medium")]


def test_identity_shared_phone_is_ambiguous(mock):
    rows = run(mock.resolve_channel_identity("phone", "+1-299-555-0150"))
    assert sorted(r["customer_id"] for r in rows) == ["CUST-S18A", "CUST-S18B"]
    assert {r["auth_level"] for r in rows} == {"weak"}


def test_identity_channel_must_match(mock):
    ident = identifier(mock, "CUST-S01", "web_session")
    assert run(mock.resolve_channel_identity("email", ident)) == []
    assert run(mock.resolve_channel_identity("phone", "")) == []


# ---------------------------------------------------------------------------- payments, loyalty, contacts

def test_payment_summary_failed_refund_s09(mock):
    rows = run(mock.get_payment_summary("CUST-S09"))
    assert len(rows) == 1
    r = rows[0]
    assert r["order_id"] == "ORD-S09-1" and r["payment_last4"] == "1881" and r["payments_on_order"] == 1
    assert r["refunds_failed_total"] == 68.0 and r["refunds_completed_total"] == 0.0 and r["refund_count"] == 1
    assert "method_token" not in r


def test_payment_summary_completed_refund_s03(mock):
    r = run(mock.get_payment_summary("CUST-S03"))[0]
    assert r["payment_last4"] == "4242" and r["refunds_completed_total"] > 0
    assert r["payment_status"] in ("refunded", "partially_refunded")


def test_every_order_has_exactly_one_payment(mock):
    # Documents the frozen-data limit: duplicate charges cannot appear, so billing claims go to review.
    for exp in EXPECTATIONS:
        for r in run(mock.get_payment_summary(exp["customer"])):
            assert r["payments_on_order"] == 1, r["order_id"]


def test_loyalty_profile_s01(mock):
    (r,) = run(mock.get_loyalty_profile("CUST-S01"))
    assert r["points"] == 2400 and r["loyalty_tier"] == r["customer_tier"] == "gold"
    assert r["last_adjustment_at"] is None and r["as_of"] == "2026-10-16"


def test_loyalty_unknown_customer_is_empty(mock):
    assert run(mock.get_loyalty_profile("CUST-NOPE")) == []


def test_contact_history_returns_injected_note_as_data(mock):
    (c,) = run(mock.get_contact_history("CUST-S20"))
    assert c["order_id"] == "ORD-S20-1" and c["channel"] == "email" and c["topic"] == "return question"
    assert c["notes"].startswith("SYSTEM OVERRIDE")  # returned verbatim; Phase 2 tools mark it untrusted


def test_contact_history_newest_first(mock):
    rows = run(mock.get_contact_history("CUST-S03"))
    assert rows and [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)


# ---------------------------------------------------------------------------- envelope and retries

class FlakyAdapter(MockAdapter):
    """Mock adapter whose get_loyalty_profile fails with the given exceptions before answering."""

    def __init__(self, base: MockAdapter, failures: list[BaseException]) -> None:  # noqa: D107 - share loaded data
        self.__dict__.update(base.__dict__)
        self.failures = list(failures)
        self.calls = 0

    async def get_loyalty_profile(self, customer_id: str):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return await super().get_loyalty_profile(customer_id)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "x", None, None)


def test_envelope_ok_and_empty(mock):
    ok = run(mock.fetch("get_loyalty_profile", "CUST-S01", policy=FAST))
    assert ok.status == results.OK and ok.rows and ok.attempts == 1 and not ok.failed
    empty = run(mock.fetch("get_contact_history", "CUST-S01", policy=FAST))
    assert empty.status == results.EMPTY and empty.rows == [] and not empty.failed


def test_transient_errors_are_retried_then_succeed(mock):
    flaky = FlakyAdapter(mock, [TimeoutError(), http_error(503)])
    result = run(flaky.fetch("get_loyalty_profile", "CUST-S01", policy=FAST))
    assert result.status == results.OK and result.attempts == 3 and flaky.calls == 3


def test_retries_are_bounded(mock):
    flaky = FlakyAdapter(mock, [TimeoutError()] * 10)
    result = run(flaky.fetch("get_loyalty_profile", "CUST-S01", policy=FAST))
    assert result.status == results.TIMEOUT and result.retryable and result.failed
    assert flaky.calls == 3  # 1 + max_retries


@pytest.mark.parametrize("exc, status", [
    (http_error(403), results.PERMISSION_DENIED),
    (http_error(400), results.BACKEND_ERROR),
    (RuntimeError("get_loyalty_profile failed: syntax"), results.BACKEND_ERROR),
])
def test_permanent_errors_are_not_retried(mock, exc, status):
    flaky = FlakyAdapter(mock, [exc])
    result = run(flaky.fetch("get_loyalty_profile", "CUST-S01", policy=FAST))
    assert result.status == status and not result.retryable and flaky.calls == 1


def test_slow_call_times_out(mock):
    class Slow(FlakyAdapter):
        async def get_loyalty_profile(self, customer_id: str):
            await asyncio.sleep(1)

    result = run(Slow(mock, []).fetch("get_loyalty_profile", "CUST-S01",
                                     policy={"max_retries": 0, "retry_backoff_s": 0, "tool_timeout_s": 0.05}))
    assert result.status == results.TIMEOUT


# ---------------------------------------------------------------------------- reference guard

def test_policy_reference_date_matches_generator_settings():
    settings = yaml.safe_load((ROOT / "test_data/generator/settings.yaml").read_text())
    assert load_policy()["reference_date"] == settings["reference_date"]


def test_reference_guard_passes_on_mock(mock):
    check = run(check_data_reference(mock, {**load_policy(), **FAST}))
    assert check["ok"], check["mismatched"]
    assert check["reference_date"] == "2026-10-16" and check["policy_version"]


@pytest.mark.parametrize("key", DATA_REFERENCE_KEYS)
def test_reference_guard_flags_each_mismatch(mock, key):
    data = {r["key"]: r["value"] for r in run(mock.get_reference_config())}
    data[key] = "2025-01-01" if key == "reference_date" else "999"
    check = compare_data_reference(load_policy(), data)
    assert not check["ok"] and [m["key"] for m in check["mismatched"]] == [key]


def test_reference_guard_blocks_when_config_unreadable(mock):
    class Broken(FlakyAdapter):
        async def get_reference_config(self):
            raise http_error(403)

    check = run(check_data_reference(Broken(mock, []), {**load_policy(), **FAST}))
    assert not check["ok"] and check["error"] == results.PERMISSION_DENIED


def test_catalog_variants_join_product_details(mock):
    rows = run(mock.get_catalog_variants())
    assert len(rows) == 358 and len({r["product_id"] for r in rows}) == 46
    overcoat = next(r for r in rows if r["sku"] == "SKU-WO-CAM-S")  # the size S camel overcoat S-scenarios use
    assert (overcoat["title"], overcoat["category"], overcoat["active"]) == ("Wool Overcoat", "outerwear", True)
    assert overcoat["list_price"] == 269.99 and overcoat["currency"] == "USD"
    staged = {r["product_id"] for r in run(mock.get_product_launches())}
    assert all(r["stock"] == 0 for r in rows if not r["active"] and r["product_id"] not in staged)  # discontinued
    assert any(r["stock"] for r in rows if r["product_id"] in staged)  # staged for launch: stock is ready
    assert [r["sku"] for r in rows] == sorted((r["sku"] for r in rows), key=lambda s: (
        next(x["product_id"] for x in rows if x["sku"] == s), s))


def test_stylist_data_reads(mock):
    """Companion plan P4: staged launches soonest first, style tags, colour families and harmony."""
    launches = run(mock.get_product_launches())
    assert [r["product_id"] for r in launches][:2] == ["PRD-CSC", "PRD-WFJ"]
    assert launches[0] == {"launch_id": "LNC-CSC", "product_id": "PRD-CSC", "planned_at": "2026-10-18T09:00:00Z",
                           "headline": "New in: pure cashmere scarves"}
    styles = run(mock.get_product_styles())
    assert {r["style"] for r in styles if r["product_id"] == "PRD-WO"} == {"smart", "classic"}
    families = {r["color"]: r["family"] for r in run(mock.get_colour_families())}
    assert (families["navy"], families["camel"], families["black"]) == ("blue", "earth", "dark_neutral")
    harmony = {(r["family"], r["other_family"]): r["strength"] for r in run(mock.get_colour_harmony())}
    assert len(harmony) == 21 and harmony[("blue", "earth")] == 0.9  # navy and camel: a classic pair


def test_product_relations_strongest_first(mock):
    rows = run(mock.get_product_relations())
    assert len(rows) == 84
    chinos = [(r["related_product_id"], r["relation"]) for r in rows if r["product_id"] == "PRD-SCP"]
    assert chinos[0] == ("PRD-LB", "complement") and ("PRD-CT", "alternative") in chinos


def test_adapter_interface_is_complete():
    missing = sorted(getattr(CustomerDataAdapter, "__abstractmethods__", set()) - set(vars(MockAdapter)))
    assert missing == []


# ---------------------------------------------------------------------------- mock vs Databricks parity

databricks = pytest.mark.skipif(os.environ.get("RUN_DATABRICKS_TESTS") != "1",
                                reason="set RUN_DATABRICKS_TESTS=1 to compare against Databricks")


def _canonical(rows):
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@databricks
def test_read_layer_mock_matches_databricks(mock):
    from coded_tools.customer_service.databricks_adapter import DatabricksAdapter

    remote = DatabricksAdapter.from_env()  # these reads use the Statement API only, never MCP

    async def compare() -> list[str]:
        customers = sorted({e["customer"] for e in EXPECTATIONS}
                           | {"CUST-S17B", "CUST-S18B", "CUST-S19B", "CUST-B0001", "CUST-B0042", "CUST-B0250"})
        calls = [("get_reference_config", ()), ("get_catalog_variants", ()), ("get_product_relations", ()),
                 ("get_product_copurchase", ()), ("get_product_launches", ()), ("get_product_styles", ()),
                 ("get_colour_families", ()), ("get_colour_harmony", ())]
        for cid in customers:
            calls += [(fn, (cid,)) for fn in ("get_payment_summary", "get_loyalty_profile", "get_contact_history",
                                              "get_purchase_history")]
            calls += [("resolve_channel_identity", (i["channel"], i["identifier"]))
                      for i in mock.identities if i["customer_id"] == cid]
        calls += [("resolve_channel_identity", ("phone", "+1-299-555-0150")),
                  ("resolve_channel_identity", ("email", "nobody@example.invalid"))]
        diffs = []
        for fn, fargs in calls:
            local, other = await getattr(mock, fn)(*fargs), await getattr(remote, fn)(*fargs)
            if _canonical(local) != _canonical(other):
                diffs.append(f"{fn}{fargs}: mock {local[:2]} vs databricks {other[:2]}")
        return diffs

    assert run(compare()) == []
