"""
Product recommendation engine (no LLM): rules on a small hand-built catalog, then the same engine over the mock
data (scenario customers and invariants across background customers), eligibility and failure handling.
"""

import asyncio
import json

import pytest

import coded_tools.customer_service.data_adapter as data_adapter
from coded_tools.customer_service import recommendations as rec
from coded_tools.customer_service.fault_injection import FaultInjectingAdapter
from coded_tools.customer_service.fault_injection import parse_faults
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.policy import load_policy

POLICY = {**load_policy(), "retry_backoff_s": 0.0, "max_retries": 0}
SETTINGS = POLICY["recommendations"]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_cache():
    rec._CATALOG_CACHE.clear()


# ---------------------------------------------------------------------------- a small catalog with full control

def variant(pid, color, size, stock=5):
    return {"sku": f"{pid}-{color}-{size or 'OS'}", "color": color, "size": size, "stock": stock}


def catalog(stock: dict | None = None, active: dict | None = None, copurchase=()) -> rec.Catalog:
    stock, active = stock or {}, active or {}
    products = {
        "CHINO": ("Slim Chino Pants", "pants", [("khaki", s) for s in ("30", "32", "34")] + [("navy", "32")]),
        "BELT": ("Leather Belt", "accessories", [("brown", "M"), ("brown", "L")]),
        "SHIRT": ("Oxford Shirt", "shirts", [("blue", s) for s in ("S", "M", "L")]),
        "CORD": ("Corduroy Trousers", "pants", [("brown", "32")]),
        "RELAX": ("Relaxed Chino Pants", "pants", [("stone", "32")]),
        "BOOT": ("Chelsea Boots", "footwear", [("brown", s) for s in ("9", "10")]),
        "TEE": ("Crew Tee", "shirts", [("white", "M")]),
    }
    rows = []
    for pid, (title, category, combos) in products.items():
        for color, size in combos:
            v = variant(pid, color, size, stock.get(f"{pid}-{color}-{size}", 5))
            rows.append({**v, "product_id": pid, "title": title, "category": category, "description": "",
                         "list_price": 50.0, "currency": "USD", "active": active.get(pid, pid != "CORD")})
    relations = [
        {"product_id": "CHINO", "related_product_id": r, "relation": kind, "strength": s}
        for r, kind, s in [("BELT", "complement", 0.9), ("SHIRT", "complement", 0.8), ("BOOT", "complement", 0.5),
                           ("RELAX", "alternative", 0.8), ("CORD", "alternative", 0.9)]
    ]
    return rec.build_catalog(rows, relations, list(copurchase))


def item(sku, n=1, status="delivered", reason=None, category=None, order_status="delivered"):
    pid, color, size = sku.split("-")
    cat = category or {"CHINO": "pants", "BELT": "accessories", "SHIRT": "shirts", "BOOT": "footwear",
                       "TEE": "shirts", "RELAX": "pants", "CORD": "pants"}[pid]
    return {"item_id": f"ITM-{n}", "order_id": f"ORD-{n}", "placed_on": f"2026-10-{20 - n:02d}",
            "order_status": order_status, "sku": sku, "title": pid, "category": cat, "color": color,
            "size": None if size == "OS" else size, "item_status": status,
            "return_reason": reason, "return_status": "received" if reason else None, "as_of": "2026-10-16"}


def suggested(result):
    return [(r["product_id"], r["reason"]) for r in result["recommendations"]]


def test_complements_ranked_by_weight_and_strength_capped_and_inactive_never_offered():
    out = rec.recommend(catalog(), [item("CHINO-khaki-32")], SETTINGS, anchor_item_id="ITM-1")
    # Scores: BELT 0.8*0.9 = .72, SHIRT 0.8*0.8 = .64, RELAX 0.6*0.8 = .48, BOOT 0.8*0.5 = .40 -> the top three.
    # CORD (alternative, 0.6*0.9 = .54) would rank third but is discontinued.
    assert suggested(out) == [("BELT", "complement"), ("SHIRT", "complement"), ("RELAX", "alternative")]
    assert out["recommendations"][0]["because"] == "Goes well with your Slim Chino Pants."
    assert out["status"] == "ok" and "Offer only these products" in out["message_hint"]


def test_sold_out_products_and_sizes_are_never_offered():
    sold_out = {"BELT-brown-M": 0, "BELT-brown-L": 0}
    out = rec.recommend(catalog(stock=sold_out), [item("CHINO-khaki-32")], SETTINGS, anchor_item_id="ITM-1")
    assert "BELT" not in [p for p, _ in suggested(out)]


def test_usual_size_limits_options_and_drops_a_product_sold_out_in_that_size():
    history = [item("CHINO-khaki-32", 1), item("TEE-white-M", 2)]  # kept a size M shirt
    out = rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1")
    shirt = next(r for r in out["recommendations"] if r["product_id"] == "SHIRT")
    assert shirt["in_your_usual_size"] == "M" and [o["size"] for o in shirt["available"]] == ["M"]
    out = rec.recommend(catalog(stock={"SHIRT-blue-M": 0}), history, SETTINGS, anchor_item_id="ITM-1")
    assert "SHIRT" not in [p for p, _ in suggested(out)]


def test_already_owned_products_are_not_suggested():
    history = [item("CHINO-khaki-32", 1), item("BELT-brown-M", 2)]
    assert "BELT" not in [p for p, _ in suggested(rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1"))]


def test_a_product_returned_as_not_as_described_is_never_suggested_again():
    history = [item("CHINO-khaki-32", 1), item("BELT-brown-M", 2, status="returned", reason="not_as_described")]
    assert "BELT" not in [p for p, _ in suggested(rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1"))]


def test_a_category_returned_too_often_is_skipped():
    history = [item("CHINO-khaki-32", 1), item("TEE-white-M", 2, status="returned", reason="changed_mind"),
               item("TEE-white-M", 3, status="returned", reason="changed_mind")]
    out = rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1")
    assert "SHIRT" not in [p for p, _ in suggested(out)]


def test_wrong_size_return_offers_the_neighbouring_sizes_in_the_same_colour_first():
    history = [item("CHINO-khaki-32", 1, status="returned", reason="wrong_size")]
    out = rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1")
    first = out["recommendations"][0]
    assert (first["product_id"], first["reason"]) == ("CHINO", "replacement_other_size")
    assert [(o["color"], o["size"]) for o in first["available"]] == [("khaki", "30"), ("khaki", "34")]
    assert "another size (30, 34)" in first["because"]
    # a returned item is not "yours": complements say "the"
    assert out["recommendations"][1]["because"] == "Goes well with the Slim Chino Pants."


def test_damaged_return_offers_the_same_item_only_if_in_stock():
    history = [item("CHINO-khaki-32", 1, status="returned", reason="damaged")]
    out = rec.recommend(catalog(), history, SETTINGS, anchor_item_id="ITM-1")
    assert suggested(out)[0] == ("CHINO", "replacement_same_item")
    assert out["recommendations"][0]["available"] == [{"sku": "CHINO-khaki-32", "color": "khaki", "size": "32"}]
    out = rec.recommend(catalog(stock={"CHINO-khaki-32": 0}), history, SETTINGS, anchor_item_id="ITM-1")
    assert ("CHINO", "replacement_same_item") not in suggested(out)


@pytest.mark.parametrize("together, with_product, offered", [(20, 100, True), (4, 10, False), (10, 100, False)])
def test_bought_together_needs_enough_orders_and_share(together, with_product, offered):
    cop = [{"product_id": "CHINO", "related_product_id": "TEE", "orders_together": together,
            "orders_with_product": with_product}]
    out = rec.recommend(catalog(copurchase=cop), [item("CHINO-khaki-32")], {**SETTINGS, "max_items": 10},
                        anchor_item_id="ITM-1")
    assert (("TEE", "bought_together") in suggested(out)) is offered


@pytest.mark.parametrize("kwargs, history, reason", [
    ({"anchor_item_id": "ITM-99"}, [item("CHINO-khaki-32")], "item_not_found"),
    ({"product_query": "umbrella"}, [item("CHINO-khaki-32")], "no_matching_product"),
    ({}, [], "no_purchase_history"),
    ({}, [item("CHINO-khaki-32", status="returned", reason="changed_mind")], "no_purchase_history"),
])
def test_no_anchor_means_no_suggestions(kwargs, history, reason):
    out = rec.recommend(catalog(), history, SETTINGS, **kwargs)
    assert (out["status"], out["reason"], out["recommendations"]) == ("no_anchor", reason, [])


def test_a_product_query_prefers_what_the_customer_bought():
    out = rec.recommend(catalog(), [item("CHINO-khaki-32")], SETTINGS, product_query="what goes with my chinos?")
    assert out["based_on"][0] == "Slim Chino Pants" and suggested(out)[0] == ("BELT", "complement")


@pytest.mark.parametrize("identity, auth, flags, with_person, code", [
    ("not_found", "strong", [], False, "not_identified"),
    ("verified", "weak", [], False, "sign_in_too_weak"),
    ("verified", "strong", [], True, "case_with_a_person"),
    ("verified", "strong", ["fraud_review"], False, "not_available"),
    ("verified", "medium", [], False, ""),
])
def test_eligibility(identity, auth, flags, with_person, code):
    assert rec.eligibility(identity, auth, flags, with_person, SETTINGS) == code


# ---------------------------------------------------------------------------- over the mock data

@pytest.fixture(scope="module")
def mock():
    return MockAdapter.from_env()


def recommend_for(adapter, customer_id, auth="strong", **kwargs):
    return run(rec.get_recommendations(adapter, POLICY, customer_id=customer_id, identity_status="verified",
                                       auth_level=auth, **kwargs))


def test_s01_wrong_size_puffer_gets_other_sizes_first(mock):
    out = recommend_for(mock, "CUST-S01", product_query="puffer jacket")
    first = out["recommendations"][0]
    assert (first["product_id"], first["reason"]) == ("PRD-CPJ", "replacement_other_size")
    assert {o["size"] for o in first["available"]} <= {"S", "L"} and first["available"][0]["color"] == "blue"


def test_s12_never_gets_the_belt_returned_as_not_as_described(mock):
    out = recommend_for(mock, "CUST-S12", product_query="belt")
    assert "PRD-LB" not in [r["product_id"] for r in out["recommendations"]]


def test_risk_flags_refuse_without_saying_why_s15(mock):
    out = recommend_for(mock, "CUST-S15", product_query="jacket")
    text = json.dumps(out).lower()
    assert out["status"] == "not_offered" and out["reason"] == "not_available"
    assert not any(w in text for w in ("risk", "fraud", "refund_rate", "chargeback"))


def test_weak_sign_in_gets_no_suggestions(mock):
    out = recommend_for(mock, "CUST-S10", auth="weak", product_query="shoes")
    assert (out["status"], out["reason"]) == ("not_offered", "sign_in_too_weak")


def test_suggestions_are_always_active_in_stock_not_owned_and_priced_from_the_catalog(mock):
    variants = run(mock.get_catalog_variants())
    by_sku = {v["sku"]: v for v in variants}
    customers = [c for c in sorted(mock.customers) if c.startswith("CUST-B")][:200]
    offered = 0
    for cid in customers:
        out = recommend_for(mock, cid)
        assert len(out["recommendations"]) <= SETTINGS["max_items"]
        history = run(mock.get_purchase_history(cid))
        kept_products = {by_sku[i["sku"]]["product_id"] for i in history
                         if i["item_status"] not in rec.NOT_KEPT and i["order_status"] != "cancelled"}
        never = {by_sku[i["sku"]]["product_id"] for i in history if i["return_reason"] == "not_as_described"}
        for r in out["recommendations"]:
            offered += 1
            assert r["product_id"] not in never
            if r["reason"] not in rec.REPLACEMENTS:
                assert r["product_id"] not in kept_products
            for o in r["available"]:
                v = by_sku[o["sku"]]
                assert v["stock"] > 0 and v["active"] and v["product_id"] == r["product_id"]
                assert r["list_price"] == v["list_price"]
            assert "stock" not in json.dumps(r["available"])
    assert offered > 300  # the rules leave plenty to suggest


def test_catalog_reads_are_cached(mock):
    calls = []

    class Counting(MockAdapter):
        def __init__(self, inner):
            self.__dict__.update(inner.__dict__)

        async def get_catalog_variants(self):
            calls.append(1)
            return await super().get_catalog_variants()

    counting = Counting(mock)
    recommend_for(counting, "CUST-B0001")
    recommend_for(counting, "CUST-B0002")
    assert len(calls) == 1


@pytest.mark.parametrize("spec", ["get_catalog_variants:error", "get_purchase_history:timeout",
                                  "get_policy_signals:permission"])
def test_failed_reads_suggest_nothing(mock, monkeypatch, spec):
    broken = FaultInjectingAdapter(mock, parse_faults(spec))
    monkeypatch.setattr(data_adapter, "_ADAPTER", broken)
    out = recommend_for(broken, "CUST-B0001")
    assert (out["status"], out["recommendations"]) == ("unavailable", [])
