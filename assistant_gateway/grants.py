"""
What a customer has allowed an assistant to do, and everything an assistant asked. SQLite, local only.

    grants    one per (customer, assistant) consent: scopes, sign-in level, expiry, revocation. The access token is
              random and only its SHA-256 is stored, so the database alone cannot be used to call the gateway.
    codes     one-time OAuth authorization codes, issued on the consent page (server.py)
    requests  ask_support requests: the turn runs in the background; get_answer reads the result
    audit     every tool call with the assistant's client id, the outcome and the time

Environment: ASSISTANT_STORE_PATH (default <project>/runtime/assistant/assistant.db).
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from coded_tools.customer_service.policy import PROJECT_ROOT

TOKEN_PREFIX = "acme_"  # ACME Retail assistant token; recognisable in logs, never logged in full
_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, client_id TEXT NOT NULL, client_name TEXT,
    customer_id TEXT NOT NULL, auth_level TEXT NOT NULL, scopes TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, case_id TEXT, message TEXT NOT NULL,
    status TEXT NOT NULL, answer TEXT, result TEXT, created_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, grant_id TEXT, client_id TEXT, tool TEXT NOT NULL,
    args TEXT, outcome TEXT NOT NULL
);
"""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class GrantStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("ASSISTANT_STORE_PATH",
                                                PROJECT_ROOT / "runtime" / "assistant" / "assistant.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _grant(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        grant = dict(row)
        grant.pop("token_hash")
        grant["scopes"] = json.loads(grant["scopes"])
        return grant

    # ------------------------------------------------------------------ grants (access tokens)

    def issue(self, *, customer_id: str, client_id: str, client_name: str, auth_level: str, scopes: list[str],
              ttl_s: float) -> tuple[str, dict[str, Any]]:
        """Creates a grant; returns (access token, grant). The token is shown once and never stored."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        grant_id = "GRANT-" + secrets.token_hex(5).upper()
        now = time.time()
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                       (grant_id, _hash(token), client_id, client_name[:100], customer_id, auth_level,
                        json.dumps(sorted(set(scopes))), now, now + ttl_s))
            return token, self._grant(db.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone())

    def verify(self, token: str) -> dict[str, Any] | None:
        """The grant behind a token, or None if unknown, expired or revoked."""
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        with self._connect() as db:
            grant = self._grant(db.execute("SELECT * FROM grants WHERE token_hash = ?", (_hash(token),)).fetchone())
        if grant is None or grant["revoked_at"] or grant["expires_at"] <= time.time():
            return None
        return grant

    def get(self, grant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._grant(db.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone())

    def customer_grants(self, customer_id: str) -> list[dict[str, Any]]:
        """The customer's active grants, newest first (for the consent page)."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM grants WHERE customer_id = ? AND revoked_at IS NULL AND expires_at > ? "
                              "ORDER BY created_at DESC", (customer_id, time.time())).fetchall()
        return [self._grant(r) for r in rows]

    def revoke(self, grant_id: str, customer_id: str) -> bool:
        """Revokes one of the customer's own grants; False if it is not theirs or already revoked."""
        with _LOCK, self._connect() as db:
            cur = db.execute("UPDATE grants SET revoked_at = ? WHERE grant_id = ? AND customer_id = ? AND "
                             "revoked_at IS NULL", (time.time(), grant_id, customer_id))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ registered assistants (OAuth clients)

    def save_client(self, client_id: str, data: dict[str, Any]) -> None:
        with _LOCK, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO clients VALUES (?, ?, ?)", (client_id, json.dumps(data), time.time()))

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ------------------------------------------------------------------ one-time codes (consent, authorization)

    def save_code(self, code: str, data: dict[str, Any], ttl_s: float) -> None:
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO codes VALUES (?, ?, ?, NULL)", (_hash(code), json.dumps(data), time.time() + ttl_s))

    def peek_code(self, code: str) -> dict[str, Any] | None:
        """The code's data if it is unused and not expired (does not use it)."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM codes WHERE code_hash = ?", (_hash(code),)).fetchone()
        if row is None or row["used_at"] or row["expires_at"] <= time.time():
            return None
        return json.loads(row["data"])

    def update_code(self, code: str, data: dict[str, Any]) -> None:
        """New data for an unused code (e.g. a sign-in attempt's count of wrong tries); the expiry stays."""
        with _LOCK, self._connect() as db:
            db.execute("UPDATE codes SET data = ? WHERE code_hash = ? AND used_at IS NULL",
                       (json.dumps(data), _hash(code)))

    def use_code(self, code: str) -> dict[str, Any] | None:
        """Uses a code once: a second use, or an expired code, returns None."""
        with _LOCK, self._connect() as db:
            cur = db.execute("UPDATE codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                             (time.time(), _hash(code), time.time()))
            if cur.rowcount != 1:
                return None
            return json.loads(db.execute("SELECT data FROM codes WHERE code_hash = ?", (_hash(code),)).fetchone()[0])

    # ------------------------------------------------------------------ ask_support requests

    def new_request(self, grant_id: str, message: str, case_id: str | None) -> str:
        request_id = "REQ-" + secrets.token_hex(6).upper()
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, 'running', NULL, NULL, ?, NULL)",
                       (request_id, grant_id, case_id, message[:2000], time.time()))
        return request_id

    def finish_request(self, request_id: str, *, status: str, answer: str, result: dict[str, Any],
                       case_id: str | None) -> None:
        with _LOCK, self._connect() as db:
            db.execute("UPDATE requests SET status = ?, answer = ?, result = ?, case_id = COALESCE(?, case_id), "
                       "finished_at = ? WHERE request_id = ?",
                       (status, answer, json.dumps(result, default=str), case_id, time.time(), request_id))

    def get_request(self, request_id: str, grant_id: str) -> dict[str, Any] | None:
        """A request of this grant only; another grant's request id finds nothing."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM requests WHERE request_id = ? AND grant_id = ?",
                             (request_id, grant_id)).fetchone()
        if row is None:
            return None
        request = dict(row)
        request["result"] = json.loads(request["result"]) if request["result"] else None
        return request

    def chat_context(self, grant_id: str, case_id: str) -> dict[str, Any] | None:
        """The conversation so far on this case through this grant (kept internally, never returned)."""
        with self._connect() as db:
            row = db.execute("SELECT result FROM requests WHERE grant_id = ? AND case_id = ? AND status = 'done' "
                             "ORDER BY finished_at DESC LIMIT 1", (grant_id, case_id)).fetchone()
        return (json.loads(row["result"]) or {}).get("_chat_context") if row and row["result"] else None

    def running_requests(self, grant_id: str) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM requests WHERE grant_id = ? AND status = 'running'",
                              (grant_id,)).fetchone()[0]

    def requests_since(self, grant_id: str, since: float) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM requests WHERE grant_id = ? AND created_at >= ?",
                              (grant_id, since)).fetchone()[0]

    def fail_stale_requests(self, older_than_s: float) -> int:
        """Requests still 'running' after a restart or past their time limit are marked failed."""
        with _LOCK, self._connect() as db:
            return db.execute("UPDATE requests SET status = 'failed', answer = ?, finished_at = ? WHERE status = "
                              "'running' AND created_at < ?",
                              ("The request did not finish. Please ask again.", time.time(),
                               time.time() - older_than_s)).rowcount

    def purge_answers(self, older_than_s: float) -> int:
        """Answers are kept only for a while (answers_kept_hours): they hold account data."""
        with _LOCK, self._connect() as db:
            return db.execute("DELETE FROM requests WHERE status != 'running' AND finished_at < ?",
                              (time.time() - older_than_s,)).rowcount

    # ------------------------------------------------------------------ audit

    def audit(self, grant: dict[str, Any] | None, tool: str, args: dict[str, Any], outcome: str) -> None:
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO audit (at, grant_id, client_id, tool, args, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                       (time.time(), (grant or {}).get("grant_id"), (grant or {}).get("client_id"), tool,
                        json.dumps(args, default=str)[:2000], outcome[:200]))

    def audit_rows(self, grant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if grant_id:
                rows = db.execute("SELECT * FROM audit WHERE grant_id = ? ORDER BY audit_id", (grant_id,))
            else:
                rows = db.execute("SELECT * FROM audit ORDER BY audit_id")
            return [dict(r) for r in rows]

    def calls_since(self, grant_id: str, since: float) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM audit WHERE grant_id = ? AND at >= ?",
                              (grant_id, since)).fetchone()[0]
