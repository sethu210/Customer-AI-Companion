"""
Shared case state for the multi-agent network, in a local SQLite file.

A case is one customer contact. It holds one or more issues (one per thing the customer wants), each issue's
evidence (findings), the actions requested for it and any hand-off to a person. Agents never keep this in
conversational memory: they read and update it through the coded tools, which enforce the rules below.

Issue status lifecycle:
    open -> investigating -> proposed -> executed_verified | queued_for_review | handed_off | resolved_info_only

    * The LLM may only move an issue to investigating, proposed or resolved_info_only (update_issue), and
      resolved_info_only needs at least one finding recorded by a tool.
    * executed_verified and queued_for_review are set only by the action layer; handed_off only by a hand-off.
    * A case counts as resolved only when every issue is executed_verified or resolved_info_only.

Environment:
    CASE_STORE_PATH   default <project>/runtime/customer_service/cases.db
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()

DOMAINS = ("order_delivery", "returns_refunds", "payments_billing", "loyalty_account", "account_identity", "shopping",
           "companion", "stylist", "other")

# The issue kinds intake may record, per domain. A fixed list, because code keys on them (ACTION_KINDS in
# orchestrator_tools): a cancellation recorded under a free-text label would escape the "needs a decision" guard.
ISSUE_KINDS = {
    "order_delivery": ("where_is_order", "delivery_problem", "missing_item", "cancel_request"),
    "returns_refunds": ("return_request", "return_status", "refund_status"),
    "payments_billing": ("duplicate_charge", "billing_question"),
    "loyalty_account": ("points_balance", "missing_points", "tier_question"),
    "account_identity": ("account_change", "sign_in_problem"),
    # The customer asks for product suggestions; only such an issue can get them (recommendation_tools).
    "shopping": ("product_question",),
    # The customer asks their companion to remember or forget a preference (companion_tools, P3).
    "companion": ("preference",),
    # The customer asks about a Personal Stylist pick, or says one is not for them (stylist_tools, P6).
    "stylist": ("pick_question", "pick_feedback"),
    "other": ("human_requested", "question"),
}
DOMAIN_OF_KIND = {kind: domain for domain, kinds in ISSUE_KINDS.items() for kind in kinds}

OPEN = "open"
INVESTIGATING = "investigating"
PROPOSED = "proposed"
AWAITING_CUSTOMER = "awaiting_customer"  # the orchestrator asked the customer a question (e.g. which item)
EXECUTED_VERIFIED = "executed_verified"
QUEUED_FOR_REVIEW = "queued_for_review"
HANDED_OFF = "handed_off"
RESOLVED_INFO_ONLY = "resolved_info_only"
REVIEW_REJECTED = "review_rejected"  # a person reviewed a queued action and declined it

TERMINAL = {EXECUTED_VERIFIED, QUEUED_FOR_REVIEW, HANDED_OFF, RESOLVED_INFO_ONLY, REVIEW_REJECTED}
RESOLVED = {EXECUTED_VERIFIED, RESOLVED_INFO_ONLY}
LLM_SETTABLE = {INVESTIGATING, PROPOSED, RESOLVED_INFO_ONLY, AWAITING_CUSTOMER}

# Which status may follow which. Terminal statuses are final; a reopened need is a new issue.
_WORKING = {INVESTIGATING, PROPOSED, RESOLVED_INFO_ONLY, QUEUED_FOR_REVIEW, EXECUTED_VERIFIED, HANDED_OFF}
TRANSITIONS: dict[str, set[str]] = {
    OPEN: _WORKING | {AWAITING_CUSTOMER},
    INVESTIGATING: _WORKING | {AWAITING_CUSTOMER},
    AWAITING_CUSTOMER: _WORKING | {AWAITING_CUSTOMER},
    PROPOSED: _WORKING,  # a proposal waits for the customer through awaiting_confirmation (action layer)
    # Only the review console (by="review") moves an issue on from the review queue.
    QUEUED_FOR_REVIEW: {EXECUTED_VERIFIED, REVIEW_REJECTED},
}
REVIEW_ONLY_FROM = {QUEUED_FOR_REVIEW}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, customer_id TEXT, channel TEXT, auth_level TEXT, identity_status TEXT,
    status TEXT NOT NULL, policy_version TEXT, reference_date TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id), seq INTEGER NOT NULL,
    domain TEXT NOT NULL, kind TEXT, summary TEXT, refs TEXT, status TEXT NOT NULL, proposal TEXT,
    outcome TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(issue_id), source TEXT NOT NULL,
    author TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id), issue_ids TEXT NOT NULL,
    reason TEXT NOT NULL, packet TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, issue_id TEXT, kind TEXT NOT NULL,
    data TEXT, at TEXT
);
"""


LEFT_OPEN = "left_open"  # hand-off reason when the agents ended a turn with an issue neither closed nor waiting
REPLY_FAILED = "reply_failed"  # hand-off reason when the reply could not be shown to the customer


def _waits_for_customer(issue: dict[str, Any]) -> bool:
    return issue["status"] == AWAITING_CUSTOMER or (
        issue["status"] == PROPOSED and (issue.get("outcome") or {}).get("outcome") == "awaiting_confirmation")


class CaseError(ValueError):
    """A request the case rules do not allow; the message is safe to show to the agent."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def turn_start() -> str:
    """For gateways: the current time as this store records it, to pass to end_of_turn after the reply."""
    return _now()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class CaseStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("CASE_STORE_PATH",
                                                PROJECT_ROOT / "runtime" / "customer_service" / "cases.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _event(self, db: sqlite3.Connection, case_id: str, issue_id: str | None, kind: str, data: Any = None) -> None:
        db.execute("INSERT INTO events (case_id, issue_id, kind, data, at) VALUES (?, ?, ?, ?, ?)",
                   (case_id, issue_id, kind, json.dumps(data, default=str), _now()))

    # ------------------------------------------------------------------ cases

    def open_case(self, *, customer_id: str | None, channel: str | None, auth_level: str | None,
                  identity_status: str, policy_version: str | None, reference_date: str | None,
                  existing_case_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """
        Returns (case, created). An existing case id from the gateway is reused only if it belongs to the same
        customer and is still open; otherwise a new case is opened, so a forged id reveals nothing.
        """
        with _LOCK, self._connect() as db:
            if existing_case_id:
                row = db.execute("SELECT * FROM cases WHERE case_id = ?", (existing_case_id,)).fetchone()
                # Both sides None is an unidentified contact (e.g. ambiguous phone): it holds no account data.
                if row and row["customer_id"] == customer_id and row["status"] == "open":
                    return dict(row), False
            case_id = _new_id("CASE")
            now = _now()
            db.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                       (case_id, customer_id, channel, auth_level, identity_status, policy_version, reference_date,
                        now, now))
            self._event(db, case_id, None, "case_opened", {"identity_status": identity_status, "channel": channel})
            return dict(db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()), True

    def customer_cases(self, customer_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """The customer's own cases, newest first (for gateways that show status, e.g. an assistant)."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM cases WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (customer_id, limit))]

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            return dict(row) if row else None

    def case_view(self, case_id: str) -> dict[str, Any]:
        """The case with its issues, findings and hand-offs, as the orchestrator sees it."""
        case = self.get_case(case_id)
        if case is None:
            raise CaseError(f"case {case_id} not found")
        with self._connect() as db:
            issues = []
            for row in db.execute("SELECT * FROM issues WHERE case_id = ? ORDER BY seq", (case_id,)):
                issue = self._issue_dict(row)
                issue["findings"] = [
                    {"source": f["source"], "author": f["author"], "data": json.loads(f["data"])}
                    for f in db.execute("SELECT * FROM findings WHERE issue_id = ? ORDER BY created_at, rowid",
                                        (issue["issue_id"],))
                ]
                issues.append(issue)
            handoffs = [{"handoff_id": h["handoff_id"], "issue_ids": json.loads(h["issue_ids"]), "reason": h["reason"],
                         "status": h["status"]}
                        for h in db.execute("SELECT * FROM handoffs WHERE case_id = ? ORDER BY created_at", (case_id,))]
        return {"case_id": case_id, "status": case["status"], "identity_status": case["identity_status"],
                "issues": issues, "handoffs": handoffs, "summary": self.summarise(issues)}

    @staticmethod
    def summarise(issues: list[dict[str, Any]]) -> dict[str, Any]:
        still_open = [i["issue_id"] for i in issues if i["status"] not in TERMINAL]
        if not issues:
            outcome = "no_issues"
        elif still_open:
            outcome = "open"
        elif all(i["status"] in RESOLVED for i in issues):
            outcome = "resolved"
        else:
            outcome = "closed_with_handoff"
        return {"outcome": outcome, "open_issue_ids": still_open,
                "resolved": [i["issue_id"] for i in issues if i["status"] in RESOLVED],
                "with_people": [i["issue_id"] for i in issues if i["status"] in (QUEUED_FOR_REVIEW, HANDED_OFF)]}

    def complete_case(self, case_id: str) -> dict[str, Any]:
        """Closes the case only when no issue is still open; otherwise raises CaseError naming them."""
        view = self.case_view(case_id)
        summary = view["summary"]
        if summary["outcome"] == "open":
            raise CaseError("case cannot be completed: issues still open: " + ", ".join(summary["open_issue_ids"]))
        if summary["outcome"] == "no_issues":
            raise CaseError("case cannot be completed: it has no issues; record what the customer asked first")
        with _LOCK, self._connect() as db:
            db.execute("UPDATE cases SET status = ?, updated_at = ? WHERE case_id = ?",
                       (summary["outcome"], _now(), case_id))
            self._event(db, case_id, None, "case_completed", summary)
        return {**summary, "case_id": case_id}

    def end_of_turn(self, case_id: str, since: str, reply_delivered: bool = True) -> dict[str, Any]:
        """
        The gateway's check after each reply (G4): a turn may only end with an issue open if it waits for the
        customer's answer (awaiting_customer, or a pending confirmation) and was updated in this turn (`since`,
        the turn's start in this store's timestamp format). Any other open issue was left behind by the agents: it
        is handed to a person (reason left_open), so nothing is silently dropped. When the reply could not be
        delivered (gateway.reply_problem), nothing waits: the customer never saw the question (reason
        reply_failed). The case is then completed if nothing is still waiting.
        """
        case = self.get_case(case_id)
        if case is None or case["status"] != "open":
            return {"left_open": [], "handoff_id": None, "completed": False}
        view = self.case_view(case_id)
        waiting = [i for i in view["issues"]
                   if reply_delivered and _waits_for_customer(i) and i["updated_at"] >= since]
        left = [i for i in view["issues"] if i["status"] not in TERMINAL and i not in waiting]
        handoff_id = None
        reason = LEFT_OPEN if reply_delivered else REPLY_FAILED
        if left:
            packet = {"reason": reason, "customer_id": case["customer_id"], "channel": case["channel"],
                      "identity_status": case["identity_status"], "auth_level": case["auth_level"],
                      "policy_version": case["policy_version"], "reference_date": case["reference_date"],
                      "issues": [{k: i.get(k) for k in ("issue_id", "domain", "kind", "summary", "refs", "status",
                                                         "proposal", "outcome", "findings")} for i in left],
                      "agent_summary": "The agents ended the turn without closing or handing off these issues."
                      if reply_delivered else "The agents' reply could not be shown to the customer (not readable "
                                              "text); the customer was told a colleague will pick this up."}
            ids = [i["issue_id"] for i in left]
            handoff_id = self.add_handoff(case_id, ids, reason, packet)
            for issue_id in ids:
                self.set_status(case_id, issue_id, HANDED_OFF, by="system",
                                outcome={"handoff_id": handoff_id, "reason": reason})
        completed = bool(view["issues"]) and not waiting
        if completed:
            self.complete_case(case_id)
        return {"left_open": [i["issue_id"] for i in left], "handoff_id": handoff_id, "completed": completed}

    def reopen_case(self, case_id: str, reason: str) -> dict[str, Any]:
        """Reopens a completed case (e.g. the customer asks for a person after it was resolved). System only."""
        with _LOCK, self._connect() as db:
            row = db.execute("SELECT status FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if row is None:
                raise CaseError(f"case {case_id} not found")
            if row["status"] != "open":
                db.execute("UPDATE cases SET status = 'open', updated_at = ? WHERE case_id = ?", (_now(), case_id))
                self._event(db, case_id, None, "case_reopened", {"from": row["status"], "reason": reason})
        return self.get_case(case_id)

    # ------------------------------------------------------------------ issues

    @staticmethod
    def _issue_dict(row: sqlite3.Row) -> dict[str, Any]:
        issue = dict(row)
        issue["refs"] = json.loads(issue["refs"] or "{}")
        issue["proposal"] = json.loads(issue["proposal"]) if issue["proposal"] else None
        issue["outcome"] = json.loads(issue["outcome"]) if issue["outcome"] else None
        return issue

    def add_issue(self, case_id: str, *, domain: str, kind: str, summary: str, refs: dict[str, str]) -> dict:
        if domain not in DOMAINS:
            raise CaseError(f"unknown domain {domain!r}; use one of {', '.join(DOMAINS)}")
        with _LOCK, self._connect() as db:
            if not db.execute("SELECT 1 FROM cases WHERE case_id = ? AND status = 'open'", (case_id,)).fetchone():
                raise CaseError(f"case {case_id} is not open")
            for row in db.execute("SELECT * FROM issues WHERE case_id = ? AND domain = ? AND kind = ?",
                                  (case_id, domain, kind)):
                existing = self._issue_dict(row)
                if existing["refs"] == refs and existing["status"] not in TERMINAL:
                    return existing  # the same need restated in a later message
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM issues WHERE case_id = ?", (case_id,)).fetchone()[0]
            issue_id = f"{case_id}-I{seq}"
            now = _now()
            db.execute("INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                       (issue_id, case_id, seq, domain, kind, summary[:500], json.dumps(refs), OPEN, now, now))
            self._event(db, case_id, issue_id, "issue_added", {"domain": domain, "kind": kind, "refs": refs})
            return self._issue_dict(db.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone())

    def get_issue(self, case_id: str, issue_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM issues WHERE issue_id = ? AND case_id = ?", (issue_id, case_id)).fetchone()
        if row is None:
            raise CaseError(f"issue {issue_id} is not part of this case")
        return self._issue_dict(row)

    def set_status(self, case_id: str, issue_id: str, status: str, *, by: str, outcome: dict | None = None,
                   proposal: dict | None = None) -> dict[str, Any]:
        """
        by='llm' is limited to LLM_SETTABLE; 'system' (action layer, hand-off) may set any allowed transition;
        'review' (a person in the review console) is the only caller that may move an issue out of review.
        """
        issue = self.get_issue(case_id, issue_id)
        if by == "llm" and status not in LLM_SETTABLE:
            raise CaseError(f"status {status} can only be set by the action layer or a hand-off")
        if status not in TRANSITIONS.get(issue["status"], set()):
            raise CaseError(f"issue {issue_id} is {issue['status']}; it cannot become {status}")
        if issue["status"] in REVIEW_ONLY_FROM and by != "review":
            raise CaseError(f"issue {issue_id} is waiting for a person's review; only the review console can close it")
        with _LOCK, self._connect() as db:
            if status == RESOLVED_INFO_ONLY and by == "llm":
                n = db.execute("SELECT COUNT(*) FROM findings WHERE issue_id = ? AND author = 'tool'",
                               (issue_id,)).fetchone()[0]
                if n == 0:
                    raise CaseError(f"issue {issue_id} has no evidence from a data tool yet; investigate it first")
            db.execute("UPDATE issues SET status = ?, outcome = COALESCE(?, outcome), proposal = COALESCE(?, proposal),"
                       " updated_at = ? WHERE issue_id = ?",
                       (status, json.dumps(outcome, default=str) if outcome else None,
                        json.dumps(proposal) if proposal else None, _now(), issue_id))
            self._event(db, case_id, issue_id, "status", {"from": issue["status"], "to": status, "by": by,
                                                          "outcome": outcome, "proposal": proposal})
        return self.get_issue(case_id, issue_id)

    # ------------------------------------------------------------------ findings and hand-offs

    def add_finding(self, case_id: str, issue_id: str, *, source: str, author: str, data: Any) -> None:
        """author 'tool' = rows returned by a data tool (evidence); 'agent' = an agent's own assessment."""
        self.get_issue(case_id, issue_id)
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
                       (_new_id("F"), issue_id, source, author, json.dumps(data, default=str), _now()))

    def add_handoff(self, case_id: str, issue_ids: list[str], reason: str, packet: dict[str, Any]) -> str:
        handoff_id = _new_id("HO")
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO handoffs VALUES (?, ?, ?, ?, ?, 'open', ?)",
                       (handoff_id, case_id, json.dumps(issue_ids), reason, json.dumps(packet, default=str), _now()))
            self._event(db, case_id, None, "handoff", {"handoff_id": handoff_id, "issue_ids": issue_ids,
                                                        "reason": reason})
        return handoff_id

    def handoffs(self, status: str | None = "open") -> list[dict[str, Any]]:
        """Hand-offs across all cases (newest last), optionally only those with the given status."""
        with self._connect() as db:
            rows = db.execute("SELECT h.*, c.customer_id FROM handoffs h JOIN cases c ON c.case_id = h.case_id "
                              "WHERE ? IS NULL OR h.status = ? ORDER BY h.created_at, h.rowid", (status, status))
            return [{**dict(r), "issue_ids": json.loads(r["issue_ids"]), "packet": json.loads(r["packet"])}
                    for r in rows]

    def resolve_handoff(self, handoff_id: str, *, reviewer: str, note: str) -> dict[str, Any]:
        """A person closes a hand-off. The issues stay handed_off; the event records who did what."""
        with _LOCK, self._connect() as db:
            row = db.execute("SELECT case_id, status FROM handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
            if row is None:
                raise CaseError(f"hand-off {handoff_id} not found")
            if row["status"] != "open":
                raise CaseError(f"hand-off {handoff_id} is already {row['status']}")
            db.execute("UPDATE handoffs SET status = 'resolved' WHERE handoff_id = ?", (handoff_id,))
            self._event(db, row["case_id"], None, "handoff_resolved",
                        {"handoff_id": handoff_id, "reviewer": reviewer, "note": note})
        return {"handoff_id": handoff_id, "case_id": row["case_id"], "status": "resolved"}

    def handoff_packet(self, handoff_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT packet FROM handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
        if row is None:
            raise CaseError(f"hand-off {handoff_id} not found")
        return json.loads(row["packet"])

    def log_event(self, case_id: str, issue_id: str | None, kind: str, data: Any = None) -> None:
        """An event from outside the conversation (the Resolution Guardian) on the case's history."""
        with _LOCK, self._connect() as db:
            self._event(db, case_id, issue_id, kind, data)

    def events(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None}
                    for r in db.execute("SELECT * FROM events WHERE case_id = ? ORDER BY event_id", (case_id,))]
