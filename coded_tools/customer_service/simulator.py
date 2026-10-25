"""
Local event simulator for the Resolution Guardian (E3, decision 4). The test data is static (a fixed
reference_date), so nothing "happens" by itself. This overlay keeps a simulated clock and business events (refund
completed or failed, parcel scanned or delivered, return received) in one local JSON file, and SimulatedAdapter
shows them on top of the base data. The base data, mock or Databricks, is never changed.

    clock    reference_date at START_HOUR (after every event in the data) + the hours advanced so far
    events   {"kind", "item_id" | "order_id", "at", "location"?, "reason"?, "confirmed"}

confirmed=false is a test of the Guardian's read-back: the account view shows the change but the order timeline
does not (like a replica that is behind), so a careful reader must not trust it.

What the overlay changes: get_customer_context (refund, return and shipment fields), get_case_timeline (the
event, unless unconfirmed) and get_stuck_cases (a case the event resolved disappears; a failed refund appears).
The data's own "today" (as_of) does not move: only the Guardian's clock does.

Environment:
    EVENT_SIMULATOR   1 = get_adapter() wraps the backend with the overlay (demo, Guardian evaluation)
    SIMULATOR_STATE   default <project>/runtime/simulator/state.json
"""

import json
import os
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.data_adapter import Rows

PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_HOUR = 18  # the generator puts every event of a day at or before 18:00 (test_data/generator HOUR)
_LOCK = threading.Lock()

# kind -> the record it targets
EVENT_KINDS = {
    "refund_completed": "item_id",
    "refund_failed": "item_id",
    "return_received": "item_id",
    "parcel_scanned": "order_id",
    "parcel_delivered": "order_id",
}
CITIES = ["Louisville, KY", "Columbus, OH", "Atlanta, GA", "Dallas, TX", "Reno, NV"]
TS = "%Y-%m-%dT%H:%M:%SZ"


def enabled() -> bool:
    return os.environ.get("EVENT_SIMULATOR", "").strip() == "1"


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS)


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, TS).replace(tzinfo=timezone.utc)


def _reference_date() -> str:
    from coded_tools.customer_service.policy import load_policy

    return load_policy()["reference_date"]


class Simulator:
    def __init__(self, path: Path | str | None = None, reference_date: str | None = None) -> None:
        self.path = Path(path or os.environ.get("SIMULATOR_STATE",
                                                PROJECT_ROOT / "runtime" / "simulator" / "state.json"))
        self._reference_date = reference_date

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"offset_hours": 0.0, "events": []}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def start(self) -> datetime:
        day = datetime.fromisoformat(self._reference_date or _reference_date())
        return day.replace(hour=START_HOUR, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.start() + timedelta(hours=float(self._load().get("offset_hours", 0)))

    def now_ts(self) -> str:
        return _ts(self.now())

    def events(self) -> list[dict[str, Any]]:
        return self._load().get("events", [])

    def state(self) -> dict[str, Any]:
        return {"clock": self.now_ts(), "start": _ts(self.start()), "events": self.events()}

    def advance(self, hours: float) -> str:
        if hours <= 0 or hours > 24 * 60:
            raise ValueError("advance by more than 0 hours and at most 60 days")
        with _LOCK:
            state = self._load()
            state["offset_hours"] = float(state.get("offset_hours", 0)) + hours
            self._save(state)
        return self.now_ts()

    def add_event(self, kind: str, *, target: str, location: str | None = None, reason: str | None = None,
                  confirmed: bool = True) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event {kind!r}; use one of {', '.join(EVENT_KINDS)}")
        with _LOCK:
            state = self._load()
            events = state.setdefault("events", [])
            event = {"id": len(events) + 1, "kind": kind, EVENT_KINDS[kind]: target, "at": self.now_ts(),
                     "confirmed": bool(confirmed)}
            if kind == "parcel_scanned":
                event["location"] = location or CITIES[len(events) % len(CITIES)]
            if kind == "refund_failed":
                event["reason"] = reason or "payment_declined"
            events.append(event)
            self._save(state)
        return event

    def reset(self) -> None:
        with _LOCK:
            self._save({"offset_hours": 0.0, "events": []})


# ---------------------------------------------------------------- the overlay


def _for_row(event: dict[str, Any], row: dict[str, Any]) -> bool:
    if EVENT_KINDS[event["kind"]] == "item_id":
        return event.get("item_id") == row.get("item_id")
    return event.get("order_id") == row.get("order_id") and bool(row.get("tracking_no"))


def apply_to_context(rows: Rows, events: list[dict[str, Any]]) -> Rows:
    out = []
    for row in rows:
        row = dict(row)
        for e in events:
            if not _for_row(e, row):
                continue
            kind, at = e["kind"], e["at"]
            if kind in ("refund_completed", "refund_failed"):
                if row.get("refund_status") == "completed":
                    continue
                if row.get("refund_amount") is None:  # a refund the agents started: no refund row in the data yet
                    row.update(refund_amount=round(float(row.get("price") or 0) * int(row.get("qty") or 1), 2),
                               refund_method="original_payment", refund_initiated_at=at)
                if kind == "refund_completed":
                    row.update(refund_status="completed", refund_completed_at=at, refund_failure_reason=None)
                else:
                    row.update(refund_status="failed", refund_completed_at=None, refund_failure_reason=e.get("reason"))
            elif kind == "return_received":
                if row.get("return_status") in (None, "label_created", "in_transit"):
                    row.update(return_status="received", return_warehouse_scanned_at=at)
            elif kind == "parcel_scanned":
                if row.get("shipment_status") != "delivered":
                    row.update(shipment_status="in_transit", last_shipment_event="in_transit",
                               last_shipment_location=e.get("location"), last_shipment_event_at=at)
            elif kind == "parcel_delivered":
                row.update(shipment_status="delivered", last_shipment_event="delivered", last_shipment_event_at=at,
                           delivered_on=at[:10])
        out.append(row)
    return out


class SimulatedAdapter(CustomerDataAdapter):
    """The configured backend with the simulator's events shown on top. Every other read passes through."""

    def __init__(self, inner: CustomerDataAdapter, path: Path | str | None = None) -> None:
        self.inner = inner
        self.path = path
        self.name = f"{inner.name}+simulator"

    def _events(self) -> list[dict[str, Any]]:
        return Simulator(self.path).events()

    async def get_customer_context(self, customer_id: str) -> Rows:
        return apply_to_context(await self.inner.get_customer_context(customer_id), self._events())

    async def get_case_timeline(self, customer_id: str, order_id: str) -> Rows:
        rows = await self.inner.get_case_timeline(customer_id, order_id)
        events = [e for e in self._events() if e.get("confirmed", True)]
        if not rows or not events:
            return rows
        context = {r["item_id"]: r for r in await self.inner.get_customer_context(customer_id)
                   if r["order_id"] == order_id}
        as_of = rows[0]["as_of"]
        added = []
        for e in events:
            if e.get("item_id") in context:
                item = context[e["item_id"]]
                kind = {"return_received": "return_warehouse_scanned"}.get(e["kind"], e["kind"])
                detail = e.get("reason") if e["kind"] == "refund_failed" else "simulated"
                added.append({"event_at": e["at"], "event_type": kind, "item_id": item["item_id"], "detail": detail,
                              "as_of": as_of})
            elif e.get("order_id") == order_id:
                kind = {"parcel_scanned": "shipment_in_transit", "parcel_delivered": "shipment_delivered"}[e["kind"]]
                for tracking, carrier in sorted({(r["tracking_no"], r["carrier"]) for r in context.values()
                                                 if r.get("tracking_no")}):
                    added.append({"event_at": e["at"], "event_type": kind, "item_id": None,
                                  "detail": f"{carrier} {tracking} @ {e.get('location') or 'destination'}",
                                  "as_of": as_of})
        return sorted(rows + added, key=lambda r: (r["event_at"], r["event_type"]))

    async def get_stuck_cases(self, customer_id: str) -> Rows:
        rows = await self.inner.get_stuck_cases(customer_id)
        events = self._events()
        if not events:
            return rows
        moved = {e["item_id"] for e in events if e["kind"] in ("refund_completed", "refund_failed")}
        moved_orders = {e["order_id"] for e in events if e["kind"] in ("parcel_scanned", "parcel_delivered")}
        out = [r for r in rows if not (r["case_type"] == "return" and r["item_id"] in moved)
               and not (r["case_type"] == "shipment" and r["order_id"] in moved_orders)]
        context = apply_to_context(await self.inner.get_customer_context(customer_id), events)
        as_of = rows[0]["as_of"] if rows else (context[0]["as_of"] if context else None)
        for r in context:
            failed = next((e for e in events if e["kind"] == "refund_failed" and e["item_id"] == r["item_id"]), None)
            if failed and r.get("refund_status") == "failed":
                out.append({"case_type": "return", "case_id": f"SIM-{failed['id']}", "order_id": r["order_id"],
                            "item_id": r["item_id"], "diagnosis": "refund_failed", "case_state": r.get("return_status"),
                            "last_activity_at": failed["at"], "days_since_activity": 0, "refund_status": "failed",
                            "refund_amount": r.get("refund_amount"), "refund_failure_reason": failed.get("reason"),
                            "as_of": as_of})
        return out


def _passthrough(function: str):
    async def method(self: SimulatedAdapter, *args: Any, **kwargs: Any) -> Any:
        return await getattr(self.inner, function)(*args, **kwargs)

    method.__name__ = function
    return method


for _name in sorted(CustomerDataAdapter.__abstractmethods__ | {"list_customers"}):
    if _name not in SimulatedAdapter.__dict__:
        setattr(SimulatedAdapter, _name, _passthrough(_name))
SimulatedAdapter.__abstractmethods__ = frozenset()
