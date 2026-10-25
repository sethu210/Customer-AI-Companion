"""
Append-only local stores for write actions (a stand-in for real systems until Phase 6).

    decisions.jsonl  audit log: every policy decision, with the facts it was based on
    actions.jsonl    one record per action outcome (executed / queued / awaiting confirmation); used for idempotency
    cases.jsonl      hand-off cases for people: approvals, specialist reviews, shadow-mode reviews, customer requests
    reviews.jsonl    a person's decision on each review item (review_service.py)
    outbox.jsonl     customer notifications waiting for a channel gateway to send them
    executed.jsonl   receipts from the (mock) system of record
    actions.db       claims (SQLite): one row per idempotency key, inserted BEFORE an action is executed or queued,
                     so two requests, in any process, can never both act on the same customer + action + target
                     (review finding P4); also the running total of automatic refunds behind the cumulative limits

Environment:
    ACTION_STORE_DIR   default <project>/runtime/customer_service
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from coded_tools.customer_service.policy import ALLOW
from coded_tools.customer_service.policy import REFUND_ACTIONS
from coded_tools.customer_service.policy import PolicyDecision

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()

# Outcomes that mean "this action is already handled"; a repeat request returns the same reference.
FINAL_OUTCOMES = {"executed", "queued_for_review", "queued_for_approval", "escalated"}
DAY_S, MONTH_S = 86400, 30 * 86400

CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_claims (
    key TEXT PRIMARY KEY, reference TEXT NOT NULL, customer_id TEXT NOT NULL, action TEXT NOT NULL,
    target_id TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, decision TEXT NOT NULL, outcome TEXT NOT NULL,
    case_id TEXT, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_claims_customer ON action_claims (customer_id, at);
CREATE TABLE IF NOT EXISTS review_claims (review_id TEXT PRIMARY KEY, reviewer TEXT, at REAL NOT NULL);
"""


class OverLimit(Exception):
    """The automatic refunds of the last day or 30 days plus this one would pass a cumulative limit."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def idempotency_key(customer_id: str, action: str, target_id: str) -> str:
    return hashlib.sha256(f"{customer_id}|{action}|{target_id}".encode()).hexdigest()


class ActionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = Path(directory or os.environ.get("ACTION_STORE_DIR", PROJECT_ROOT / "runtime" / "customer_service"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "actions.db"
        new = not self.db_path.exists()
        with self._db() as db:
            db.executescript(CLAIMS_SCHEMA)
        if new:  # a store from before the claims table: its final actions are claimed, so they are never repeated
            for a in self._read("actions.jsonl"):
                if a.get("outcome") in FINAL_OUTCOMES:
                    self._insert_claim(a["key"], a, a["outcome"], at=time.time(), replace=True)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)  # explicit transactions below
        db.row_factory = sqlite3.Row
        return db

    def _insert_claim(self, key: str, fields: dict[str, Any], outcome: str, *, at: float, replace: bool = False,
                      db: sqlite3.Connection | None = None) -> None:
        sql = (f"INSERT {'OR REPLACE ' if replace else ''}INTO action_claims (key, reference, customer_id, action, "
               "target_id, amount, decision, outcome, case_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        args = (key, fields["reference"], fields["customer_id"], fields["action"], fields["target_id"],
                float(fields.get("amount") or 0), fields.get("decision") or "", outcome, fields.get("case_id"), at)
        if db is not None:
            db.execute(sql, args)
            return
        with self._db() as own:
            own.execute(sql, args)

    # ------------------------------------------------------------------ claims (idempotency, cumulative limits)

    def claim(self, key: str, *, reference: str, customer_id: str, decision: PolicyDecision, outcome: str,
              limits: tuple[float, float] | None = None) -> dict[str, Any] | None:
        """
        Claims the key for this outcome, atomically and before anything is executed or queued. Returns None when this
        call claimed it, else the claim that was already there (then nothing may be done). limits (daily, 30-day):
        for an automatic refund, the cumulative check runs in the same transaction; passing one raises OverLimit and
        nothing is claimed.
        """
        fields = {"reference": reference, "customer_id": customer_id, "action": decision.action,
                  "target_id": decision.target_id, "amount": decision.amount, "decision": decision.decision}
        now = time.time()
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")  # one writer at a time, across processes
            row = db.execute("SELECT * FROM action_claims WHERE key = ?", (key,)).fetchone()
            if row is not None:
                db.execute("ROLLBACK")
                return dict(row)
            if limits is not None:
                day, month = self._auto_refunds(db, customer_id, now)
                if day + decision.amount > limits[0] or month + decision.amount > limits[1]:
                    db.execute("ROLLBACK")
                    raise OverLimit(f"automatic refunds: {day:.2f} today, {month:.2f} in 30 days")
            self._insert_claim(key, fields, outcome, at=now, db=db)
            db.execute("COMMIT")
            return None
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _auto_refunds(db: sqlite3.Connection, customer_id: str, now: float) -> tuple[float, float]:
        marks = ",".join("?" * len(REFUND_ACTIONS))
        rows = db.execute(f"SELECT amount, at FROM action_claims WHERE customer_id = ? AND decision = ? AND action IN "
                          f"({marks}) AND at >= ?", (customer_id, ALLOW, *sorted(REFUND_ACTIONS), now - MONTH_S))
        amounts = [(r["amount"], r["at"]) for r in rows]
        return sum(a for a, at in amounts if at >= now - DAY_S), sum(a for a, _ in amounts)

    def customer_references(self, customer_id: str) -> set[str]:
        """The customer's action references (ACT-...) and review cases (CASE-...), for the reply check."""
        with self._db() as db:
            rows = db.execute("SELECT reference, case_id FROM action_claims WHERE customer_id = ?", (customer_id,))
            return {v for r in rows for v in (r["reference"], r["case_id"]) if v}

    def claim_review(self, review_id: str, reviewer: str) -> bool:
        """True for the one decision on a review item that may act; two reviewers at once cannot both execute it."""
        try:
            with self._db() as db:
                db.execute("INSERT INTO review_claims VALUES (?, ?, ?)", (review_id, reviewer, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def auto_refunds(self, customer_id: str) -> tuple[float, float]:
        """The customer's automatic refund amounts in the last day and the last 30 days."""
        with self._db() as db:
            return self._auto_refunds(db, customer_id, time.time())

    def _append(self, name: str, record: dict[str, Any]) -> None:
        with _LOCK, (self.dir / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _read(self, name: str) -> list[dict[str, Any]]:
        path = self.dir / name
        if not path.exists():
            return []
        with _LOCK:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def audit(self, customer_id: str | None, channel: str | None, auth_level: str | None, decision: PolicyDecision,
              mode: str, outcome: str, reference: str | None, **link: Any) -> None:
        self._append("decisions.jsonl", {
            "at": _now(), "customer_id": customer_id, "channel": channel, "auth_level": auth_level,
            "mode": mode, "outcome": outcome, "reference": reference, **link, **decision.to_dict(),
        })

    def find_final(self, key: str) -> dict[str, Any] | None:
        """The claim of an action already executed or queued (an indexed read, not a scan of the log)."""
        with self._db() as db:
            row = db.execute("SELECT * FROM action_claims WHERE key = ?", (key,)).fetchone()
        return dict(row) if row is not None and row["outcome"] in FINAL_OUTCOMES else None

    def record_action(self, key: str, customer_id: str, decision: PolicyDecision, outcome: str,
                      reference: str, case_id: str | None = None, receipt: dict | None = None,
                      link: dict[str, Any] | None = None) -> dict[str, Any]:
        """case_id is this store's review case; link holds the support case/issue ids (stored as support_*)."""
        link = link or {}
        record = {"at": _now(), "key": key, "reference": reference, "customer_id": customer_id,
                  "action": decision.action, "target_id": decision.target_id, "amount": decision.amount,
                  "decision": decision.decision, "outcome": outcome, "case_id": case_id, "receipt": receipt,
                  **{f"support_{k}": v for k, v in link.items()}}
        self._append("actions.jsonl", record)
        if outcome in FINAL_OUTCOMES:  # the claim now carries the final outcome (a review's execution included)
            with self._db() as db:
                cur = db.execute("UPDATE action_claims SET outcome = ?, case_id = COALESCE(?, case_id) WHERE key = ?",
                                 (outcome, case_id, key))
                if cur.rowcount == 0:
                    self._insert_claim(key, record, outcome, at=time.time(), db=db)
        return record

    def open_case(self, customer_id: str, kind: str, summary: str, proposal: dict | None = None,
                  key: str | None = None) -> str:
        case_id = f"CASE-{(key or uuid.uuid4().hex)[:10].upper()}"
        if key and any(c["case_id"] == case_id for c in self._read("cases.jsonl")):
            return case_id
        self._append("cases.jsonl", {"at": _now(), "case_id": case_id, "customer_id": customer_id, "kind": kind,
                                     "status": "open", "summary": summary, "proposal": proposal})
        return case_id

    def cases(self) -> list[dict[str, Any]]:
        return self._read("cases.jsonl")

    def actions(self) -> list[dict[str, Any]]:
        return self._read("actions.jsonl")

    def append(self, name: str, record: dict[str, Any]) -> None:
        """Appends to another log in this store (reviews.jsonl, outbox.jsonl)."""
        self._append(name, {"at": _now(), **record})

    def read(self, name: str) -> list[dict[str, Any]]:
        return self._read(name)

    def executed_receipt(self, reference: str) -> dict[str, Any] | None:
        """Read-back from the system of record: the receipt for this reference, if it really executed."""
        matches = [r for r in self._read("executed.jsonl") if r.get("reference") == reference]
        return matches[-1] if matches else None


class MockSystemOfRecord:
    """Stand-in for the order/payment/carrier systems. Only accepts actions the policy engine ALLOWed."""

    def __init__(self, store: ActionStore) -> None:
        self.store = store

    def execute(self, decision: PolicyDecision, reference: str) -> dict[str, Any]:
        if decision.decision != ALLOW or decision.requires_confirmation:
            raise PermissionError(f"refusing to execute {decision.action}: policy decision is {decision.decision}")
        receipt = {"system": "mock", "reference": reference, "action": decision.action,
                   "target_id": decision.target_id, "amount": decision.amount, "at": _now()}
        # Fault injection for tests: SYSTEM_OF_RECORD_FAULT=drop_receipt acknowledges the call but records nothing,
        # like a downstream system that accepted a request and lost it. Verification must catch this.
        if os.environ.get("SYSTEM_OF_RECORD_FAULT", "").strip().lower() != "drop_receipt":
            self.store._append("executed.jsonl", receipt)
        return receipt
