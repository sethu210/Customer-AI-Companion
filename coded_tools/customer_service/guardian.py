"""
Resolution Guardian (E3): follows each unresolved issue after the conversation until its business outcome is
verified. Persistent state plus a deterministic watcher, not a long-running LLM process: code reads the records,
applies the rules, hands off and closes. Customer updates are fixed templates filled with facts.

A watch (one per issue)
    created     by code at the end of a turn (gateway.finish_turn -> watch_case) for an issue left in a non-final
                business state: queued for a person's review, handed to a person, an action whose business outcome is
                still to come (refund started, carrier trace opened), or an answer about a refund, return or
                delivery that is still in progress. The same subject is never watched twice for a customer.
    milestones  what it waits for, in order: review_decided, handoff_resolved, return_received, refund_completed,
                parcel_delivered. For an answered issue the first check decides them from fresh data (nothing in
                progress: closed at once, nothing_pending).
    state       watching (not checked yet) -> waiting_on_business | waiting_on_customer -> escalated -> resolved
                -> closed (the closing update was sent, or the customer opted out)

Rules at each check (settings: guardian in config/service_policy.hocon)
    progress seen (a milestone reached, a new carrier scan)  -> an update
    no movement for delay_hours                              -> one update explaining the delay
    sla_hours passed                                         -> a hand-off to a person with the case packet
    refund failed                                            -> a hand-off and an update (no re-try: decision 5)
    last milestone reached and confirmed by read-back        -> resolved, the closing update, closed
    a completion the order timeline does not confirm         -> not trusted; a person after N checks
The Guardian never changes an order, refund or shipment and never re-tries an action (decision 5).

Updates (decision 6) go to outbox.jsonl (source "guardian") for the channel gateway: at most max_updates_per_day
per customer (counted together with the Stylist's invites, messaging.py), none in quiet hours (they wait), none after
an opt-out (hand-offs still happen). A newer waiting
update of the same issue replaces an older one. Every update is stored once (dedupe key), so a restarted worker
never repeats one.

The clock is the local event simulator's (simulator.Simulator): it only moves when the demo or a test advances it.
"""

import asyncio
import json
import sqlite3
import threading
import uuid
import weakref
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from coded_tools.customer_service import messaging
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import EXECUTED_VERIFIED
from coded_tools.customer_service.case_store import HANDED_OFF
from coded_tools.customer_service.case_store import QUEUED_FOR_REVIEW
from coded_tools.customer_service.case_store import RESOLVED_INFO_ONLY
from coded_tools.customer_service.case_store import REVIEW_REJECTED
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.policy import ITEM_ACTIONS
from coded_tools.customer_service.simulator import TS
from coded_tools.customer_service.simulator import Simulator
from coded_tools.customer_service.simulator import parse_ts

OUTBOX = "outbox.jsonl"
_LOCK = threading.Lock()
_RUN_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()

WATCHING = "watching"
WAITING_ON_BUSINESS = "waiting_on_business"
WAITING_ON_CUSTOMER = "waiting_on_customer"
ESCALATED = "escalated"
RESOLVED = "resolved"
CLOSED = "closed"
ACTIVE = (WATCHING, WAITING_ON_BUSINESS, WAITING_ON_CUSTOMER, ESCALATED, RESOLVED)

# The business outcome each action leaves to come. Actions not listed are complete when executed and verified.
ACTION_MILESTONES = {"initiate_refund": ["refund_completed"], "retrigger_refund": ["refund_completed"],
                     "open_carrier_trace": ["parcel_delivered"],
                     "create_return": ["return_received", "refund_completed"]}
ANSWERED_DOMAINS = ("returns_refunds", "order_delivery")  # answered issues whose outcome may still be in progress
WAITING_WORDS = {"review_decided": "a colleague's review", "handoff_resolved": "a colleague",
                 "return_received": "the returned parcel to reach the warehouse",
                 "refund_completed": "the refund to complete", "parcel_delivered": "the parcel to be delivered"}

# Fixed wording, filled with facts from the records. No promises of a time or an outcome.
TEMPLATES = {
    "return_received": "We have received your returned {title}. The refund is the next step. Case {case_id}.",
    "refund_completed": "Your refund of {amount} for the {title} has been completed{method}. Case {case_id}.",
    "parcel_scanned": "Your parcel with the {title} is moving: {carrier} scanned it in {location}. Case {case_id}.",
    "parcel_delivered": "{carrier} reports your {title} as delivered. Case {case_id}.",
    "refund_delayed": "Your refund for the {title} is taking longer than usual: nothing has moved for {days} days. "
                      "We are watching it, and a colleague steps in if it stays stuck. Case {case_id}.",
    "parcel_delayed": "Your parcel with the {title} has had no carrier scan for {days} days (last seen in "
                      "{location}). We are watching it, and a colleague steps in if it stays stuck. Case {case_id}.",
    "refund_failed": "There is a problem with your refund for the {title}, so a colleague is now looking at it. You "
                     "do not need to contact us again. Case {case_id}.",
    "escalated": "Your request is taking longer than it should, so a colleague is now looking at it. You do not need "
                 "to contact us again. Case {case_id}.",
    "handoff_resolved": "A colleague has finished working on your request. Case {case_id}.",
}
FINAL_NOTICES = {"refund_completed", "parcel_delivered", "handoff_resolved"}
METHOD_WORDS = {"original_payment": " to your original payment method", "store_credit": " as store credit"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_watches (
    watch_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, issue_id TEXT NOT NULL UNIQUE, customer_id TEXT NOT NULL,
    domain TEXT, subject_key TEXT, subject TEXT NOT NULL, milestones TEXT, step INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL, created_at TEXT NOT NULL, step_started_at TEXT, step_since TEXT, sla_due TEXT,
    next_check_at TEXT, last_checked_at TEXT, checks INTEGER NOT NULL DEFAULT 0,
    unconfirmed INTEGER NOT NULL DEFAULT 0, delay_explained INTEGER NOT NULL DEFAULT 0, last_seen TEXT,
    escalation_handoff_id TEXT, closed_reason TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS guardian_notices (
    notice_id TEXT PRIMARY KEY, dedupe TEXT NOT NULL UNIQUE, watch_id TEXT NOT NULL, customer_id TEXT NOT NULL,
    case_id TEXT, kind TEXT NOT NULL, final INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, held_reason TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS guardian_preferences (
    customer_id TEXT PRIMARY KEY, updates_opt_out INTEGER NOT NULL, updated_at TEXT
);
"""


def _ts(dt: datetime) -> str:
    return dt.strftime(TS)


def issue_subject(issue: dict[str, Any]) -> tuple[str | None, str | None]:
    """(item_id, order_id): the issue's references, else the latest unique find_order_items match."""
    item, order = (issue.get("refs") or {}).get("item_id"), (issue.get("refs") or {}).get("order_id")
    if item or order:
        return item, order
    for finding in reversed(issue.get("findings") or []):
        data = finding["data"]
        if finding["source"] == "find_order_items" and isinstance(data, dict) and data.get("result") == "unique":
            return data["items"][0]["item_id"], data["items"][0]["order_id"]
    return None, None


def subject_key(subject: dict[str, Any]) -> str:
    """What one watch is about, so it is never watched twice: a hand-off, else the item, else the order."""
    for kind in ("handoff_id", "item_id", "order_id"):
        if subject.get(kind):
            return f"{kind[:-3]}:{subject[kind]}"
    return ""


class GuardianStore:
    """The Guardian's state, in the case store's SQLite file (CASE_STORE_PATH)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else CaseStore().path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _watch(row: sqlite3.Row) -> dict[str, Any]:
        w = dict(row)
        w["subject"] = json.loads(w["subject"])
        w["milestones"] = json.loads(w["milestones"]) if w["milestones"] else None
        w["last_seen"] = json.loads(w["last_seen"]) if w["last_seen"] else {}
        return w

    def add_watch(self, *, case_id: str, issue_id: str, customer_id: str, domain: str, subject: dict[str, Any],
                  milestones: list[str] | None, now: str) -> str | None:
        """The new watch id, or None when the issue or the same subject is already watched."""
        key = subject_key(subject)
        with _LOCK, self._connect() as db:
            if db.execute("SELECT 1 FROM guardian_watches WHERE issue_id = ?", (issue_id,)).fetchone():
                return None
            if key and db.execute(
                    "SELECT 1 FROM guardian_watches WHERE customer_id = ? AND subject_key = ? AND state IN "
                    f"({','.join('?' * len(ACTIVE))})", (customer_id, key, *ACTIVE)).fetchone():
                return None
            watch_id = f"W-{uuid.uuid4().hex[:10].upper()}"
            db.execute("INSERT INTO guardian_watches (watch_id, case_id, issue_id, customer_id, domain, subject_key, "
                       "subject, milestones, state, created_at, step_started_at, step_since, next_check_at, "
                       "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (watch_id, case_id, issue_id, customer_id, domain, key, json.dumps(subject),
                        json.dumps(milestones) if milestones is not None else None, WATCHING, now, now, now, now,
                        now))
        return watch_id

    def update(self, watch_id: str, **fields: Any) -> None:
        for k in ("subject", "milestones", "last_seen"):
            if k in fields and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with _LOCK, self._connect() as db:
            db.execute(f"UPDATE guardian_watches SET {sets} WHERE watch_id = ?", (*fields.values(), watch_id))

    def get(self, watch_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM guardian_watches WHERE watch_id = ?", (watch_id,)).fetchone()
        return self._watch(row) if row else None

    def watches(self, *, customer_id: str | None = None, case_id: str | None = None,
                active_only: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM guardian_watches WHERE 1 = 1", []
        if customer_id:
            sql, args = sql + " AND customer_id = ?", [*args, customer_id]
        if case_id:
            sql, args = sql + " AND case_id = ?", [*args, case_id]
        if active_only:
            sql, args = sql + f" AND state IN ({','.join('?' * len(ACTIVE))})", [*args, *ACTIVE]
        with self._connect() as db:
            return [self._watch(r) for r in db.execute(sql + " ORDER BY created_at, rowid", args)]

    def due(self, now: str, customer_id: str | None = None) -> list[dict[str, Any]]:
        """Watches to check now (or every active one of a customer, after a simulated event)."""
        states = (WATCHING, WAITING_ON_BUSINESS, WAITING_ON_CUSTOMER, ESCALATED)
        sql = f"SELECT * FROM guardian_watches WHERE state IN ({','.join('?' * len(states))})"
        args: list[Any] = list(states)
        if customer_id:
            sql, args = sql + " AND customer_id = ?", [*args, customer_id]
        else:
            sql, args = sql + " AND next_check_at <= ?", [*args, now]
        with self._connect() as db:
            return [self._watch(r) for r in db.execute(sql + " ORDER BY created_at, rowid", args)]

    # ------------------------------------------------------------------ notices and preferences

    def add_notice(self, *, dedupe: str, watch: dict[str, Any], kind: str, final: bool, message: str,
                   now: str) -> dict[str, Any] | None:
        """A new notice in status 'new', or None when this exact update was already created (dedupe)."""
        notice_id = f"N-{uuid.uuid4().hex[:10].upper()}"
        with _LOCK, self._connect() as db:
            try:
                db.execute("INSERT INTO guardian_notices (notice_id, dedupe, watch_id, customer_id, case_id, kind, "
                           "final, message, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
                           (notice_id, dedupe, watch["watch_id"], watch["customer_id"], watch["case_id"], kind,
                            int(final), message, now))
            except sqlite3.IntegrityError:
                return None
            # A newer update replaces an older one of the same issue that is still waiting to be sent.
            db.execute("UPDATE guardian_notices SET status = 'superseded' WHERE watch_id = ? AND status = 'held' "
                       "AND final = 0 AND notice_id != ?", (watch["watch_id"], notice_id))
            return dict(db.execute("SELECT * FROM guardian_notices WHERE notice_id = ?", (notice_id,)).fetchone())

    def set_notice(self, notice_id: str, status: str, *, held_reason: str | None = None,
                   sent_at: str | None = None) -> None:
        with _LOCK, self._connect() as db:
            db.execute("UPDATE guardian_notices SET status = ?, held_reason = ?, sent_at = ? WHERE notice_id = ?",
                       (status, held_reason, sent_at, notice_id))

    def notices(self, *, customer_id: str | None = None, watch_id: str | None = None,
                status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM guardian_notices WHERE 1 = 1", []
        for col, value in (("customer_id", customer_id), ("watch_id", watch_id), ("status", status)):
            if value:
                sql, args = sql + f" AND {col} = ?", [*args, value]
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY created_at, rowid", args)]

    def opt_out(self, customer_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT updates_opt_out FROM guardian_preferences WHERE customer_id = ?",
                             (customer_id,)).fetchone()
        return bool(row and row[0])

    def set_opt_out(self, customer_id: str, opt_out: bool, now: str) -> None:
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO guardian_preferences VALUES (?, ?, ?) ON CONFLICT(customer_id) DO UPDATE SET "
                       "updates_opt_out = excluded.updates_opt_out, updated_at = excluded.updated_at",
                       (customer_id, int(opt_out), now))


# ---------------------------------------------------------------- creating watches (end of turn, no data reads)


def _plan(issue: dict[str, Any], handoffs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str] | None] | None:
    """(subject, milestones) for an issue the Guardian should follow, or None. Milestones None: decided later."""
    status = issue["status"]
    proposal = issue.get("proposal") or {}
    action = proposal.get("action")
    item, order = issue_subject(issue)
    if action and proposal.get("target_id"):
        if action in ITEM_ACTIONS:
            item = proposal["target_id"]
        else:
            order = proposal["target_id"]
    subject = {"item_id": item, "order_id": order, "action": action}
    if status == HANDED_OFF:
        handoff = next((h for h in reversed(handoffs) if issue["issue_id"] in h["issue_ids"]), None)
        if handoff is None:
            return None
        return {**subject, "handoff_id": handoff["handoff_id"]}, ["handoff_resolved"]
    if status == QUEUED_FOR_REVIEW:
        return subject, ["review_decided", *ACTION_MILESTONES.get(action, [])]
    if status == EXECUTED_VERIFIED and action in ACTION_MILESTONES:
        return subject, list(ACTION_MILESTONES[action])
    query = (issue.get("refs") or {}).get("item_query")
    if status == RESOLVED_INFO_ONLY and issue["domain"] in ANSWERED_DOMAINS and (item or order or query):
        return {**subject, **({"item_query": query} if not (item or order) else {})}, None
    return None


def watch_case(case_id: str, *, cases: CaseStore | None = None, store: GuardianStore | None = None,
               now: str | None = None) -> list[str]:
    """Called by the gateway after each turn: starts a watch for each issue the Guardian should follow."""
    cases = cases or CaseStore()
    case = cases.get_case(case_id)
    if not case or not case["customer_id"]:
        return []
    store = store or GuardianStore(cases.path)
    now = now or Simulator().now_ts()
    view = cases.case_view(case_id)
    started = []
    for issue in view["issues"]:
        planned = _plan(issue, view["handoffs"])
        if planned is None:
            continue
        subject, milestones = planned
        watch_id = store.add_watch(case_id=case_id, issue_id=issue["issue_id"], customer_id=case["customer_id"],
                                   domain=issue["domain"], subject=subject, milestones=milestones, now=now)
        if watch_id:
            started.append(watch_id)
            cases.log_event(case_id, issue["issue_id"], "guardian_watching",
                            {"watch_id": watch_id, "milestones": milestones})
    return started


def brief(watch: dict[str, Any], store: GuardianStore | None = None) -> dict[str, Any]:
    """What agents, assistants and the demo may show: what is watched, its state and when it was last checked."""
    milestones = watch["milestones"] or []
    waiting = milestones[watch["step"]] if watch["step"] < len(milestones) else None
    out = {"case_id": watch["case_id"], "issue_id": watch["issue_id"], "state": watch["state"],
           "waiting_for": WAITING_WORDS.get(waiting, "nothing" if watch["state"] in (RESOLVED, CLOSED) else
                                            "the first check"),
           "watching_since": watch["created_at"], "last_checked_at": watch["last_checked_at"],
           "with_a_person": bool(watch["escalation_handoff_id"]) or waiting == "handoff_resolved"}
    if store is not None:
        out["updates_sent"] = sum(1 for n in store.notices(watch_id=watch["watch_id"]) if n["status"] == "sent")
    if watch["closed_reason"]:
        out["closed_reason"] = watch["closed_reason"]
    return out


# ---------------------------------------------------------------- the watcher


class DataUnavailable(RuntimeError):
    pass


def _run_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _RUN_LOCKS:
        _RUN_LOCKS[loop] = asyncio.Lock()
    return _RUN_LOCKS[loop]


def _money(amount: float, currency: str | None) -> str:
    return f"${amount:.2f}" if (currency or "USD") == "USD" else f"{amount:.2f} {currency}"


def _titles(rows: list[dict[str, Any]]) -> str:
    titles = list(dict.fromkeys(f"{r['color'] + ' ' if r.get('color') else ''}{r['title']}".strip() for r in rows))
    return " and ".join(titles) or "order"


class Guardian:
    def __init__(self, *, cases: CaseStore | None = None, store: GuardianStore | None = None,
                 actions: ActionStore | None = None, adapter: Any = None, policy: dict[str, Any] | None = None,
                 simulator: Simulator | None = None) -> None:
        from coded_tools.customer_service.policy import load_policy

        self.cases = cases or CaseStore()
        self.store = store or GuardianStore(self.cases.path)
        self.actions = actions or ActionStore()
        self._adapter = adapter
        self.policy = policy or load_policy()
        self.settings = self.policy["guardian"]
        self.clock = simulator or Simulator(reference_date=self.policy["reference_date"])

    @property
    def adapter(self) -> Any:
        if self._adapter is None:
            from coded_tools.customer_service.data_adapter import get_adapter

            self._adapter = get_adapter()
        return self._adapter

    # ------------------------------------------------------------------ run

    async def run_once(self, customer_id: str | None = None) -> dict[str, Any]:
        """Checks every watch that is due (or all of one customer's), then sends updates that may go out now."""
        async with _run_lock():
            now = self.clock.now()
            summary: dict[str, Any] = {"clock": _ts(now), "checked": [], "postponed": [], "notices": [],
                                       "handoffs": []}
            self._release(now, summary)
            for watch in self.store.due(_ts(now), customer_id):
                try:
                    await self._check(watch, now, summary)
                    summary["checked"].append(watch["watch_id"])
                except DataUnavailable as e:  # nothing is decided on missing data: the next check tries again
                    self._schedule(watch, now, error=str(e))
                    summary["postponed"].append(watch["watch_id"])
            self._release(now, summary)
            return summary

    def _schedule(self, watch: dict[str, Any], now: datetime, **fields: Any) -> None:
        fields.pop("error", None)
        self.store.update(watch["watch_id"], last_checked_at=_ts(now), checks=watch["checks"] + 1,
                          next_check_at=_ts(now + timedelta(hours=float(self.settings["check_every_hours"]))),
                          updated_at=_ts(now), **fields)

    async def _rows(self, watch: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self.adapter.fetch("get_customer_context", watch["customer_id"], policy=self.policy)
        if result.failed:
            raise DataUnavailable(f"get_customer_context: {result.status}")
        items, order = watch["subject"].get("items"), watch["subject"].get("order_id")
        item = watch["subject"].get("item_id")
        return [r for r in result.rows if (r["item_id"] in items if items else
                                           (r["item_id"] == item if item else r["order_id"] == order))]

    async def _timeline(self, watch: dict[str, Any], order_id: str) -> list[dict[str, Any]]:
        result = await self.adapter.fetch("get_case_timeline", watch["customer_id"], order_id, policy=self.policy)
        if result.failed:
            raise DataUnavailable(f"get_case_timeline: {result.status}")
        return result.rows

    # ------------------------------------------------------------------ one check

    async def _check(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        if watch["milestones"] is None and not await self._classify(watch, now):
            return
        watch = self.store.get(watch["watch_id"])
        if watch["escalation_handoff_id"] and self._handoff_status(watch["escalation_handoff_id"]) == "resolved":
            self._finish(watch, now, summary, "handoff_resolved", reason="resolved_by_person",
                         notify=not self._person_wrote(watch["escalation_handoff_id"]))
            return
        milestone = watch["milestones"][watch["step"]]
        await getattr(self, f"_on_{milestone}")(watch, now, summary)

    async def _identify(self, watch: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """
        An issue known only by the customer's words ("blue jacket"): the item is found the way the agents find it
        (find_order_items, fresh). Only a unique match is watched, and only if it is not watched already.
        """
        result = await self.adapter.fetch("find_order_items", watch["customer_id"], watch["subject"]["item_query"],
                                          policy=self.policy)
        if result.failed:
            raise DataUnavailable(f"find_order_items: {result.status}")
        matches = {(r["item_id"], r["order_id"]) for r in result.rows}
        if len(matches) != 1:
            self._schedule(watch, now, state=CLOSED, closed_reason="item_unclear", milestones=[])
            return None
        ((item, order),) = matches
        key = subject_key({"item_id": item})
        if any(w["subject_key"] == key and w["watch_id"] != watch["watch_id"]
               for w in self.store.watches(customer_id=watch["customer_id"], active_only=True)):
            self._schedule(watch, now, state=CLOSED, closed_reason="already_watched", milestones=[])
            return None
        subject = {**watch["subject"], "item_id": item, "order_id": order}
        self.store.update(watch["watch_id"], subject=subject, subject_key=key)
        return {**watch, "subject": subject, "subject_key": key}

    async def _classify(self, watch: dict[str, Any], now: datetime) -> bool:
        """For an answered issue: what is still in progress on its item or order (fresh data)."""
        if not (watch["subject"].get("item_id") or watch["subject"].get("order_id")):
            watch = await self._identify(watch, now)
            if watch is None:
                return False
        rows = await self._rows(watch)
        refunds = [r for r in rows if r.get("refund_status") in ("pending", "processing")]
        returns = [r for r in rows if r.get("return_status") in ("label_created", "in_transit")
                   and r.get("refund_status") != "completed"]
        parcels = [r for r in rows if r.get("tracking_no") and r.get("shipment_status") != "delivered"
                   and r.get("order_status") != "cancelled"]
        if watch["domain"] == "returns_refunds" and (returns or refunds):
            chosen, milestones = (returns, ["return_received", "refund_completed"]) if returns else \
                (refunds, ["refund_completed"])
            since = max(filter(None, [r.get(k) for r in chosen for k in
                                      ("return_label_created_at", "return_warehouse_scanned_at",
                                       "refund_initiated_at")]), default=watch["created_at"])
        elif watch["domain"] == "order_delivery" and parcels:
            chosen, milestones = parcels, ["parcel_delivered"]
            since = max((r["last_shipment_event_at"] for r in chosen if r.get("last_shipment_event_at")),
                        default=watch["created_at"])
        else:
            self._schedule(watch, now, state=CLOSED, closed_reason="nothing_pending", milestones=[])
            return False
        subject = {**watch["subject"], "items": sorted({r["item_id"] for r in chosen})}
        self.store.update(watch["watch_id"], subject=subject, milestones=milestones, step_since=since)
        return True

    def _wait(self, watch: dict[str, Any], now: datetime, milestone: str, **fields: Any) -> None:
        state = ESCALATED if watch["escalation_handoff_id"] else \
            WAITING_ON_CUSTOMER if milestone == "return_received" else WAITING_ON_BUSINESS
        sla_due = watch["sla_due"] or _ts(parse_ts(watch["step_started_at"]) +
                                          timedelta(hours=float(self.settings["sla_hours"][milestone])))
        self._schedule(watch, now, state=state, sla_due=fields.pop("sla_due", sla_due), **fields)

    def _sla_breached(self, watch: dict[str, Any], now: datetime, milestone: str) -> bool:
        due = watch["sla_due"] or _ts(parse_ts(watch["step_started_at"]) +
                                      timedelta(hours=float(self.settings["sla_hours"][milestone])))
        return _ts(now) >= due and not watch["escalation_handoff_id"]

    def _advance(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any], kind: str | None,
                 facts: dict[str, Any]) -> None:
        """The current milestone was reached: the next one starts, or the watch is resolved."""
        step = watch["step"] + 1
        if step >= len(watch["milestones"]):
            self._finish({**watch, "step": step}, now, summary, kind, reason="resolved_verified", facts=facts,
                         notify=kind is not None)
            return
        if kind:
            self._notify(watch, now, summary, kind, facts, marker=f"step{watch['step']}")
        nxt = watch["milestones"][step]
        self.store.update(watch["watch_id"], step=step, step_started_at=_ts(now), step_since=_ts(now), sla_due=None,
                          delay_explained=0, unconfirmed=0)
        self._wait({**self.store.get(watch["watch_id"])}, now, nxt)

    def _finish(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any], kind: str | None, *,
                reason: str, facts: dict[str, Any] | None = None, notify: bool = True) -> None:
        notice = self._notify(watch, now, summary, kind, facts or {}, marker="final", final=True) \
            if notify and kind else None
        state = RESOLVED if notice and notice["status"] == "held" else CLOSED
        self._schedule(watch, now, state=state, step=watch["step"], closed_reason=reason)
        self.cases.log_event(watch["case_id"], watch["issue_id"], "guardian_resolved" if reason == "resolved_verified"
                             else "guardian_closed", {"watch_id": watch["watch_id"], "reason": reason})

    def _escalate(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any], reason: str, notice: str,
                  facts: dict[str, Any], why: str, rows: list[dict[str, Any]] | None = None) -> None:
        """A hand-off to a person with the case packet. Nothing is re-tried or changed (decision 5)."""
        case = self.cases.get_case(watch["case_id"]) or {}
        issue = next((i for i in self.cases.case_view(watch["case_id"])["issues"]
                      if i["issue_id"] == watch["issue_id"]), {})
        packet = {"reason": reason, "customer_id": watch["customer_id"], "channel": case.get("channel"),
                  "identity_status": case.get("identity_status"), "auth_level": case.get("auth_level"),
                  "policy_version": self.policy.get("version"), "reference_date": case.get("reference_date"),
                  "issues": [{k: issue.get(k) for k in ("issue_id", "domain", "kind", "summary", "refs", "status",
                                                        "proposal", "outcome", "findings")}],
                  "agent_summary": f"Resolution Guardian: {why}",
                  "agent_next_step": "Check the records below and contact the customer. The Guardian keeps watching "
                                     "and closes its watch when the outcome is confirmed or this hand-off is resolved.",
                  "guardian": brief(watch), "records": rows or []}
        handoff_id = self.cases.add_handoff(watch["case_id"], [watch["issue_id"]], reason, packet)
        summary["handoffs"].append(handoff_id)
        self.store.update(watch["watch_id"], escalation_handoff_id=handoff_id, state=ESCALATED)
        self.cases.log_event(watch["case_id"], watch["issue_id"], "guardian_handoff",
                             {"watch_id": watch["watch_id"], "handoff_id": handoff_id, "reason": reason})
        self._notify({**watch, "escalation_handoff_id": handoff_id}, now, summary, notice, facts, marker=reason)

    def _delay_due(self, watch: dict[str, Any], now: datetime, milestone: str, last_activity: str | None) -> bool:
        hours = (self.settings.get("delay_hours") or {}).get(milestone)
        if hours is None or watch["delay_explained"]:
            return False
        baseline = max(filter(None, [watch["step_since"], last_activity]))
        return now - parse_ts(baseline) >= timedelta(hours=float(hours))

    # ------------------------------------------------------------------ milestones

    async def _on_review_decided(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        status = self.cases.get_issue(watch["case_id"], watch["issue_id"])["status"]
        if status == QUEUED_FOR_REVIEW:
            if self._sla_breached(watch, now, "review_decided"):
                self._escalate(watch, now, summary, "guardian_sla_breached", "escalated", self._case_facts(watch),
                               "the review has waited longer than the target; please decide it.")
            self._wait(self.store.get(watch["watch_id"]), now, "review_decided")
        elif status == EXECUTED_VERIFIED:  # approved: the review console already told the customer
            self._advance(watch, now, summary, None, {})
        else:  # rejected (the review console told the customer) or moved on otherwise
            self._finish(watch, now, summary, None, reason="review_rejected" if status == REVIEW_REJECTED else
                         f"issue_{status}", notify=False)

    async def _on_handoff_resolved(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        handoff_id = watch["subject"].get("handoff_id")
        status = self._handoff_status(handoff_id)
        if status == "resolved":
            self._finish(watch, now, summary, "handoff_resolved", reason="resolved_by_person",
                         facts=self._case_facts(watch), notify=not self._person_wrote(handoff_id))
        elif status is None:
            self._finish(watch, now, summary, None, reason="no_handoff", notify=False)
        else:
            if self._sla_breached(watch, now, "handoff_resolved"):
                self._escalate(watch, now, summary, "guardian_sla_breached", "escalated", self._case_facts(watch),
                               f"hand-off {handoff_id} has been open longer than the target.")
            self._wait(self.store.get(watch["watch_id"]), now, "handoff_resolved")

    async def _on_return_received(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        rows = await self._rows(watch)
        if rows and all(r.get("return_status") in ("received", "inspected") or r.get("refund_status") for r in rows):
            self._advance(watch, now, summary, "return_received", self._facts(watch, rows))
            return
        if self._sla_breached(watch, now, "return_received"):
            self._escalate(watch, now, summary, "guardian_sla_breached", "escalated", self._facts(watch, rows),
                           "the return has not reached the warehouse within the target.", rows)
        self._wait(self.store.get(watch["watch_id"]), now, "return_received")

    async def _on_refund_completed(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        rows = await self._rows(watch)
        facts = self._facts(watch, rows)
        if any(r.get("refund_status") == "failed" for r in rows):
            if not watch["escalation_handoff_id"]:
                self._escalate(watch, now, summary, "guardian_refund_failed", "refund_failed", facts,
                               "the refund failed (" + ", ".join(sorted({r.get("refund_failure_reason") or "unknown"
                                                                        for r in rows})) + "); it was not re-tried.",
                               rows)
            self._wait(self.store.get(watch["watch_id"]), now, "refund_completed")
            return
        if rows and all(r.get("refund_status") == "completed" for r in rows):
            if await self._refund_confirmed(watch, rows):
                self._advance(watch, now, summary, "refund_completed", facts)
                return
            await self._unconfirmed(watch, now, summary, "refund_completed", facts, rows)
            return
        last = max(filter(None, [r.get(k) for r in rows for k in ("return_warehouse_scanned_at",
                                                                     "refund_initiated_at")]), default=None)
        if self._delay_due(watch, now, "refund_completed", last):
            baseline = max(filter(None, [last, watch["step_since"]]))
            self._notify(watch, now, summary, "refund_delayed",
                         {**facts, "days": max(1, (now - parse_ts(baseline)).days)}, marker="delay")
            self.store.update(watch["watch_id"], delay_explained=1)
        if self._sla_breached(watch, now, "refund_completed"):
            self._escalate(watch, now, summary, "guardian_sla_breached", "escalated", facts,
                           "the refund has not completed within the target.", rows)
        self._wait(self.store.get(watch["watch_id"]), now, "refund_completed")

    async def _on_parcel_delivered(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        rows = [r for r in await self._rows(watch) if r.get("tracking_no")]
        facts = self._facts(watch, rows)
        if rows and all(r.get("shipment_status") == "delivered" for r in rows):
            if await self._delivery_confirmed(watch, rows):
                self._advance(watch, now, summary, "parcel_delivered", facts)
                return
            await self._unconfirmed(watch, now, summary, "parcel_delivered", facts, rows)
            return
        last_scan = max((r["last_shipment_event_at"] for r in rows if r.get("last_shipment_event_at")), default=None)
        seen = watch["last_seen"].get("scan")
        fields: dict[str, Any] = {}
        if last_scan and seen and last_scan > seen:  # the parcel moved: an update, and the targets start again
            latest = max(rows, key=lambda r: r.get("last_shipment_event_at") or "")
            self._notify(watch, now, summary, "parcel_scanned",
                         {**facts, "location": latest.get("last_shipment_location") or "transit"}, marker=last_scan)
            fields.update(delay_explained=0, step_since=_ts(now),
                          sla_due=_ts(now + timedelta(hours=float(self.settings["sla_hours"]["parcel_delivered"]))))
            watch = {**watch, "delay_explained": 0, "step_since": _ts(now), "sla_due": fields["sla_due"]}
        if last_scan:
            fields["last_seen"] = {**watch["last_seen"], "scan": last_scan}
        if self._delay_due(watch, now, "parcel_delivered", last_scan):
            baseline = last_scan or watch["step_since"]
            self._notify(watch, now, summary, "parcel_delayed",
                         {**facts, "days": max(1, (now - parse_ts(baseline)).days)}, marker=f"delay{baseline}")
            fields["delay_explained"] = 1
        if fields:
            self.store.update(watch["watch_id"], **fields)
        watch = self.store.get(watch["watch_id"])
        if self._sla_breached(watch, now, "parcel_delivered"):
            self._escalate(watch, now, summary, "guardian_sla_breached", "escalated", facts,
                           "the parcel has not been delivered within the target.", rows)
        self._wait(self.store.get(watch["watch_id"]), now, "parcel_delivered")

    # ------------------------------------------------------------------ read-back

    async def _refund_confirmed(self, watch: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
        """Every item's refund completion also appears on its order's timeline (an independent read)."""
        for order_id in sorted({r["order_id"] for r in rows}):
            events = await self._timeline(watch, order_id)
            for r in (r for r in rows if r["order_id"] == order_id):
                if not any(e["event_type"] == "refund_completed" and
                           (e.get("item_id") == r["item_id"] or
                            (e.get("item_id") is None and e["event_at"] >= watch["created_at"]))
                           for e in events):
                    return False
        return True

    async def _delivery_confirmed(self, watch: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
        for order_id in sorted({r["order_id"] for r in rows}):
            events = await self._timeline(watch, order_id)
            for tracking in {r["tracking_no"] for r in rows if r["order_id"] == order_id}:
                if not any(e["event_type"] == "shipment_delivered" and tracking in (e.get("detail") or "")
                           for e in events):
                    return False
        return True

    async def _unconfirmed(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any], milestone: str,
                           facts: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        count = watch["unconfirmed"] + 1
        self.store.update(watch["watch_id"], unconfirmed=count)
        self.cases.log_event(watch["case_id"], watch["issue_id"], "guardian_unconfirmed",
                             {"watch_id": watch["watch_id"], "milestone": milestone, "checks": count})
        if count >= int(self.settings["unconfirmed_checks_before_handoff"]) and not watch["escalation_handoff_id"]:
            self._escalate(watch, now, summary, "guardian_unconfirmed", "escalated", facts,
                           f"the account shows {milestone.replace('_', ' ')} but the order timeline does not "
                           f"confirm it after {count} checks; nothing was closed.", rows)
        self._wait(self.store.get(watch["watch_id"]), now, milestone)

    # ------------------------------------------------------------------ facts, people, updates

    def _case_facts(self, watch: dict[str, Any]) -> dict[str, Any]:
        return {"case_id": watch["case_id"]}

    def _facts(self, watch: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        amounts = [r["refund_amount"] for r in rows if r.get("refund_amount") is not None]
        methods = {r.get("refund_method") for r in rows if r.get("refund_method")}
        carriers = sorted({r["carrier"] for r in rows if r.get("carrier")})
        locations = [r.get("last_shipment_location") for r in rows if r.get("last_shipment_location")]
        return {"case_id": watch["case_id"], "title": _titles(rows),
                "amount": _money(sum(amounts), rows[0].get("currency")) if amounts else "the amount due",
                "method": METHOD_WORDS.get(next(iter(methods)), "") if len(methods) == 1 else "",
                "carrier": " and ".join(carriers) or "The carrier", "location": locations[-1] if locations else
                "transit", "days": 0}

    def _handoff_status(self, handoff_id: str | None) -> str | None:
        found = [h for h in self.cases.handoffs(status=None) if h["handoff_id"] == handoff_id]
        return found[0]["status"] if found else None

    def _person_wrote(self, handoff_id: str) -> bool:
        """The colleague already sent the customer a message when resolving the hand-off (review console)."""
        return any(n.get("review_id") == handoff_id for n in self.actions.read(OUTBOX))

    def _hold_reason(self, customer_id: str, now: datetime) -> str | None:
        """Quiet hours and the customer's daily limit, shared with the Stylist's invites (messaging.py)."""
        return messaging.service_hold_reason(self.store.path, customer_id, now, self.policy)

    def _notify(self, watch: dict[str, Any], now: datetime, summary: dict[str, Any], kind: str,
                facts: dict[str, Any], *, marker: str, final: bool = False) -> dict[str, Any] | None:
        message = TEMPLATES[kind].format(**{"case_id": watch["case_id"], **facts})
        notice = self.store.add_notice(dedupe=f"{watch['watch_id']}:{kind}:{marker}", watch=watch, kind=kind,
                                       final=final or kind in FINAL_NOTICES, message=message, now=_ts(now))
        if notice is None:
            return None
        if self.store.opt_out(watch["customer_id"]):
            self.store.set_notice(notice["notice_id"], "suppressed", held_reason="opted_out")
            notice["status"] = "suppressed"
        else:
            reason = self._hold_reason(watch["customer_id"], now)
            if reason:
                self.store.set_notice(notice["notice_id"], "held", held_reason=reason)
                notice["status"] = "held"
            else:
                self._send(notice, now)
        summary["notices"].append({k: notice[k] for k in ("notice_id", "kind", "status")})
        return notice

    def _send(self, notice: dict[str, Any], now: datetime) -> None:
        self.store.set_notice(notice["notice_id"], "sent", sent_at=_ts(now))
        notice["status"] = "sent"
        self.actions.append(OUTBOX, {"customer_id": notice["customer_id"], "support_case_id": notice["case_id"],
                                     "source": "guardian", "watch_id": notice["watch_id"],
                                     "notice_id": notice["notice_id"], "kind": notice["kind"],
                                     "message": notice["message"], "sent_at_clock": _ts(now),
                                     "status": "pending_send"})

    def _release(self, now: datetime, summary: dict[str, Any]) -> None:
        """Held updates that may go out now (oldest first); a watch waiting on its closing update closes."""
        for notice in self.store.notices(status="held"):
            if self._hold_reason(notice["customer_id"], now):
                continue
            if self.store.opt_out(notice["customer_id"]):
                self.store.set_notice(notice["notice_id"], "suppressed", held_reason="opted_out")
            else:
                self._send(notice, now)
                summary["notices"].append({"notice_id": notice["notice_id"], "kind": notice["kind"],
                                           "status": "sent"})
            watch = self.store.get(notice["watch_id"])
            if notice["final"] and watch and watch["state"] == RESOLVED:
                self.store.update(watch["watch_id"], state=CLOSED, updated_at=_ts(now))

    # ------------------------------------------------------------------ for gateways and the demo

    def set_opt_out(self, customer_id: str, opt_out: bool, *, channel: str = "web") -> None:
        """The customer's choice is their companion's service_updates consent, which also sets the opt-out here."""
        from coded_tools.customer_service.companion import SERVICE_UPDATES
        from coded_tools.customer_service.companion import CompanionStore

        CompanionStore(self.store.path).set_consent(customer_id, SERVICE_UPDATES, not opt_out, channel=channel)


# ---------------------------------------------------------------- simulated events (demo console, CLI, evaluation)

# What can happen next to a watch, by the milestone it waits for. Business events go to the simulator; review and
# hand-off decisions go through the review service, as a person in the review console would make them.
SIM_EVENTS = {
    "refund_completed": ["refund_completed", "refund_completed_unconfirmed", "refund_failed"],
    "return_received": ["return_received"],
    "parcel_delivered": ["parcel_scanned", "parcel_delivered"],
    "review_decided": ["review_approved", "review_rejected"],
    "handoff_resolved": ["handoff_resolved"],
}
SIM_LABELS = {"refund_completed": "Refund completes", "refund_completed_unconfirmed": "Refund shows completed "
              "(timeline lags)", "refund_failed": "Refund fails", "return_received": "Return arrives",
              "parcel_scanned": "Carrier scan", "parcel_delivered": "Parcel delivered",
              "review_approved": "Reviewer approves", "review_rejected": "Reviewer rejects",
              "handoff_resolved": "Colleague resolves"}


def available_events(watch: dict[str, Any]) -> list[str]:
    if watch["state"] not in (WAITING_ON_BUSINESS, WAITING_ON_CUSTOMER, ESCALATED) or not watch["milestones"]:
        return []
    events = list(SIM_EVENTS[watch["milestones"][watch["step"]]])
    if watch["escalation_handoff_id"] and "handoff_resolved" not in events:
        events.append("handoff_resolved")
    return events


async def simulate_event(guard: Guardian, watch_id: str, event: str, *, actor: str = "demo (simulated)",
                         customer_id: str | None = None, check: bool = True) -> dict[str, Any]:
    """
    Makes one thing happen to a watched issue, then checks that customer's watches. customer_id, when given, must
    own the watch (the demo console passes the signed-in customer's). Raises ValueError for anything not allowed.
    check=False: no check here; the caller has Orbit's clock tick check that customer (the demo console does).
    """
    from coded_tools.customer_service.review_service import ReviewService

    watch = guard.store.get(watch_id)
    if watch is None or (customer_id and watch["customer_id"] != customer_id):
        raise ValueError("no such watch for this customer")
    if event not in available_events(watch):
        raise ValueError(f"{event} cannot happen to this watch now; possible: {', '.join(available_events(watch))}")
    reviews = ReviewService(guard.actions, guard.cases, guard.adapter, guard.policy)
    if event in ("review_approved", "review_rejected"):
        item = next((i for i in reviews.queue() if i.get("support_issue_id") == watch["issue_id"]
                     and i["type"] == "action_review"), None)
        if item is None:
            raise ValueError("no open review for this issue")
        if event == "review_approved":
            await reviews.approve(item["review_id"], reviewer=actor, note="simulated approval")
        else:
            reviews.reject(item["review_id"], reviewer=actor, reason="simulated rejection")
    elif event == "handoff_resolved":
        handoff_id = watch["escalation_handoff_id"] or watch["subject"].get("handoff_id")
        reviews.resolve(handoff_id, reviewer=actor, note="simulated: a colleague dealt with it")
    else:
        rows = await guard._rows(watch)
        kind = "refund_completed" if event == "refund_completed_unconfirmed" else event
        if kind in ("parcel_scanned", "parcel_delivered"):
            targets = sorted({r["order_id"] for r in rows if r.get("tracking_no")})
        else:
            targets = sorted({r["item_id"] for r in rows})
        for target in targets:
            guard.clock.add_event(kind, target=target, confirmed=event != "refund_completed_unconfirmed")
    if not check:
        return {"watch_id": watch_id, "event": event, "customer_id": watch["customer_id"]}
    return await guard.run_once(customer_id=watch["customer_id"])


def customer_watching(customer_id: str | None, *, cases: CaseStore | None = None,
                      case_id: str | None = None) -> list[dict[str, Any]]:
    """The customer's active watches as briefs (empty for an unidentified contact)."""
    if not customer_id:
        return []
    store = GuardianStore((cases or CaseStore()).path)
    return [brief(w, store) for w in store.watches(customer_id=customer_id, case_id=case_id, active_only=True)]
