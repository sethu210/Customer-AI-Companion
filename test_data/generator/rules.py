"""
Reference implementation of the data-side diagnosis rules.

The Databricks gold view `case_status` and the mock adapter must agree with this module;
the scenario files' `expected.diagnosis` is checked against it.
"""

from datetime import datetime
from datetime import timedelta
from typing import Any

STUCK_REFUND = "stuck_refund"
REFUND_FAILED = "refund_failed"
STUCK_SHIPMENT = "stuck_shipment"


def diagnose_customer(
    customer_id: str, tables: dict[str, list[dict[str, Any]]], reference: datetime, policy: dict[str, Any]
) -> list[str]:
    """
    :return: sorted, de-duplicated diagnosis codes for one customer's open cases.
    """
    orders = {o["order_id"]: o for o in tables["orders"] if o["customer_id"] == customer_id}
    items = {i["item_id"]: i for i in tables["order_items"] if i["order_id"] in orders}
    returns = [r for r in tables["returns"] if r["item_id"] in items]
    refunds = [r for r in tables["refunds"] if r["order_id"] in orders]
    shipments = [s for s in tables["shipments"] if s["order_id"] in orders]

    found: set[str] = set()

    stuck_refund_cutoff = reference - timedelta(days=policy["stuck_refund_days"])
    for ret in returns:
        scanned = ret["warehouse_scanned_at"]
        if scanned is None or scanned > stuck_refund_cutoff:
            continue
        ret_refunds = [r for r in refunds if r["return_id"] == ret["return_id"]]
        if not any(r["status"] == "completed" for r in ret_refunds) and not any(
            r["status"] == "failed" for r in ret_refunds
        ):
            found.add(STUCK_REFUND)

    if any(r["status"] == "failed" for r in refunds):
        found.add(REFUND_FAILED)

    stuck_shipment_cutoff = reference - timedelta(days=policy["stuck_shipment_days"])
    for shp in shipments:
        if shp["status"] == "delivered" or orders[shp["order_id"]]["status"] == "cancelled":
            continue
        events = [e["event_at"] for e in tables["shipment_events"] if e["shipment_id"] == shp["shipment_id"]]
        if events and max(events) <= stuck_shipment_cutoff:
            found.add(STUCK_SHIPMENT)

    return sorted(found)
