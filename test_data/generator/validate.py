"""
Integrity checks over the generated output in test_data/out/parquet.

Prints a summary only (never the data). Exit code 1 if any check fails.

Usage (from the project root):
    python -m test_data.generator.validate
"""

import json
import sys
from collections import Counter
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import pyarrow.parquet as pq
import yaml

from test_data.generator.generate import OUT_DIR
from test_data.generator.generate import SCHEMA_FILE
from test_data.generator.generate import load_scenarios
from test_data.generator.rules import diagnose_customer


def load_tables(schema: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {name: pq.read_table(OUT_DIR / "parquet" / f"{name}.parquet").to_pylist() for name in schema["tables"]}


def check_schema(tables: dict, schema: dict) -> list[str]:
    errors = []
    for name, spec in schema["tables"].items():
        rows = tables[name]
        pk = spec["primary_key"]
        dupes = [k for k, n in Counter(r[pk] for r in rows).items() if n > 1]
        if dupes:
            errors.append(f"{name}: duplicate {pk}: {dupes[:5]}")
        for col, cdef in spec["columns"].items():
            values = [r[col] for r in rows]
            if not cdef.get("nullable") and any(v is None for v in values):
                errors.append(f"{name}.{col}: unexpected nulls")
            if "enum" in cdef:
                allowed = set(schema["enums"][cdef["enum"]])
                bad = {v for v in values if v is not None and v not in allowed}
                if bad:
                    errors.append(f"{name}.{col}: values outside enum {cdef['enum']}: {sorted(bad)}")
            if "fk" in cdef:
                ref_table, ref_col = cdef["fk"].split(".")
                targets = {r[ref_col] for r in tables[ref_table]}
                missing = {v for v in values if v is not None and v not in targets}
                if missing:
                    errors.append(f"{name}.{col}: {len(missing)} dangling refs to {cdef['fk']}, e.g. {sorted(missing)[:3]}")
    return errors


def check_timelines(tables: dict, reference: datetime) -> list[str]:
    errors = []
    end_of_reference_day = reference + timedelta(days=1)
    orders = {o["order_id"]: o for o in tables["orders"]}
    customers = {c["customer_id"]: c for c in tables["customers"]}
    shipments = {s["shipment_id"]: s for s in tables["shipments"]}
    items = {i["item_id"]: i for i in tables["order_items"]}

    for name, table in tables.items():
        if name == "product_launches":
            continue  # planned launches are in the future by design (check_stylist)
        for row in table:
            for col, value in row.items():
                if isinstance(value, datetime) and value >= end_of_reference_day:
                    errors.append(f"{col}={value} is after the reference date")

    for o in orders.values():
        if o["placed_at"] < customers[o["customer_id"]]["created_at"]:
            errors.append(f"{o['order_id']}: placed before customer was created")

    events_by_shipment: dict[str, list[dict]] = {}
    for e in sorted(tables["shipment_events"], key=lambda e: e["event_id"]):
        events_by_shipment.setdefault(e["shipment_id"], []).append(e)
    delivered_at: dict[str, datetime] = {}
    for sid, events in events_by_shipment.items():
        times = [e["event_at"] for e in events]
        if times != sorted(times):
            errors.append(f"{sid}: shipment events out of order")
        if times[0] < orders[shipments[sid]["order_id"]]["placed_at"]:
            errors.append(f"{sid}: shipment event before order was placed")
        for e in events:
            if e["event_type"] == "delivered":
                delivered_at[sid] = e["event_at"]
    for sid, s in shipments.items():
        if sid not in events_by_shipment:
            errors.append(f"{sid}: shipment has no events")

    for r in tables["returns"]:
        item = items[r["item_id"]]
        shipped_on = delivered_at.get(item["shipment_id"])
        if shipped_on and r["label_created_at"] < shipped_on:
            errors.append(f"{r['return_id']}: return label before delivery")
        if r["warehouse_scanned_at"] and r["warehouse_scanned_at"] < r["label_created_at"]:
            errors.append(f"{r['return_id']}: scanned before label created")
        if r["status"] in ("received", "inspected") and r["warehouse_scanned_at"] is None:
            errors.append(f"{r['return_id']}: status {r['status']} but never scanned")

    for f in tables["refunds"]:
        if f["initiated_at"] < orders[f["order_id"]]["placed_at"]:
            errors.append(f"{f['refund_id']}: refund before order was placed")
        if f["completed_at"] and f["completed_at"] < f["initiated_at"]:
            errors.append(f"{f['refund_id']}: completed before initiated")
        if (f["status"] == "completed") != (f["completed_at"] is not None):
            errors.append(f"{f['refund_id']}: status/completed_at mismatch")
        if (f["status"] == "failed") != (f["failure_reason"] is not None):
            errors.append(f"{f['refund_id']}: status/failure_reason mismatch")
    return errors


def check_identities(tables: dict, scenarios: list[dict]) -> list[str]:
    explicit = {
        ident["identifier"]
        for s in scenarios
        for c in s["customers"]
        for ident in c.get("identities", [])
        if ident.get("identifier")
    }
    counts = Counter((i["channel"], i["identifier"]) for i in tables["channel_identities"])
    return [
        f"identifier {ident} ({channel}) shared by {n} customers but not declared in a scenario"
        for (channel, ident), n in counts.items()
        if n > 1 and ident not in explicit
    ]


def check_scenarios(tables: dict, scenarios: list[dict], reference: datetime, policy: dict) -> list[str]:
    errors = []
    identities = tables["channel_identities"]
    for s in scenarios:
        exp = s["expected"]
        cid = exp["customer"]
        if not any(i["customer_id"] == cid and i["channel"] == exp["channel"] for i in identities):
            errors.append(f"{s['id']}: customer {cid} has no {exp['channel']} identity")
        actual = diagnose_customer(cid, tables, reference, policy)
        expected = sorted(exp.get("diagnosis", []))
        if actual != expected:
            errors.append(f"{s['id']}: diagnosis expected {expected}, data gives {actual}")
        item_ids = {i["item_id"] for i in tables["order_items"]}
        for item_id in exp.get("matching_items", []):
            if item_id not in item_ids:
                errors.append(f"{s['id']}: expected matching item {item_id} does not exist")
    return errors


def check_catalog(tables: dict) -> list[str]:
    """The product catalog agrees with what was ordered and is usable for recommendations."""
    errors = []
    products = {p["product_id"]: p for p in tables["products"]}
    variants = {v["sku"]: v for v in tables["product_variants"]}
    for item in tables["order_items"]:
        v = variants.get(item["sku"])
        if v is None:
            continue  # reported by check_schema as a dangling reference
        p = products[v["product_id"]]
        if (p["title"], p["category"], v["color"], v["size"]) != (item["title"], item["category"], item["color"],
                                                                   item["size"]):
            errors.append(f"{item['item_id']}: order item differs from catalog variant {item['sku']}")
    combos = Counter((v["product_id"], v["color"], v["size"]) for v in tables["product_variants"])
    errors += [f"{c}: duplicate variant" for c, n in combos.items() if n > 1]
    staged = {r["product_id"] for r in tables.get("product_launches", [])}
    for pid, p in products.items():
        mine = [v for v in variants.values() if v["product_id"] == pid]
        if not mine:
            errors.append(f"{pid}: product without variants")
        if not p["active"] and pid not in staged and any(v["stock"] for v in mine):
            errors.append(f"{pid}: discontinued product still has stock")
        if p["list_price"] <= 0:
            errors.append(f"{pid}: list price {p['list_price']}")
    errors += [f"{v['sku']}: negative stock" for v in variants.values() if v["stock"] < 0]
    pairs = Counter((r["product_id"], r["related_product_id"], r["relation"]) for r in tables["product_relations"])
    errors += [f"{k}: duplicate relation" for k, n in pairs.items() if n > 1]
    for r in tables["product_relations"]:
        if r["product_id"] == r["related_product_id"]:
            errors.append(f"{r['relation_id']}: product related to itself")
        if not 0 < r["strength"] <= 1:
            errors.append(f"{r['relation_id']}: strength {r['strength']} outside (0, 1]")
    # Later phases test that sold-out variants and discontinued products are never recommended.
    if not any(v["stock"] == 0 and products[v["product_id"]]["active"] for v in variants.values()):
        errors.append("catalog has no sold-out variant of an active product")
    if all(p["active"] for p in products.values()):
        errors.append("catalog has no discontinued product")
    return errors


def check_stylist(tables: dict[str, list[dict]], reference: datetime) -> list[str]:
    """Stylist data (companion plan P4): staged launches, style tags, colour families and their harmony."""
    errors = []
    products = {p["product_id"]: p for p in tables["products"]}
    launches = tables["product_launches"]
    for pid, n in Counter(r["product_id"] for r in launches).items():
        if n > 1:
            errors.append(f"{pid}: launched more than once")
    for r in launches:
        p = products.get(r["product_id"])
        if p is None:
            continue  # dangling reference: check_schema reports it
        if p["active"]:
            errors.append(f"{r['launch_id']}: {r['product_id']} is already on sale (a staged product is inactive)")
        if not any(v["stock"] for v in tables["product_variants"] if v["product_id"] == r["product_id"]):
            errors.append(f"{r['launch_id']}: no stock to launch")
        if r["planned_at"] <= reference:
            errors.append(f"{r['launch_id']}: planned at {r['planned_at']}, not after the reference date")
        related = [x for x in tables["product_relations"] if x["product_id"] == r["product_id"]
                   and x["relation"] == "complement" and products.get(x["related_product_id"], {}).get("active")]
        if not related:
            errors.append(f"{r['launch_id']}: no complement relation to a product on sale (wardrobe signal)")
    tags: dict[str, list[str]] = {}
    for r in tables["product_styles"]:
        tags.setdefault(r["product_id"], []).append(r["style"])
    for pid in products:
        mine = tags.get(pid, [])
        if not 1 <= len(mine) <= 3 or len(set(mine)) != len(mine):
            errors.append(f"{pid}: style tags {mine} (1-3 different tags needed)")
    families = {r["color"]: r["family"] for r in tables["colour_families"]}
    used = {v["color"] for v in tables["product_variants"]}
    errors += [f"colour {c}: no colour family" for c in sorted(used - set(families))]
    errors += [f"colour {c}: in colour_families but on no variant" for c in sorted(set(families) - used)]
    pairs = Counter((r["family"], r["other_family"]) for r in tables["colour_harmony"])
    for r in tables["colour_harmony"]:
        if r["family"] > r["other_family"]:
            errors.append(f"{r['harmony_id']}: write each pair once, family <= other_family")
        if not 0 <= r["strength"] <= 1:
            errors.append(f"{r['harmony_id']}: strength {r['strength']} outside [0, 1]")
    names = sorted({r["family"] for r in tables["colour_families"]})
    for i, a in enumerate(names):
        for b in names[i:]:
            if pairs[(a, b)] != 1:
                errors.append(f"colour_harmony: {pairs[(a, b)]} rows for {a} + {b} (exactly 1 needed)")
    return errors


def run() -> tuple[list[str], dict[str, Any]]:
    with SCHEMA_FILE.open(encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    tables = load_tables(schema)
    manifest = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    config = {r["key"]: r["value"] for r in tables["config"]}
    reference = datetime.fromisoformat(config["reference_date"]).replace(tzinfo=timezone.utc)
    policy = {k: float(v) if "." in v else int(v) for k, v in config.items() if k not in ("reference_date", "seed")}
    scenarios = load_scenarios()

    errors: list[str] = []
    for name, info in manifest["tables"].items():
        if len(tables[name]) != info["rows"]:
            errors.append(f"{name}: {len(tables[name])} rows, manifest says {info['rows']}")
    errors += check_schema(tables, schema)
    errors += check_timelines(tables, reference)
    errors += check_identities(tables, scenarios)
    errors += check_scenarios(tables, scenarios, reference, policy)
    errors += check_catalog(tables)
    errors += check_stylist(tables, reference)
    stats = {"tables": len(tables), "rows": sum(len(t) for t in tables.values()), "scenarios": len(scenarios)}
    return errors, stats


def main() -> None:
    errors, stats = run()
    print(f"Checked {stats['tables']} tables, {stats['rows']} rows, {stats['scenarios']} scenarios")
    for e in errors[:50]:
        print(f"  FAIL {e}")
    if len(errors) > 50:
        print(f"  ... and {len(errors) - 50} more")
    print("OK" if not errors else f"{len(errors)} problem(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
