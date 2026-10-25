import copy
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
import yaml

from test_data.generator.generate import SCHEMA_FILE
from test_data.generator.generate import load_scenarios
from test_data.generator.rules import diagnose_customer
from test_data.generator.validate import check_catalog
from test_data.generator.validate import check_scenarios
from test_data.generator.validate import check_schema
from test_data.generator.validate import check_stylist
from test_data.generator.validate import check_timelines
from test_data.generator.validate import load_tables
from test_data.generator.validate import run


@pytest.fixture(scope="module")
def data():
    with SCHEMA_FILE.open(encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    tables = load_tables(schema)
    config = {r["key"]: r["value"] for r in tables["config"]}
    reference = datetime.fromisoformat(config["reference_date"]).replace(tzinfo=timezone.utc)
    policy = {k: float(v) if "." in v else int(v) for k, v in config.items() if k not in ("reference_date", "seed")}
    return schema, tables, reference, policy


def test_generated_output_is_valid():
    errors, stats = run()
    assert errors == []
    assert stats["tables"] == 19  # 12 customer-service tables + 3 product catalog + 4 stylist tables (P4)
    assert stats["scenarios"] == 20


def test_dangling_foreign_key_is_caught(data):
    schema, tables, _, _ = data
    broken = copy.deepcopy(tables)
    broken["orders"][0]["customer_id"] = "CUST-DOES-NOT-EXIST"
    assert any("dangling" in e for e in check_schema(broken, schema))


def test_bad_enum_is_caught(data):
    schema, tables, _, _ = data
    broken = copy.deepcopy(tables)
    broken["refunds"][0]["status"] = "lost"
    assert any("outside enum" in e for e in check_schema(broken, schema))


def test_refund_before_order_is_caught(data):
    _, tables, reference, _ = data
    broken = copy.deepcopy(tables)
    refund = broken["refunds"][0]
    order = next(o for o in broken["orders"] if o["order_id"] == refund["order_id"])
    refund["initiated_at"] = order["placed_at"] - timedelta(days=1)
    assert any("refund before order" in e for e in check_timelines(broken, reference))


def test_wrong_scenario_diagnosis_is_caught(data):
    _, tables, reference, policy = data
    scenarios = copy.deepcopy(load_scenarios())
    s08 = next(s for s in scenarios if s["id"] == "S08")
    s08["expected"]["diagnosis"] = []
    assert any(e.startswith("S08: diagnosis") for e in check_scenarios(tables, scenarios, reference, policy))


# ---------------------------------------------------------------------------- product catalog


def test_every_ordered_sku_is_a_catalog_variant(data):
    schema, tables, _, _ = data
    broken = copy.deepcopy(tables)
    broken["order_items"][0]["sku"] = "SKU-NOPE-BLU-M"
    assert any("order_items.sku" in e and "dangling" in e for e in check_schema(broken, schema))


def test_order_item_must_match_its_catalog_variant(data):
    _, tables, _, _ = data
    broken = copy.deepcopy(tables)
    broken["order_items"][0]["title"] = "Something Else"
    assert any("differs from catalog variant" in e for e in check_catalog(broken))


@pytest.mark.parametrize("break_it, message", [
    (lambda t: t["product_variants"].append({**t["product_variants"][0], "sku": "SKU-COPY"}), "duplicate variant"),
    (lambda t: next(v for v in t["product_variants"] if v["product_id"] == "PRD-CT").update(stock=3),
     "discontinued product still has stock"),
    (lambda t: t["product_relations"][0].update(related_product_id=t["product_relations"][0]["product_id"]),
     "related to itself"),
    (lambda t: t["product_relations"][0].update(strength=1.5), "outside (0, 1]"),
])
def test_catalog_problems_are_caught(data, break_it, message):
    _, tables, _, _ = data
    broken = copy.deepcopy(tables)
    break_it(broken)
    assert any(message in e for e in check_catalog(broken))


def test_stylist_data_is_valid(data):
    _, tables, reference, _ = data
    assert check_stylist(tables, reference) == []


@pytest.mark.parametrize("break_it, message", [
    (lambda t: next(p for p in t["products"] if p["product_id"] == "PRD-CSC").update(active=True),
     "already on sale"),
    (lambda t: t["product_launches"][0].update(planned_at=t["product_launches"][0]["planned_at"].replace(year=2020)),
     "not after the reference date"),
    (lambda t: t["product_launches"].append({**t["product_launches"][0], "launch_id": "LNC-COPY"}),
     "launched more than once"),
    (lambda t: t["product_relations"].__setitem__(slice(None), [r for r in t["product_relations"]
                                                                if r["product_id"] != "PRD-TFJ"]),
     "no complement relation"),
    (lambda t: t["product_styles"].__setitem__(slice(None), [r for r in t["product_styles"]
                                                             if r["product_id"] != "PRD-WO"]),
     "style tags []"),
    (lambda t: t["colour_families"].__setitem__(slice(None), [r for r in t["colour_families"]
                                                              if r["color"] != "navy"]),
     "colour navy: no colour family"),
    (lambda t: t["colour_harmony"].pop(), "exactly 1 needed"),
    (lambda t: t["colour_harmony"][0].update(family="warm", other_family="blue"), "family <= other_family"),
])
def test_stylist_problems_are_caught(data, break_it, message):
    _, tables, reference, _ = data
    broken = copy.deepcopy(tables)
    break_it(broken)
    assert any(message in e for e in check_stylist(broken, reference))


def test_staged_products_may_have_stock_but_discontinued_ones_may_not(data):
    _, tables, _, _ = data
    assert check_catalog(tables) == []
    broken = copy.deepcopy(tables)
    broken["product_launches"] = [r for r in broken["product_launches"] if r["product_id"] != "PRD-CSC"]
    assert any("PRD-CSC: discontinued product still has stock" in e for e in check_catalog(broken))


def test_catalog_does_not_change_the_customer_service_data(data):
    """The catalog has its own random generator: rebuilding without it gives the same customer-service rows."""
    from test_data.generator import generate

    real = generate.build_catalog
    generate.build_catalog = lambda settings, reference=None: {}
    try:
        without, _, settings = generate.build(settings_reference_date(), 500)
    finally:
        generate.build_catalog = real
    with_catalog, _, _ = generate.build(settings_reference_date(), 500)
    assert {k: v for k, v in with_catalog.items() if k in without} == without


def settings_reference_date() -> str:
    with (SCHEMA_FILE.parents[1] / "generator" / "settings.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)["reference_date"]


def test_background_customers_have_no_stuck_cases(data):
    _, tables, reference, policy = data
    background = [c["customer_id"] for c in tables["customers"] if c["customer_id"].startswith("CUST-B")]
    stuck = [cid for cid in background if diagnose_customer(cid, tables, reference, policy)]
    assert stuck == []
