"""
In-memory adapter over the generated test data (test_data/out/parquet).

Mirrors test_data/databricks/uc_functions.sql (and the gold layer it reads) so both backends answer identically;
tests/test_customer_data.py checks that. Free and fast: no Databricks compute is used.

Environment:
    CUSTOMER_DATA_MOCK_DIR  directory of <table>.parquet files (default: <project>/test_data/out/parquet)
"""

import os
import re
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.data_adapter import Rows
from coded_tools.customer_service.schemas import normalise_rows

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = PROJECT_ROOT / "test_data" / "out" / "parquet"
TABLES = [
    "customers", "channel_identities", "orders", "shipments", "order_items", "shipment_events",
    "returns", "refunds", "payments", "loyalty", "support_contacts", "config",
    "products", "product_variants", "product_relations",
    "product_launches", "product_styles", "colour_families", "colour_harmony",
]
STOPWORDS = {"the", "and", "for", "my", "where", "what", "refund", "order", "return", "item", "one"}


def _num(value: float) -> str:
    """Format a double the way Spark's concat() does for the values in this dataset (e.g. 129.0, 59.99)."""
    return repr(float(value))


class MockAdapter(CustomerDataAdapter):
    name = "mock"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        t = {name: pq.read_table(data_dir / f"{name}.parquet").to_pylist() for name in TABLES}
        config = {r["key"]: r["value"] for r in t["config"]}
        self.config = config
        self.identities = t["channel_identities"]
        self.as_of = datetime.fromisoformat(config["reference_date"]).date()
        self.reference = datetime.fromisoformat(config["reference_date"]).replace(tzinfo=timezone.utc)
        self.window_days = int(config["return_window_days"])
        self.stuck_refund_days = int(config["stuck_refund_days"])
        self.stuck_shipment_days = int(config["stuck_shipment_days"])

        self.customers = {c["customer_id"]: c for c in t["customers"]}
        self.loyalty = {r["customer_id"]: r for r in t["loyalty"]}
        self.orders = {o["order_id"]: o for o in t["orders"]}
        self.shipments = {s["shipment_id"]: s for s in t["shipments"]}
        self.payments = {p["order_id"]: p for p in t["payments"]}
        self.payments_by_order: dict[str, list[dict]] = defaultdict(list)
        for p in t["payments"]:
            self.payments_by_order[p["order_id"]].append(p)
        self.orders_by_customer: dict[str, list[dict]] = defaultdict(list)
        for o in t["orders"]:
            self.orders_by_customer[o["customer_id"]].append(o)
        self.items_by_order: dict[str, list[dict]] = defaultdict(list)
        for i in t["order_items"]:
            self.items_by_order[i["order_id"]].append(i)
        self.events_by_shipment: dict[str, list[dict]] = defaultdict(list)
        for e in t["shipment_events"]:
            self.events_by_shipment[e["shipment_id"]].append(e)
        self.returns_by_item: dict[str, list[dict]] = defaultdict(list)
        for r in t["returns"]:
            self.returns_by_item[r["item_id"]].append(r)
        self.refunds_by_return: dict[str, list[dict]] = defaultdict(list)
        self.refunds_by_order: dict[str, list[dict]] = defaultdict(list)
        for f in t["refunds"]:
            self.refunds_by_order[f["order_id"]].append(f)
            if f["return_id"]:
                self.refunds_by_return[f["return_id"]].append(f)
        self.contacts_by_customer: dict[str, list[dict]] = defaultdict(list)
        for c in t["support_contacts"]:
            self.contacts_by_customer[c["customer_id"]].append(c)
        self.products = {p["product_id"]: p for p in t["products"]}
        self.product_variants = t["product_variants"]
        self.product_relations = t["product_relations"]
        self.stylist = {name: t[name] for name in ("product_launches", "product_styles", "colour_families",
                                                   "colour_harmony")}

    @classmethod
    def from_env(cls) -> "MockAdapter":
        return cls(Path(os.environ.get("CUSTOMER_DATA_MOCK_DIR", DEFAULT_DIR)))

    # ------------------------------------------------------------------ helpers (gold layer equivalents)

    def _enriched_items(self, customer_id: str) -> list[dict[str, Any]]:
        """gold.order_items_enriched for one customer."""
        out = []
        for o in self.orders_by_customer.get(customer_id, []):
            for i in self.items_by_order[o["order_id"]]:
                s = self.shipments.get(i["shipment_id"]) if i["shipment_id"] else None
                text = " ".join(v for v in (i["color"], i["title"], i["category"]) if v is not None).lower()
                out.append({**i, "customer_id": customer_id, "placed_at": o["placed_at"],
                            "order_status": o["status"], "currency": o["currency"],
                            "carrier": s["carrier"] if s else None, "tracking_no": s["tracking_no"] if s else None,
                            "shipment_status": s["status"] if s else None, "search_text": text})
        return out

    def _last_event(self, shipment_id: str | None) -> dict[str, Any]:
        events = self.events_by_shipment.get(shipment_id or "", [])
        if not events:
            return {}
        last = max(events, key=lambda e: e["event_at"])
        delivered = [e["event_at"] for e in events if e["event_type"] == "delivered"]
        return {"last_event": last["event_type"], "last_location": last["location"],
                "last_event_at": last["event_at"], "delivered_at": max(delivered) if delivered else None}

    # ------------------------------------------------------------------ the five functions

    async def get_customer_profile(self, customer_id: str) -> Rows:
        c = self.customers.get(customer_id)
        if not c:
            return []
        loyalty = self.loyalty.get(customer_id) or {}
        return normalise_rows("get_customer_profile", [{
            "customer_id": c["customer_id"], "full_name": c["full_name"], "tier": c["tier"],
            "member_since": c["created_at"], "loyalty_points": loyalty.get("points"), "as_of": self.as_of,
        }])

    async def get_customer_context(self, customer_id: str) -> Rows:
        rows = []
        for i in self._enriched_items(customer_id):
            e = self._last_event(i["shipment_id"])
            delivered_at = e.get("delivered_at")
            window_open = None if delivered_at is None else (self.as_of - delivered_at.date()).days <= self.window_days
            base = {
                "order_id": i["order_id"], "placed_on": i["placed_at"], "order_status": i["order_status"],
                "item_id": i["item_id"], "title": i["title"], "color": i["color"], "size": i["size"],
                "qty": i["qty"], "price": i["price"], "currency": i["currency"], "item_status": i["item_status"],
                "carrier": i["carrier"], "tracking_no": i["tracking_no"], "shipment_status": i["shipment_status"],
                "last_shipment_event": e.get("last_event"), "last_shipment_location": e.get("last_location"),
                "last_shipment_event_at": e.get("last_event_at"), "delivered_on": delivered_at,
                "payment_last4": (self.payments.get(i["order_id"]) or {}).get("last4"),
                "return_window_open": window_open, "as_of": self.as_of, "_sort": (i["placed_at"], i["item_id"]),
            }
            returns = self.returns_by_item.get(i["item_id"]) or [None]
            for r in returns:
                refunds = (self.refunds_by_return.get(r["return_id"]) if r else None) or [None]
                for f in refunds:
                    rows.append({**base,
                                 "return_status": r and r["status"], "return_label_created_at": r and r["label_created_at"],
                                 "return_warehouse_scanned_at": r and r["warehouse_scanned_at"],
                                 "refund_status": f and f["status"], "refund_amount": f and f["amount"],
                                 "refund_method": f and f["method"], "refund_initiated_at": f and f["initiated_at"],
                                 "refund_completed_at": f and f["completed_at"],
                                 "refund_failure_reason": f and f["failure_reason"]})
        rows.sort(key=lambda r: (-r["_sort"][0].timestamp(), r["_sort"][1]))
        return normalise_rows("get_customer_context", rows[:200])

    async def find_order_items(self, customer_id: str, item_query: str) -> Rows:
        cleaned = re.sub(r"[^a-z0-9 ]", " ", (item_query or "").lower())
        words = [w for w in re.split(" +", cleaned) if len(w) >= 3 and w not in STOPWORDS]
        words = [re.sub("s$", "", w) if len(w) > 3 else w for w in words]
        scored = [(sum(1 for w in words if w in i["search_text"]), i) for i in self._enriched_items(customer_id)]
        top = max((s for s, _ in scored), default=0)
        best = sorted((i for s, i in scored if s > 0 and s == top), key=lambda i: i["placed_at"], reverse=True)
        return normalise_rows("find_order_items", [{
            "item_id": i["item_id"], "order_id": i["order_id"], "placed_on": i["placed_at"], "title": i["title"],
            "color": i["color"], "size": i["size"], "price": i["price"], "item_status": i["item_status"],
            "match_score": top, "as_of": self.as_of,
        } for i in best])

    async def get_case_timeline(self, customer_id: str, order_id: str) -> Rows:
        o = self.orders.get(order_id)
        if not o or o["customer_id"] != customer_id:
            return []
        events = [(o["placed_at"], "order_placed", None, f"total {_num(o['total'])} {o['currency']}")]
        for s in (s for s in self.shipments.values() if s["order_id"] == order_id):
            for e in self.events_by_shipment[s["shipment_id"]]:
                events.append((e["event_at"], f"shipment_{e['event_type']}", None,
                               f"{s['carrier']} {s['tracking_no']} @ {e['location']}"))
        for i in self.items_by_order[order_id]:
            for r in self.returns_by_item.get(i["item_id"], []):
                events.append((r["label_created_at"], "return_label_created", r["item_id"], r["reason"]))
                if r["warehouse_scanned_at"]:
                    events.append((r["warehouse_scanned_at"], "return_warehouse_scanned", r["item_id"], r["status"]))
        for f in self.refunds_by_order.get(order_id, []):
            events.append((f["initiated_at"], "refund_initiated", None, f"{_num(f['amount'])} via {f['method']}"))
            if f["completed_at"]:
                events.append((f["completed_at"], "refund_completed", None, f"{_num(f['amount'])} via {f['method']}"))
            if f["status"] == "failed":
                events.append((f["initiated_at"], "refund_failed", None, f["failure_reason"]))
        for c in self.contacts_by_customer.get(customer_id, []):
            if c["order_id"] == order_id:
                events.append((c["created_at"], f"support_{c['channel']}", None, c["topic"]))
        events.sort(key=lambda e: (e[0], e[1]))
        return normalise_rows("get_case_timeline", [
            {"event_at": at, "event_type": et, "item_id": item, "detail": detail, "as_of": self.as_of}
            for at, et, item, detail in events
        ])

    async def get_stuck_cases(self, customer_id: str) -> Rows:
        refund_cutoff = self.reference - timedelta(days=self.stuck_refund_days)
        shipment_cutoff = self.reference - timedelta(days=self.stuck_shipment_days)
        cases = []
        for o in self.orders_by_customer.get(customer_id, []):
            for i in self.items_by_order[o["order_id"]]:
                for r in self.returns_by_item.get(i["item_id"], []):
                    for f in self.refunds_by_return.get(r["return_id"]) or [None]:
                        status = f["status"] if f else None
                        scanned = r["warehouse_scanned_at"]
                        if status == "failed":
                            diagnosis = "refund_failed"
                        elif scanned is not None and scanned <= refund_cutoff and status in (None, "pending", "processing"):
                            diagnosis = "stuck_refund"
                        else:
                            continue
                        cases.append({"case_type": "return", "case_id": r["return_id"], "order_id": o["order_id"],
                                      "item_id": i["item_id"], "diagnosis": diagnosis, "case_state": r["status"],
                                      "last_activity_at": scanned, "refund_status": status,
                                      "refund_amount": f and f["amount"],
                                      "refund_failure_reason": f and f["failure_reason"]})
            for s in (s for s in self.shipments.values() if s["order_id"] == o["order_id"]):
                last = max(e["event_at"] for e in self.events_by_shipment[s["shipment_id"]])
                if s["status"] != "delivered" and o["status"] != "cancelled" and last <= shipment_cutoff:
                    cases.append({"case_type": "shipment", "case_id": s["shipment_id"], "order_id": o["order_id"],
                                  "item_id": None, "diagnosis": "stuck_shipment", "case_state": s["status"],
                                  "last_activity_at": last, "refund_status": None, "refund_amount": None,
                                  "refund_failure_reason": None})
        for c in cases:
            last = c["last_activity_at"]
            c["days_since_activity"] = (self.as_of - last.date()).days if last else None
            c["as_of"] = self.as_of
        cases.sort(key=lambda c: (c["last_activity_at"] is None, c["last_activity_at"] or self.reference))
        return normalise_rows("get_stuck_cases", cases)

    async def get_policy_signals(self, customer_id: str) -> Rows:
        c = self.customers.get(customer_id)
        if not c:
            return []
        since = self.as_of - timedelta(days=365)
        completed = [f for o in self.orders_by_customer.get(customer_id, [])
                     for f in self.refunds_by_order.get(o["order_id"], [])
                     if f["status"] == "completed" and f["completed_at"].date() > since]
        flags = sorted(f for f in (c["risk_flags"] or "").split(";") if f)
        return normalise_rows("get_policy_signals", [{
            "customer_id": customer_id, "risk_flags": ";".join(flags),
            "account_age_days": (self.as_of - c["created_at"].date()).days, "refunds_365d": len(completed),
            "refund_total_365d": sum(f["amount"] for f in completed), "as_of": self.as_of,
        }])

    # ------------------------------------------------------------------ multi-agent read layer
    # Each mirrors one statement in databricks_adapter.READ_QUERIES.

    async def resolve_channel_identity(self, channel: str, identifier: str) -> Rows:
        wanted = (identifier or "").strip().lower()
        rows = [i for i in self.identities if i["channel"] == channel and i["identifier"].strip().lower() == wanted]
        rows.sort(key=lambda i: i["identity_id"])
        return normalise_rows("resolve_channel_identity", rows[:10])

    async def get_payment_summary(self, customer_id: str) -> Rows:
        rows = []
        for o in self.orders_by_customer.get(customer_id, []):
            refunds = self.refunds_by_order.get(o["order_id"], [])

            def total(statuses: tuple[str, ...], refunds: list[dict] = refunds) -> float:
                return round(sum(f["amount"] for f in refunds if f["status"] in statuses), 2)

            base = {"order_id": o["order_id"], "placed_on": o["placed_at"], "order_status": o["status"],
                    "order_total": o["total"], "currency": o["currency"],
                    "payments_on_order": len(self.payments_by_order.get(o["order_id"], [])),
                    "refund_count": len(refunds), "refunds_completed_total": total(("completed",)),
                    "refunds_open_total": total(("pending", "processing")), "refunds_failed_total": total(("failed",)),
                    "as_of": self.as_of}
            for p in self.payments_by_order.get(o["order_id"]) or [None]:
                rows.append({**base, "payment_id": p and p["payment_id"], "payment_status": p and p["status"],
                             "payment_last4": p and p["last4"], "_sort": (o["placed_at"], o["order_id"])})
        rows.sort(key=lambda r: (-r["_sort"][0].timestamp(), r["_sort"][1], r["payment_id"] or ""))
        return normalise_rows("get_payment_summary", rows[:200])

    async def get_loyalty_profile(self, customer_id: str) -> Rows:
        c = self.customers.get(customer_id)
        if not c:
            return []
        loyalty = self.loyalty.get(customer_id) or {}
        return normalise_rows("get_loyalty_profile", [{
            "customer_id": customer_id, "points": loyalty.get("points"), "loyalty_tier": loyalty.get("tier"),
            "customer_tier": c["tier"], "member_since": c["created_at"],
            "last_adjustment_at": loyalty.get("last_adjustment_at"), "as_of": self.as_of,
        }])

    async def get_contact_history(self, customer_id: str) -> Rows:
        contacts = sorted(self.contacts_by_customer.get(customer_id, []),
                          key=lambda c: (-c["created_at"].timestamp(), c["contact_id"]))
        return normalise_rows("get_contact_history", [{**c, "as_of": self.as_of} for c in contacts[:20]])

    async def get_reference_config(self) -> Rows:
        return normalise_rows("get_reference_config",
                              [{"key": k, "value": v} for k, v in sorted(self.config.items())])

    async def get_catalog_variants(self) -> Rows:
        rows = [{**self.products[v["product_id"]], **v} for v in self.product_variants]
        rows.sort(key=lambda r: (r["product_id"], r["sku"]))
        return normalise_rows("get_catalog_variants", rows)

    async def get_product_relations(self) -> Rows:
        rows = sorted(self.product_relations, key=lambda r: (r["product_id"], -r["strength"], r["related_product_id"]))
        return normalise_rows("get_product_relations", rows)

    async def get_product_launches(self) -> Rows:
        rows = sorted(self.stylist["product_launches"], key=lambda r: (r["planned_at"], r["launch_id"]))
        return normalise_rows("get_product_launches", rows)

    async def get_product_styles(self) -> Rows:
        rows = sorted(self.stylist["product_styles"], key=lambda r: (r["product_id"], r["style"]))
        return normalise_rows("get_product_styles", rows)

    async def get_colour_families(self) -> Rows:
        return normalise_rows("get_colour_families", sorted(self.stylist["colour_families"], key=lambda r: r["color"]))

    async def get_colour_harmony(self) -> Rows:
        rows = sorted(self.stylist["colour_harmony"], key=lambda r: (r["family"], r["other_family"]))
        return normalise_rows("get_colour_harmony", rows)

    async def get_product_copurchase(self) -> Rows:
        product_of = {v["sku"]: v["product_id"] for v in self.product_variants}
        by_order: dict[str, set[str]] = defaultdict(set)
        for o in self.orders.values():
            if o["status"] != "cancelled":
                for i in self.items_by_order[o["order_id"]]:
                    if i["sku"] in product_of:
                        by_order[o["order_id"]].add(product_of[i["sku"]])
        together: dict[tuple[str, str], int] = defaultdict(int)
        with_product: dict[str, int] = defaultdict(int)
        for products in by_order.values():
            for a in products:
                with_product[a] += 1
                for b in products - {a}:
                    together[(a, b)] += 1
        rows = [{"product_id": a, "related_product_id": b, "orders_together": n, "orders_with_product": with_product[a]}
                for (a, b), n in together.items()]
        rows.sort(key=lambda r: (r["product_id"], -r["orders_together"], r["related_product_id"]))
        return normalise_rows("get_product_copurchase", rows)

    async def get_purchase_history(self, customer_id: str) -> Rows:
        rows = []
        for o in self.orders_by_customer.get(customer_id, []):
            for i in self.items_by_order[o["order_id"]]:
                for r in self.returns_by_item.get(i["item_id"]) or [None]:
                    rows.append({**i, "placed_on": o["placed_at"], "order_status": o["status"],
                                 "return_reason": r and r["reason"], "return_status": r and r["status"],
                                 "as_of": self.as_of, "_sort": (o["placed_at"], i["item_id"])})
        rows.sort(key=lambda r: (-r["_sort"][0].timestamp(), r["_sort"][1], r["return_status"] or ""))
        return normalise_rows("get_purchase_history", rows[:200])

    async def list_customers(self) -> Rows:
        web = {}
        for i in sorted(self.identities, key=lambda i: i["identity_id"]):
            if i["channel"] == "web_session":
                web.setdefault(i["customer_id"], []).append(i)
        rows = [{**c, "order_count": len(self.orders_by_customer.get(cid, [])), **ident}
                for cid, c in sorted(self.customers.items()) for ident in web.get(cid) or [{}]]
        return normalise_rows("list_customers", rows)
