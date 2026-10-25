"""
Deterministic test-data generator for the zero-question service agent.

Builds all tables from hand-written scenarios (test_data/scenarios/*.yaml) plus seeded background
customers, then writes Parquet (for Databricks) and CSV (for humans) to test_data/out/.

Usage (from the project root):
    python -m test_data.generator.generate [--reference-date YYYY-MM-DD] [--background N]
"""

import argparse
import csv
import hashlib
import json
import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from faker import Faker

from test_data.generator.catalog import build_catalog

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "schema" / "tables.yaml"
SETTINGS_FILE = ROOT / "generator" / "settings.yaml"
SCENARIO_DIR = ROOT / "scenarios"
OUT_DIR = ROOT / "out"

# Fixed hour-of-day per event kind keeps same-day events in a logical order and output deterministic.
HOUR = {
    "placed": 8,
    "label_created": 10,
    "picked_up": 12,
    "in_transit": 14,
    "out_for_delivery": 9,
    "delivered": 16,
    "exception": 18,
    "return_label": 10,
    "warehouse_scan": 13,
    "refund_initiated": 15,
    "refund_completed": 17,
    "contact": 11,
    "created": 9,
}

SHIPMENT_STATUS_FROM_EVENT = {
    "label_created": "label_created",
    "picked_up": "in_transit",
    "in_transit": "in_transit",
    "out_for_delivery": "out_for_delivery",
    "delivered": "delivered",
    "exception": "exception",
}

CARRIERS = ["UPS", "FedEx", "USPS", "DHL"]
CITIES = ["Memphis, TN", "Louisville, KY", "Columbus, OH", "Reno, NV", "Atlanta, GA", "Dallas, TX"]

# (title, category, colors, sizes, min_price, max_price)
CATALOG = [
    ("Classic Puffer Jacket", "outerwear", ["blue", "black", "green"], ["S", "M", "L", "XL"], 110, 160),
    ("Rain Shell Jacket", "outerwear", ["blue", "yellow", "grey"], ["S", "M", "L"], 80, 120),
    ("Wool Overcoat", "outerwear", ["camel", "navy", "black"], ["M", "L"], 220, 320),
    ("Merino Crew Sweater", "knitwear", ["grey", "navy", "red"], ["S", "M", "L", "XL"], 70, 95),
    ("Slim Chino Pants", "pants", ["khaki", "navy", "olive"], ["30", "32", "34", "36"], 45, 70),
    ("Trail Running Shoes", "footwear", ["black", "blue", "orange"], ["8", "9", "10", "11"], 95, 140),
    ("Leather Belt", "accessories", ["brown", "black"], ["M", "L"], 30, 45),
    ("Canvas Tote Bag", "accessories", ["natural", "black"], [None], 25, 40),
    ("Oxford Shirt", "shirts", ["white", "blue", "pink"], ["S", "M", "L", "XL"], 40, 65),
    ("Thermal Beanie", "accessories", ["grey", "red", "black"], [None], 18, 28),
]


class DataBuilder:
    """Accumulates rows for every table while keeping IDs and timestamps consistent."""

    def __init__(self, reference: datetime, settings: dict[str, Any], rng: random.Random, fake: Faker) -> None:
        self.reference = reference
        self.settings = settings
        self.rng = rng
        self.fake = fake
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.phone_counter = 0
        self.expectations: list[dict[str, Any]] = []

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.tables.setdefault(table, []).append(row)

    def at(self, days_ago: float, kind: str) -> datetime:
        day = self.reference - timedelta(days=int(days_ago))
        return day.replace(hour=HOUR[kind], minute=0, second=0, microsecond=0)

    def next_phone(self) -> str:
        # 555-0100..0199 is the NANP range reserved for fiction; vary the area code for uniqueness.
        area = 201 + self.phone_counter // 100
        number = self.phone_counter % 100
        self.phone_counter += 1
        return f"+1-{area}-555-01{number:02d}"

    def identifier_for(self, channel: str, customer_id: str, full_name: str) -> str:
        if channel == "web_session":
            return f"sess_{self.rng.getrandbits(64):016x}"
        if channel == "email":
            words = ("".join(ch for ch in w if ch.isalnum()) for w in full_name.lower().split())
            local = ".".join(w for w in words if w)
            return f"{local}.{customer_id.lower()}@example.com"
        return self.next_phone()

    # ------------------------------------------------------------------ customers

    def add_customer(self, spec: dict[str, Any]) -> str:
        cid = spec["id"]
        self.add(
            "customers",
            {
                "customer_id": cid,
                "full_name": spec["name"],
                "tier": spec.get("tier", "standard"),
                "created_at": self.at(spec.get("created_days_ago", 400), "created"),
                "risk_flags": ";".join(spec.get("risk_flags", [])) or None,
            },
        )
        for n, ident in enumerate(spec.get("identities", [{"channel": "web_session"}]), start=1):
            channel = ident["channel"]
            default_level = {"web_session": "strong", "email": "medium", "phone": "weak"}[channel]
            self.add(
                "channel_identities",
                {
                    "identity_id": f"IDN-{cid}-{n}",
                    "customer_id": cid,
                    "channel": channel,
                    "identifier": ident.get("identifier") or self.identifier_for(channel, cid, spec["name"]),
                    "verified": ident.get("verified", True),
                    "auth_level": ident.get("auth_level", default_level),
                },
            )
        self.add(
            "loyalty",
            {
                "customer_id": cid,
                "points": spec.get("loyalty_points", 0),
                "tier": spec.get("tier", "standard"),
                "last_adjustment_at": None,
            },
        )
        for o_num, order in enumerate(spec.get("orders", []), start=1):
            self.add_order(cid, f"{cid.removeprefix('CUST-')}-{o_num}", order)
        for c_num, contact in enumerate(spec.get("support_contacts", []), start=1):
            order_ref = contact.get("order")
            self.add(
                "support_contacts",
                {
                    "contact_id": f"CON-{cid.removeprefix('CUST-')}-{c_num}",
                    "customer_id": cid,
                    "order_id": f"ORD-{cid.removeprefix('CUST-')}-{order_ref}" if order_ref else None,
                    "channel": contact.get("channel", "chat"),
                    "topic": contact["topic"],
                    "notes": contact.get("notes"),
                    "created_at": self.at(contact["days_ago"], "contact"),
                    "resolution": contact.get("resolution"),
                },
            )
        return cid

    # ------------------------------------------------------------------ orders

    def add_order(self, cid: str, oref: str, spec: dict[str, Any]) -> None:
        order_id = f"ORD-{oref}"
        cancelled = spec.get("cancelled", False)
        item_ids: dict[str, str] = {}
        item_rows: list[dict[str, Any]] = []

        for i_num, item in enumerate(spec["items"], start=1):
            item_id = f"ITM-{oref}-{i_num}"
            item_ids[item.get("key", str(i_num))] = item_id
            slug = "".join(w[0] for w in item["title"].split()).upper()
            row = {
                "item_id": item_id,
                "order_id": order_id,
                "shipment_id": None,
                "sku": f"SKU-{slug}-{item['color'][:3].upper()}-{item.get('size') or 'OS'}",
                "title": item["title"],
                "color": item["color"],
                "category": item["category"],
                "size": item.get("size"),
                "qty": item.get("qty", 1),
                "price": float(item["price"]),
                "item_status": "cancelled" if cancelled else "processing",
            }
            item_rows.append(row)
            self.add("order_items", row)
        items_by_id = {r["item_id"]: r for r in item_rows}

        shipment_statuses: list[str] = []
        for s_num, shp in enumerate(spec.get("shipments", []), start=1):
            shipment_id = f"SHP-{oref}-{s_num}"
            events = shp["events"]
            for e_num, (etype, days_ago) in enumerate(events, start=1):
                self.add(
                    "shipment_events",
                    {
                        "event_id": f"EVT-{oref}-{s_num}-{e_num}",
                        "shipment_id": shipment_id,
                        "event_type": etype,
                        "location": shp.get("location") or self.rng.choice(CITIES),
                        "event_at": self.at(days_ago, etype),
                    },
                )
            status = SHIPMENT_STATUS_FROM_EVENT[events[-1][0]]
            shipment_statuses.append(status)
            self.add(
                "shipments",
                {
                    "shipment_id": shipment_id,
                    "order_id": order_id,
                    "carrier": shp.get("carrier") or self.rng.choice(CARRIERS),
                    "tracking_no": f"TRK{self.rng.getrandbits(40):012d}",
                    "status": status,
                },
            )
            for key in shp.get("items", list(item_ids)):
                row = items_by_id[item_ids[key]]
                row["shipment_id"] = shipment_id
                if status == "delivered":
                    row["item_status"] = "delivered"
                elif status != "label_created":
                    row["item_status"] = "shipped"

        refunded_total = 0.0
        for r_num, ret in enumerate(spec.get("returns", []), start=1):
            return_id = f"RET-{oref}-{r_num}"
            item = items_by_id[item_ids[ret["item"]]]
            scanned = ret.get("warehouse_scanned_days_ago")
            status = ret.get("status") or ("received" if scanned is not None else "label_created")
            item["item_status"] = "returned" if status in ("received", "inspected") else "return_requested"
            self.add(
                "returns",
                {
                    "return_id": return_id,
                    "item_id": item["item_id"],
                    "reason": ret.get("reason", "changed_mind"),
                    "status": status,
                    "label_created_at": self.at(ret["label_created_days_ago"], "return_label"),
                    "warehouse_scanned_at": self.at(scanned, "warehouse_scan") if scanned is not None else None,
                },
            )
            refund = ret.get("refund")
            if refund:
                amount = float(refund.get("amount", item["price"] * item["qty"]))
                completed = refund.get("completed_days_ago")
                self.add(
                    "refunds",
                    {
                        "refund_id": f"RFD-{oref}-{r_num}",
                        "order_id": order_id,
                        "return_id": return_id,
                        "amount": amount,
                        "method": refund.get("method", "original_payment"),
                        "status": refund["status"],
                        "initiated_at": self.at(refund["initiated_days_ago"], "refund_initiated"),
                        "completed_at": self.at(completed, "refund_completed") if completed is not None else None,
                        "failure_reason": refund.get("failure_reason"),
                    },
                )
                if refund["status"] == "completed":
                    refunded_total += amount

        total = round(sum(r["price"] * r["qty"] for r in item_rows), 2)
        if cancelled:
            order_status = "cancelled"
        elif shipment_statuses and all(s == "delivered" for s in shipment_statuses) and all(
            r["shipment_id"] for r in item_rows
        ):
            order_status = "delivered"
        elif shipment_statuses and any(s != "label_created" for s in shipment_statuses):
            order_status = "shipped" if all(r["shipment_id"] for r in item_rows) else "partially_shipped"
        else:
            order_status = "processing"

        self.add(
            "orders",
            {
                "order_id": order_id,
                "customer_id": cid,
                "placed_at": self.at(spec["placed_days_ago"], "placed"),
                "status": order_status,
                "total": total,
                "currency": self.settings["currency"],
            },
        )
        if refunded_total == 0:
            pay_status = "authorized" if cancelled or order_status == "processing" else "captured"
        else:
            pay_status = "refunded" if refunded_total >= total else "partially_refunded"
        payment = spec.get("payment", {})
        self.add(
            "payments",
            {
                "payment_id": f"PAY-{oref}",
                "order_id": order_id,
                "method_token": payment.get("token") or f"tok_{self.rng.getrandbits(48):012x}",
                "last4": payment.get("last4") or f"{self.rng.randint(0, 9999):04d}",
                "status": pay_status,
            },
        )

    # ------------------------------------------------------------------ background

    def add_background(self, count: int) -> None:
        for n in range(1, count + 1):
            cid = f"CUST-B{n:04d}"
            name = self.fake.name()
            orders = []
            for _ in range(self.rng.randint(1, 5)):
                orders.append(self.background_order())
            identities = [{"channel": "web_session"}, {"channel": "email"}]
            if self.rng.random() < 0.6:
                identities.append({"channel": "phone"})
            self.add_customer(
                {
                    "id": cid,
                    "name": name,
                    "tier": self.rng.choices(["standard", "silver", "gold", "platinum"], [60, 25, 12, 3])[0],
                    "created_days_ago": self.rng.randint(370, 2000),
                    "identities": identities,
                    "loyalty_points": self.rng.randint(0, 5000),
                    "orders": orders,
                }
            )

    def background_order(self) -> dict[str, Any]:
        placed = self.rng.randint(1, 365)
        items = []
        for k in range(self.rng.randint(1, 3)):
            title, category, colors, sizes, lo, hi = self.rng.choice(CATALOG)
            items.append(
                {
                    "key": str(k + 1),
                    "title": title,
                    "category": category,
                    "color": self.rng.choice(colors),
                    "size": self.rng.choice(sizes),
                    "price": round(self.rng.uniform(lo, hi), 2),
                }
            )
        order: dict[str, Any] = {"placed_days_ago": placed, "items": items}
        if placed > 3 and self.rng.random() < 0.03:
            order["cancelled"] = True
            return order

        # Healthy timeline relative to placement; only events already in the past are kept,
        # and an order in flight never sits idle long enough to look stuck.
        timeline = [("label_created", 1), ("picked_up", 1), ("in_transit", 2), ("out_for_delivery", 4), ("delivered", 4)]
        events = [(e, placed - d) for e, d in timeline if placed - d >= 0]
        if not events:
            return order
        order["shipments"] = [{"events": events}]

        delivered_days_ago = placed - 4
        if events[-1][0] == "delivered" and delivered_days_ago >= 10:
            returns = []
            for item in items:
                if self.rng.random() < 0.08:
                    label = delivered_days_ago - 3
                    scanned = delivered_days_ago - 7
                    returns.append(
                        {
                            "item": item["key"],
                            "reason": self.rng.choice(["wrong_size", "damaged", "not_as_described", "changed_mind"]),
                            "label_created_days_ago": label,
                            "warehouse_scanned_days_ago": scanned,
                            "status": "inspected",
                            "refund": {
                                "status": "completed",
                                "initiated_days_ago": scanned,
                                "completed_days_ago": max(scanned - 2, 0),
                            },
                        }
                    )
            if returns:
                order["returns"] = returns
        return order


def load_scenarios() -> list[dict[str, Any]]:
    scenarios = []
    for path in sorted(SCENARIO_DIR.glob("s*.yaml")):
        with path.open(encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        scenario["_file"] = path.name
        scenarios.append(scenario)
    ids = [s["id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate scenario ids: {ids}")
    return scenarios


def arrow_type(col_type: str) -> pa.DataType:
    return {
        "string": pa.string(),
        "int": pa.int64(),
        "double": pa.float64(),
        "bool": pa.bool_(),
        "timestamp": pa.timestamp("us", tz="UTC"),
    }[col_type]


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def write_outputs(tables: dict[str, list[dict[str, Any]]], schema: dict[str, Any]) -> dict[str, Any]:
    (OUT_DIR / "parquet").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "csv").mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    for name, spec in schema["tables"].items():
        columns = spec["columns"]
        rows = sorted(tables.get(name, []), key=lambda r: str(r[spec["primary_key"]]))
        arrow_schema = pa.schema([pa.field(c, arrow_type(d["type"]), nullable=True) for c, d in columns.items()])
        table = pa.Table.from_pylist([{c: r.get(c) for c in columns} for r in rows], schema=arrow_schema)
        parquet_path = OUT_DIR / "parquet" / f"{name}.parquet"
        pq.write_table(table, parquet_path, compression="snappy")

        csv_path = OUT_DIR / "csv" / f"{name}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for r in rows:
                writer.writerow([csv_value(r.get(c)) for c in columns])

        summary[name] = {
            "rows": len(rows),
            "parquet_sha256": hashlib.sha256(parquet_path.read_bytes()).hexdigest(),
        }
    return summary


def build(reference_date: str, background: int) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict]:
    with SETTINGS_FILE.open(encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    reference = datetime.fromisoformat(reference_date).replace(tzinfo=timezone.utc)

    rng = random.Random(settings["seed"])
    fake = Faker("en_US")
    fake.seed_instance(settings["seed"])
    builder = DataBuilder(reference, settings, rng, fake)

    scenarios = load_scenarios()
    for scenario in scenarios:
        for customer in scenario["customers"]:
            builder.add_customer(customer)
    builder.add_background(background)

    config_rows = {"reference_date": reference_date, "seed": str(settings["seed"])}
    config_rows.update({k: str(v) for k, v in settings["policy"].items()})
    builder.tables["config"] = [{"key": k, "value": v} for k, v in config_rows.items()]
    # Its own random generator: the tables above are unaffected. Launch times are relative to the reference date.
    builder.tables.update(build_catalog(settings, reference))
    return builder.tables, scenarios, settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    with SETTINGS_FILE.open(encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    parser.add_argument("--reference-date", default=settings["reference_date"])
    parser.add_argument("--background", type=int, default=settings["background_customers"])
    args = parser.parse_args()

    with SCHEMA_FILE.open(encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    tables, scenarios, settings = build(args.reference_date, args.background)
    summary = write_outputs(tables, schema)

    expectations = [
        {"id": s["id"], "title": s["title"], "phase": s["phase"], "file": s["_file"], **s["expected"]}
        for s in scenarios
    ]
    (OUT_DIR / "scenario_expectations.json").write_text(json.dumps(expectations, indent=2), encoding="utf-8")
    manifest = {
        "seed": settings["seed"],
        "reference_date": args.reference_date,
        "background_customers": args.background,
        "scenarios": [s["id"] for s in scenarios],
        "tables": summary,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total = sum(t["rows"] for t in summary.values())
    print(f"Generated {len(summary)} tables, {total} rows, {len(scenarios)} scenarios -> {OUT_DIR}")
    for name, info in summary.items():
        print(f"  {name:<20} {info['rows']:>6}")


if __name__ == "__main__":
    main()
