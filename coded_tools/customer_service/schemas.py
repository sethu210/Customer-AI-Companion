"""
Result shapes of the customer-data functions, shared by every adapter.

Both the mock and the Databricks adapter normalise their rows through `normalise_rows`, so callers (and the
parity tests) always see the same Python types: ISO-8601 strings for timestamps ("2026-10-14T13:00:00Z") and
dates ("2026-10-16"), float for doubles, int for integers, bool for booleans and None for missing values.
"""

from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

TIMESTAMP = "timestamp"
DATE = "date"
STRING = "string"
DOUBLE = "double"
INT = "int"
BOOL = "bool"

# Column order matches the RETURNS TABLE clauses in test_data/databricks/uc_functions.sql.
FUNCTION_COLUMNS: dict[str, dict[str, str]] = {
    "get_customer_profile": {
        "customer_id": STRING, "full_name": STRING, "tier": STRING, "member_since": DATE,
        "loyalty_points": INT, "as_of": DATE,
    },
    "get_customer_context": {
        "order_id": STRING, "placed_on": DATE, "order_status": STRING,
        "item_id": STRING, "title": STRING, "color": STRING, "size": STRING, "qty": INT, "price": DOUBLE,
        "currency": STRING, "item_status": STRING,
        "carrier": STRING, "tracking_no": STRING, "shipment_status": STRING,
        "last_shipment_event": STRING, "last_shipment_location": STRING, "last_shipment_event_at": TIMESTAMP,
        "delivered_on": DATE,
        "return_status": STRING, "return_label_created_at": TIMESTAMP, "return_warehouse_scanned_at": TIMESTAMP,
        "refund_status": STRING, "refund_amount": DOUBLE, "refund_method": STRING,
        "refund_initiated_at": TIMESTAMP, "refund_completed_at": TIMESTAMP, "refund_failure_reason": STRING,
        "payment_last4": STRING, "return_window_open": BOOL, "as_of": DATE,
    },
    "find_order_items": {
        "item_id": STRING, "order_id": STRING, "placed_on": DATE, "title": STRING, "color": STRING,
        "size": STRING, "price": DOUBLE, "item_status": STRING, "match_score": INT, "as_of": DATE,
    },
    "get_case_timeline": {
        "event_at": TIMESTAMP, "event_type": STRING, "item_id": STRING, "detail": STRING, "as_of": DATE,
    },
    "get_stuck_cases": {
        "case_type": STRING, "case_id": STRING, "order_id": STRING, "item_id": STRING, "diagnosis": STRING,
        "case_state": STRING, "last_activity_at": TIMESTAMP, "days_since_activity": INT,
        "refund_status": STRING, "refund_amount": DOUBLE, "refund_failure_reason": STRING, "as_of": DATE,
    },
    # Internal (policy_api schema): never returned to the LLM.
    "get_policy_signals": {
        "customer_id": STRING, "risk_flags": STRING, "account_age_days": INT, "refunds_365d": INT,
        "refund_total_365d": DOUBLE, "as_of": DATE,
    },
    # Multi-agent read layer: fixed read-only SQL over the existing silver tables (no UC function behind them).
    # Gateway identity lookup; keyed by the channel identifier in sly_data, never by anything the LLM supplies.
    "resolve_channel_identity": {
        "identity_id": STRING, "customer_id": STRING, "channel": STRING, "verified": BOOL, "auth_level": STRING,
    },
    # One row per order and payment; method_token is deliberately not selected.
    "get_payment_summary": {
        "order_id": STRING, "placed_on": DATE, "order_status": STRING, "order_total": DOUBLE, "currency": STRING,
        "payment_id": STRING, "payment_status": STRING, "payment_last4": STRING, "payments_on_order": INT,
        "refund_count": INT, "refunds_completed_total": DOUBLE, "refunds_open_total": DOUBLE,
        "refunds_failed_total": DOUBLE, "as_of": DATE,
    },
    "get_loyalty_profile": {
        "customer_id": STRING, "points": INT, "loyalty_tier": STRING, "customer_tier": STRING,
        "member_since": DATE, "last_adjustment_at": TIMESTAMP, "as_of": DATE,
    },
    # notes is free text written by people and other systems: untrusted data, never instructions (scenario S20).
    "get_contact_history": {
        "contact_id": STRING, "order_id": STRING, "channel": STRING, "topic": STRING, "notes": STRING,
        "created_at": TIMESTAMP, "resolution": STRING, "as_of": DATE,
    },
    # Internal: the data's config table, compared with config/service_policy.hocon by policy.check_data_reference.
    "get_reference_config": {"key": STRING, "value": STRING},
    # Product catalog (<catalog>.catalog schema): no customer data, the same for every caller.
    "get_catalog_variants": {
        "sku": STRING, "product_id": STRING, "title": STRING, "category": STRING, "description": STRING,
        "material": STRING, "fit": STRING, "season": STRING, "color": STRING, "size": STRING,
        "list_price": DOUBLE, "currency": STRING, "active": BOOL, "stock": INT,
    },
    "get_product_relations": {
        "relation_id": STRING, "product_id": STRING, "related_product_id": STRING, "relation": STRING,
        "strength": DOUBLE,
    },
    # Precomputed from order history (catalog.product_copurchase); counts only, no customer data.
    "get_product_copurchase": {
        "product_id": STRING, "related_product_id": STRING, "orders_together": INT, "orders_with_product": INT,
    },
    # Stylist data (companion plan P4), catalog schema; no customer data.
    "get_product_launches": {"launch_id": STRING, "product_id": STRING, "planned_at": TIMESTAMP, "headline": STRING},
    "get_product_styles": {"product_id": STRING, "style": STRING},
    "get_colour_families": {"color": STRING, "family": STRING},
    "get_colour_harmony": {"family": STRING, "other_family": STRING, "strength": DOUBLE},
    # One customer's items with SKU and return reason, for recommendations (read-only, existing silver tables).
    "get_purchase_history": {
        "item_id": STRING, "order_id": STRING, "placed_on": DATE, "order_status": STRING, "sku": STRING,
        "title": STRING, "category": STRING, "color": STRING, "size": STRING, "item_status": STRING,
        "return_reason": STRING, "return_status": STRING, "as_of": DATE,
    },
    # Demo console only (customer picker); not an agent tool. One row per customer and website identity.
    "list_customers": {
        "customer_id": STRING, "full_name": STRING, "tier": STRING, "identifier": STRING, "verified": BOOL,
        "auth_level": STRING, "order_count": INT,
    },
}


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalise_value(value: Any, col_type: str) -> Any:
    if value is None or (isinstance(value, str) and value == "" and col_type != STRING):
        return None
    if col_type == TIMESTAMP:
        return _to_datetime(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    if col_type == DATE:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]
    if col_type == DOUBLE:
        return float(value)
    if col_type == INT:
        return int(float(value))
    if col_type == BOOL:
        return value if isinstance(value, bool) else str(value).strip().lower() == "true"
    return str(value)


def normalise_rows(function: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = FUNCTION_COLUMNS[function]
    return [{col: normalise_value(row.get(col), col_type) for col, col_type in columns.items()} for row in rows]


def rows_from_columns(columns: list[str], data: list[list[Any]]) -> list[dict[str, Any]]:
    return [dict(zip(columns, values)) for values in data]
