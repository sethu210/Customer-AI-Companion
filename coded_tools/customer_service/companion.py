"""
Everly companion core (companion plan P1). Deterministic code, no LLM.

One companion per customer, for life. It holds what the customer owns and controls, and derives everything else
from the systems of record, so nothing the model guesses is ever remembered:

    CompanionStore    the companion record (id, name, created, reset) and the customer's consents, in the case
                      store's SQLite file. Consents are kept as a history (what, when, through which channel); the
                      latest entry counts, and a consent never given counts as its default (settings: companion in
                      config/service_policy.hocon). service_updates is the Guardian's opt-out: the companion records
                      it and the Guardian enforces it.
    build_profile()   the derived profile, from the data only: name, tier and points, usual sizes, favourite colours,
                      categories bought and kept, returns and their reasons, price range. Each section names its
                      source. Style tags come with the stylist data (P4).
    open_items()      what is still being worked on for the customer: the Guardian's watches and open cases.
    recall()          one line per closed case ("Oct 2026: refund question for the olive chinos, answered"), built
                      from the case store's issue kinds and outcomes, never from model-written text. Cases before a
                      reset are not recalled.
    digest()          a compact block (at most digest_max_chars) that companion_middleware adds to the front
                      agent's instructions each turn (P2). It holds no customer id, no risk signals and no
                      contact-history notes.
    load_view()       all of the above for one customer, over the adapter's reads (the data part is cached for
                      profile_cache_s; the local stores are read fresh every time).

The continuous thread (P2): one per customer across conversations and channels. The gateway records each turn
(gateway.finish_turn: the customer's message and the reply shown); updates sent to the customer (the outbox:
Guardian and review decisions) are merged in by time. The thread is for the customer to read; it never goes into
the model's prompt (only the digest does), so it can grow without slowing the agents. greeting() is the gateway's
fixed opening line, naming updates that arrived since the customer's last message.
"""

import json
import re
import sqlite3
import statistics
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from coded_tools.customer_service import case_store as cs
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.guardian import GuardianStore
from coded_tools.customer_service.guardian import customer_watching
from coded_tools.customer_service.guardian import issue_subject
from coded_tools.customer_service.recommendations import LETTER_SIZES
from coded_tools.customer_service.recommendations import NOT_KEPT
from coded_tools.customer_service.recommendations import usual_sizes
from coded_tools.customer_service.recommendations import words

_LOCK = threading.Lock()

SERVICE_UPDATES = "service_updates"
STYLIST_MODE = "stylist_mode"
MARKETING = "marketing"
CONSENTS = (SERVICE_UPDATES, STYLIST_MODE, MARKETING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS companions (
    customer_id TEXT PRIMARY KEY, companion_id TEXT NOT NULL UNIQUE, name TEXT, created_at TEXT NOT NULL,
    updated_at TEXT, reset_at TEXT
);
CREATE TABLE IF NOT EXISTS companion_consents (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, consent TEXT NOT NULL,
    granted INTEGER NOT NULL, channel TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_thread (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
    case_id TEXT, channel TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_memory (
    customer_id TEXT NOT NULL, memory_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, status TEXT NOT NULL,
    case_id TEXT, issue_id TEXT, turn_id TEXT, channel TEXT, created_at TEXT NOT NULL, confirmed_at TEXT,
    expires_at TEXT, PRIMARY KEY (customer_id, memory_id)
);
"""
# Memory (P3): only what the customer states and then confirms in a later message (checked in code, like G1).
PENDING = "pending"
ACTIVE = "active"
MEMORY_KINDS = ("likes", "avoids", "size", "occasion")
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.'&/\-]*$")
SIZE_AREAS = {"clothing": "letter", "tops": "letter", "footwear": "shoe", "shoes": "shoe", "shoe": "shoe",
              "trousers": "waist", "waist": "waist", "jeans": "waist"}
AREA_WORDS = {"letter": "clothing", "shoe": "footwear", "waist": "trousers (waist)"}
# Colour families (stylist data, P4): the customer's words for each, and how the companion says it.
FAMILY_WORDS = {"dark_neutral": ("neutral", "dark"), "light_neutral": ("neutral", "light"), "earth": ("earth",),
                "blue": ("blue",), "green": ("green",), "warm": ("warm", "bright")}
FAMILY_SAID = {"dark_neutral": "dark neutrals", "light_neutral": "light neutrals", "earth": "earth tones",
               "blue": "blues", "green": "greens", "warm": "warm colours"}
CUSTOMER = "customer"
COMPANION = "companion"
UPDATE = "update"  # a message sent to the customer outside a conversation (outbox: Guardian, review decisions)
MAX_THREAD_TEXT = 4000

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 '\-]*$")

SIZE_WORDS = {"letter": "clothing", "shoe": "footwear", "waist": "trousers (waist)"}
KIND_WORDS = {
    "where_is_order": "delivery question", "delivery_problem": "delivery problem", "missing_item": "missing item",
    "cancel_request": "cancellation", "return_request": "return", "return_status": "return question",
    "refund_status": "refund question", "duplicate_charge": "possible double charge",
    "billing_question": "billing question", "points_balance": "points question", "missing_points": "missing points",
    "tier_question": "tier question", "account_change": "account change", "sign_in_problem": "sign-in problem",
    "product_question": "product suggestions", "human_requested": "request for a colleague", "question": "question",
}
ACTION_DONE = {"initiate_refund": "refund started", "retrigger_refund": "refund restarted",
               "reissue_refund_store_credit": "store credit issued", "open_carrier_trace": "carrier trace opened",
               "cancel_order": "order cancelled", "create_return": "return created",
               "open_billing_review": "sent to the billing team"}
STATUS_WORDS = {cs.RESOLVED_INFO_ONLY: "answered", cs.QUEUED_FOR_REVIEW: "sent to a colleague for review",
                cs.HANDED_OFF: "passed to a colleague", cs.REVIEW_REJECTED: "declined after review"}
SOURCES = {"customer": "your account and loyalty records", "sizes": "items you kept",
           "styles": "the styles of items you kept", "colour_families": "the colours of items you kept",
           "colours": "items you kept", "categories": "items you kept", "price": "items you kept",
           "returns": "your returns", "open_items": "your open requests", "recall": "your earlier requests"}


class CompanionError(ValueError):
    """A request the companion rules do not allow; the message is safe to show."""


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def check_preference(kind: str, value: str, settings: dict[str, Any]) -> str:
    """The preference as stored ('footwear 10', 'navy and earth tones'), or raises CompanionError."""
    if kind not in MEMORY_KINDS:
        raise CompanionError(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    value = " ".join(str(value or "").split())
    limit = settings["memory"]["max_value_chars"]
    if not value or len(value) > limit or not _VALUE.match(value):
        raise CompanionError(f"value must be a short phrase in the customer's words (up to {limit} letters, digits, "
                             "spaces and simple punctuation)")
    if kind != "size":
        return value.lower()
    words = value.lower().split()
    system = SIZE_AREAS.get(words[0]) if len(words) == 2 else None
    size = words[1].upper() if len(words) == 2 else ""
    valid = {"letter": size in LETTER_SIZES, "shoe": size.isdigit() and int(size) < 20,
             "waist": size.isdigit() and int(size) >= 20}
    if system is None or not valid[system]:
        raise CompanionError("a size is '<clothing|footwear|trousers> <size>', e.g. 'footwear 10' or 'clothing M'")
    return f"{AREA_WORDS[system].split(' ')[0]} {size}"


def preference_phrase(kind: str, value: str) -> str:
    """'you like navy': how the companion says the preference back (in its question and confirmation)."""
    if kind == "size":
        area, size = value.split(" ")
        return f"your {area} size is {size}"
    return {"likes": f"you like {value}", "avoids": f"you'd rather avoid {value}",
            "occasion": f"you're shopping for {value}"}[kind]


def preference_signals(memories: list[dict[str, Any]], families: dict[str, str] | None = None) -> dict[str, Any]:
    """
    What the recommendation engine uses: stated sizes per size system, and words liked or avoided. With the colour
    families (P4), a word naming a family ("earth tones", "neutrals", "blues") also stands for its colours.
    """
    out: dict[str, Any] = {"sizes": {}, "likes": [], "avoids": []}
    for m in memories:
        if m["kind"] == "size":
            area, size = m["value"].split(" ")
            out["sizes"][SIZE_AREAS[area]] = size
        elif m["kind"] in ("likes", "avoids"):
            said = words(m["value"])
            named = {f for f in set((families or {}).values()) for w in said if w in FAMILY_WORDS.get(f, ())}
            colours = sorted(c for c, f in (families or {}).items() if f in named)
            out[m["kind"]] += [w for w in said + colours if w not in out[m["kind"]]]
    return out


# ---------------------------------------------------------------------------- the store


class CompanionStore:
    """The companion record and consents, in the case store's SQLite file (CASE_STORE_PATH)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else CaseStore().path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def companion(self, customer_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """The customer's companion, created on first use; with its name and current consents."""
        if not customer_id:
            raise CompanionError("a companion belongs to an identified customer")
        with _LOCK, self._connect() as db:
            row = db.execute("SELECT * FROM companions WHERE customer_id = ?", (customer_id,)).fetchone()
            if row is None:
                now = _now()
                db.execute("INSERT INTO companions VALUES (?, ?, NULL, ?, ?, NULL)",
                           (customer_id, f"CMP-{uuid.uuid4().hex[:10].upper()}", now, now))
                row = db.execute("SELECT * FROM companions WHERE customer_id = ?", (customer_id,)).fetchone()
        record = dict(row)
        record["named_by_customer"] = bool(record["name"])
        record["name"] = record["name"] or settings["name"]
        record["consents"] = self.consents(customer_id, settings)
        return record

    def consents(self, customer_id: str, settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """consent -> {granted, at, channel, set_by}: the latest entry, else the default (set_by 'default')."""
        out = {c: {"granted": bool(settings["default_consents"][c]), "at": None, "channel": None,
                   "set_by": "default"} for c in CONSENTS}
        with self._connect() as db:
            for r in db.execute("SELECT * FROM companion_consents WHERE customer_id = ? ORDER BY entry_id",
                                (customer_id,)):
                out[r["consent"]] = {"granted": bool(r["granted"]), "at": r["at"], "channel": r["channel"],
                                     "set_by": "customer"}
        return out

    def customers_with(self, consent: str, settings: dict[str, Any]) -> list[str]:
        """Customers whose latest choice grants this consent (a default-on consent also needs a companion)."""
        with self._connect() as db:
            latest = {r["customer_id"]: bool(r["granted"]) for r in db.execute(
                "SELECT customer_id, granted FROM companion_consents WHERE consent = ? ORDER BY entry_id", (consent,))}
            if settings["default_consents"][consent]:
                for r in db.execute("SELECT customer_id FROM companions"):
                    latest.setdefault(r["customer_id"], True)
        return sorted(c for c, granted in latest.items() if granted)

    def consent_history(self, customer_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [{**dict(r), "granted": bool(r["granted"])} for r in db.execute(
                "SELECT consent, granted, channel, at FROM companion_consents WHERE customer_id = ? "
                "ORDER BY entry_id", (customer_id,))]

    def set_consent(self, customer_id: str, consent: str, granted: bool, *, channel: str) -> None:
        """Records the customer's choice. service_updates also sets the Guardian's opt-out, which it enforces."""
        if consent not in CONSENTS:
            raise CompanionError(f"unknown consent {consent!r}; use one of {', '.join(CONSENTS)}")
        if not customer_id:
            raise CompanionError("a companion belongs to an identified customer")
        now = _now()
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO companion_consents (customer_id, consent, granted, channel, at) "
                       "VALUES (?, ?, ?, ?, ?)", (customer_id, consent, int(granted), channel, now))
        if consent == SERVICE_UPDATES:
            GuardianStore(self.path).set_opt_out(customer_id, not granted, now)

    def rename(self, customer_id: str, name: str | None, settings: dict[str, Any]) -> dict[str, Any]:
        """The customer names their companion; an empty name goes back to the brand name."""
        name = " ".join((name or "").split())
        if name and (len(name) > settings["max_name_length"] or not _NAME.match(name)):
            raise CompanionError(f"a name is up to {settings['max_name_length']} letters, digits, spaces, "
                                 "hyphens or apostrophes, starting with a letter")
        self.companion(customer_id, settings)
        with _LOCK, self._connect() as db:
            db.execute("UPDATE companions SET name = ?, updated_at = ? WHERE customer_id = ?",
                       (name or None, _now(), customer_id))
        return self.companion(customer_id, settings)

    def reset(self, customer_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """
        "Reset my companion": the name goes back to the brand name, every remembered preference is deleted and
        earlier conversations are no longer recalled or shown. Consents are the customer's own choices and stay.
        """
        self.companion(customer_id, settings)
        now = _now()
        with _LOCK, self._connect() as db:
            db.execute("UPDATE companions SET name = NULL, reset_at = ?, updated_at = ? WHERE customer_id = ?",
                       (now, now, customer_id))
            db.execute("DELETE FROM companion_memory WHERE customer_id = ?", (customer_id,))
        return self.companion(customer_id, settings)

    # ------------------------------------------------------------------ memory (stated, confirmed preferences)

    @staticmethod
    def _memory(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in ("memory_id", "kind", "value", "status", "created_at", "confirmed_at",
                                    "expires_at", "channel", "case_id", "issue_id", "turn_id")}

    def memories(self, customer_id: str, *, status: str = ACTIVE) -> list[dict[str, Any]]:
        """The customer's preferences with this status that have not expired, oldest first."""
        now = _now()
        with self._connect() as db:
            return [self._memory(r) for r in db.execute(
                "SELECT * FROM companion_memory WHERE customer_id = ? AND status = ? AND "
                "(expires_at IS NULL OR expires_at > ?) ORDER BY created_at, memory_id", (customer_id, status, now))]

    def propose_memory(self, customer_id: str, kind: str, value: str, *, case_id: str, issue_id: str, turn_id: str,
                       channel: str | None, pending_hours: float) -> dict[str, Any]:
        """Records what the customer asked to be remembered, waiting for their yes. Replaces an older question."""
        now = datetime.now(timezone.utc)
        with _LOCK, self._connect() as db:
            db.execute("DELETE FROM companion_memory WHERE customer_id = ? AND status = ? AND kind = ? AND value = ?",
                       (customer_id, PENDING, kind, value))
            memory_id = self._next_id(db, customer_id)
            db.execute("INSERT INTO companion_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                       (customer_id, memory_id, kind, value, PENDING, case_id, issue_id, turn_id, channel,
                        _ts(now), _ts(now + timedelta(hours=pending_hours))))
        return next(m for m in self.memories(customer_id, status=PENDING) if m["memory_id"] == memory_id)

    @staticmethod
    def _next_id(db: sqlite3.Connection, customer_id: str) -> str:
        rows = db.execute("SELECT memory_id FROM companion_memory WHERE customer_id = ?", (customer_id,)).fetchall()
        return f"P{max((int(r[0][1:]) for r in rows), default=0) + 1}"

    def confirm_memory(self, customer_id: str, memory_id: str, *, keep_days: float) -> dict[str, Any]:
        """The customer said yes: the preference becomes active. A newer stated size replaces the older one."""
        now = datetime.now(timezone.utc)
        with _LOCK, self._connect() as db:
            row = db.execute("SELECT * FROM companion_memory WHERE customer_id = ? AND memory_id = ?",
                             (customer_id, memory_id)).fetchone()
            if row is None or row["status"] != PENDING:
                raise CompanionError(f"there is no question waiting about preference {memory_id}")
            if row["kind"] == "size":
                area = row["value"].split(" ")[0]
                db.execute("DELETE FROM companion_memory WHERE customer_id = ? AND status = ? AND kind = 'size' "
                           "AND value LIKE ?", (customer_id, ACTIVE, f"{area} %"))
            db.execute("DELETE FROM companion_memory WHERE customer_id = ? AND status = ? AND kind = ? AND value = ?",
                       (customer_id, ACTIVE, row["kind"], row["value"]))
            db.execute("UPDATE companion_memory SET status = ?, confirmed_at = ?, expires_at = ? WHERE customer_id = ? "
                       "AND memory_id = ?", (ACTIVE, _ts(now), _ts(now + timedelta(days=keep_days)), customer_id,
                                             memory_id))
        return next(m for m in self.memories(customer_id) if m["memory_id"] == memory_id)

    def forget(self, customer_id: str, memory_id: str | None) -> list[str]:
        """Deletes one preference (or all of them with None); returns the ids deleted. Nothing of it is kept."""
        with _LOCK, self._connect() as db:
            if memory_id is None:
                ids = [r[0] for r in db.execute("SELECT memory_id FROM companion_memory WHERE customer_id = ?",
                                                (customer_id,))]
                db.execute("DELETE FROM companion_memory WHERE customer_id = ?", (customer_id,))
                return ids
            n = db.execute("DELETE FROM companion_memory WHERE customer_id = ? AND memory_id = ?",
                           (customer_id, memory_id)).rowcount
        return [memory_id] if n else []

    def export(self, customer_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """What the store holds about the customer ("Download my companion data")."""
        companion = self.companion(customer_id, settings)
        return {"companion": companion, "consent_history": self.consent_history(customer_id),
                "preferences": self.memories(customer_id),
                "thread": self.thread(customer_id, since=None, limit=None)}

    # ------------------------------------------------------------------ the thread

    def record_turn(self, customer_id: str, *, case_id: str | None, channel: str | None, message: str,
                    reply: str, asked_at: str | None = None) -> None:
        """
        One conversation turn as the customer saw it: their message (at `asked_at`, the turn's start, so an update
        sent while the agents worked still counts as new) and the reply that was shown (now).
        """
        if not customer_id:
            return
        now = _now()
        with _LOCK, self._connect() as db:
            for role, text, at in ((CUSTOMER, message, asked_at or now), (COMPANION, reply, now)):
                if text and text.strip():
                    db.execute("INSERT INTO companion_thread (customer_id, role, text, case_id, channel, at) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (customer_id, role, text.strip()[:MAX_THREAD_TEXT], case_id, channel, at))

    def thread(self, customer_id: str, *, since: str | None, limit: int | None = 50) -> list[dict[str, Any]]:
        """
        The customer's thread, oldest first (the last `limit` entries): their conversations, merged by time with
        the updates sent to them (outbox). Nothing from before `since` (the last reset).
        """
        from coded_tools.customer_service.action_store import ActionStore
        from coded_tools.customer_service.guardian import OUTBOX

        with self._connect() as db:
            rows = [{"role": r["role"], "text": r["text"], "case_id": r["case_id"], "channel": r["channel"],
                     "at": r["at"], "source": "conversation", "seq": r["entry_id"]}
                    for r in db.execute("SELECT * FROM companion_thread WHERE customer_id = ? ORDER BY entry_id",
                                        (customer_id,))]
        for n, r in enumerate(ActionStore().read(OUTBOX)):
            # Private messages (a sign-in code) are delivered but never kept in the thread or the companion's notes.
            if r.get("customer_id") == customer_id and r.get("message") and not r.get("private"):
                rows.append({"role": UPDATE, "text": r["message"], "case_id": r.get("support_case_id"),
                             "channel": None, "at": r.get("at") or "", "source": r.get("source") or "review",
                             "clock": r.get("sent_at_clock"), "seq": n})
        rows = [r for r in rows if not since or r["at"] > since]
        rows.sort(key=lambda r: (r["at"], r["role"] == UPDATE, r["seq"]))  # same second: the conversation first
        for r in rows:
            r.pop("seq")
        return rows if limit is None else rows[-limit:]

    def new_updates(self, customer_id: str, *, since: str | None) -> list[dict[str, Any]]:
        """Updates sent after the customer's last message (or the reset), i.e. not seen in a conversation yet."""
        thread = self.thread(customer_id, since=since, limit=None)
        last = max((r["at"] for r in thread if r["role"] == CUSTOMER), default="")
        return [r for r in thread if r["role"] == UPDATE and r["at"] >= last]


# ---------------------------------------------------------------------------- the derived profile


def unique_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per item (the reads repeat an item per return or refund); the order is kept (newest first)."""
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        seen.setdefault(r["item_id"], r)
    return list(seen.values())


def is_kept(row: dict[str, Any]) -> bool:
    return row.get("item_status") not in NOT_KEPT and row.get("order_status") != "cancelled"


def item_name(row: dict[str, Any]) -> str:
    """'olive chinos': the colour and title as the customer would say them."""
    return " ".join(str(row[k]) for k in ("color", "title") if row.get(k)).lower() or "item"


def build_profile(profile_rows: list[dict] | None, history_rows: list[dict] | None,
                  context_rows: list[dict] | None, settings: dict[str, Any],
                  style_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    The data part of the profile. None for a read that failed: its sections are marked unavailable rather than
    guessed. Every value comes from the rows. style_data (load_style_data; None = not available) adds the style
    tags and colour families of the items they kept.
    """
    out: dict[str, Any] = {"unavailable": []}
    if history_rows is not None and style_data is not None:
        kept_products = [style_data["product_of"].get(i["sku"]) for i in unique_items(history_rows) if is_kept(i)]
        tags = Counter(t for pid in kept_products for t in style_data["styles"].get(pid, []))
        out["styles"] = [t for t, n in tags.most_common() if n >= 2][:settings["top_styles"]]
        families = Counter(style_data["families"].get(str(i["color"]).lower())
                           for i in unique_items(history_rows) if is_kept(i) and i.get("color"))
        families.pop(None, None)
        out["colour_families"] = [f for f, n in families.most_common() if n >= 2][:settings["top_colour_families"]]
    elif history_rows is not None:
        out["unavailable"].append("styles")
    if profile_rows is None:
        out["unavailable"].append("customer")
    elif profile_rows:
        p = profile_rows[0]
        since = p.get("member_since")
        out["customer"] = {"first_name": (p.get("full_name") or "").split(" ")[0] or None, "tier": p.get("tier"),
                           "points": p.get("loyalty_points"), "member_since": str(since)[:4] if since else None}
        out["as_of"] = str(p["as_of"]) if p.get("as_of") else None
    if history_rows is None:
        out["unavailable"] += ["sizes", "colours", "categories", "returns"]
    else:
        items = unique_items(history_rows)
        kept = [i for i in items if is_kept(i)]
        out["orders"] = len({i["order_id"] for i in items if i.get("order_status") != "cancelled"})
        out["sizes"] = {SIZE_WORDS[system]: size for system, size in usual_sizes(kept).items()}
        colours = Counter(str(i["color"]).lower() for i in kept if i.get("color"))
        out["colours"] = [c for c, n in colours.most_common() if n >= 2][:settings["favourite_colours"]]
        out["categories"] = [{"category": c, "kept": n} for c, n in
                             Counter(i["category"] for i in kept if i.get("category")).most_common(
                                 settings["top_categories"])]
        reasons = Counter(i["return_reason"] for i in items if i.get("return_reason"))
        out["returns"] = {"count": sum(reasons.values()), "reasons": dict(reasons.most_common())}
        out["item_names"] = {i["item_id"]: item_name(i) for i in items}
    if context_rows is None:
        out["unavailable"].append("price")
    else:
        prices = [float(r["price"]) for r in unique_items(context_rows) if is_kept(r) and r.get("price") is not None]
        currency = next((r.get("currency") for r in context_rows if r.get("currency")), "USD")
        out["price"] = {"min": min(prices), "typical": round(statistics.median(prices), 2), "max": max(prices),
                        "currency": currency} if prices else None
    out["sources"] = {k: v for k, v in SOURCES.items() if k in out or k in ("open_items", "recall")}
    return out


# ---------------------------------------------------------------------------- open items and recall (local stores)


def _subject_words(issue: dict[str, Any], names: dict[str, str]) -> str:
    item, order = issue_subject(issue)
    if item:
        return f" for the {names.get(item, 'item')}"
    return f" for order {order}" if order else ""


def issue_words(issue: dict[str, Any], names: dict[str, str]) -> str:
    return KIND_WORDS.get(issue.get("kind") or "", "request") + _subject_words(issue, names)


def outcome_words(issue: dict[str, Any]) -> str:
    if issue["status"] == cs.EXECUTED_VERIFIED:
        return ACTION_DONE.get((issue.get("proposal") or {}).get("action"), "done")
    return STATUS_WORDS.get(issue["status"], "still open")


def open_items(customer_id: str, names: dict[str, str], *, cases: CaseStore,
               exclude_case_id: str | None = None) -> list[dict[str, Any]]:
    """The Guardian's active watches, then issues still open in other open cases. Newest first."""
    out: list[dict[str, Any]] = []
    views: dict[str, dict[str, Any]] = {}

    def view(case_id: str) -> dict[str, Any]:
        if case_id not in views:
            views[case_id] = cases.case_view(case_id)
        return views[case_id]

    watched = set()
    for w in reversed(customer_watching(customer_id, cases=cases)):
        issue = next((i for i in view(w["case_id"])["issues"] if i["issue_id"] == w["issue_id"]), None)
        if issue is None:
            continue
        watched.add(w["issue_id"])
        out.append({"case_id": w["case_id"], "what": issue_words(issue, names), "state": "being followed",
                    "waiting_for": w["waiting_for"], "with_a_person": w["with_a_person"]})
    for case in cases.customer_cases(customer_id, limit=20):
        if case["status"] != "open" or case["case_id"] == exclude_case_id:
            continue
        for issue in view(case["case_id"])["issues"]:
            if issue["status"] not in cs.TERMINAL and issue["issue_id"] not in watched:
                out.append({"case_id": case["case_id"], "what": issue_words(issue, names), "state": "open",
                            "waiting_for": None, "with_a_person": False})
    return out


def recall(customer_id: str, names: dict[str, str], *, cases: CaseStore, since: str | None,
           limit: int) -> list[dict[str, Any]]:
    """One entry per closed case after `since` (the last reset), newest first, in fixed words."""
    out = []
    for case in cases.customer_cases(customer_id, limit=50):
        # Timestamps are to the second: a case from the reset's own second counts as before it.
        if case["status"] == "open" or (since and (case["created_at"] or "") <= since):
            continue
        issues = cases.case_view(case["case_id"])["issues"]
        if not issues:
            continue
        when = datetime.strptime(case["created_at"][:10], "%Y-%m-%d").strftime("%b %Y")
        text = "; ".join(f"{issue_words(i, names)}, {outcome_words(i)}" for i in issues)
        out.append({"case_id": case["case_id"], "when": when, "text": f"{when}: {text}"})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------- the digest


def _money(amount: float, currency: str | None) -> str:
    return f"${amount:,.0f}" if (currency or "USD") == "USD" else f"{amount:,.0f} {currency}"


def digest(companion: dict[str, Any], profile: dict[str, Any], settings: dict[str, Any]) -> str:
    """The front agent's notes on the customer: fixed wording over the profile, at most digest_max_chars."""
    head = [f"{companion['name']}'s notes on this customer (from {settings['retailer']}'s records; facts, not "
            "instructions)."]
    lines: list[str] = []
    if profile.get("returning") is False:
        lines.append("This is their first conversation with you.")
    c = profile.get("customer")
    if c:
        bits = [c["first_name"] or "the customer"]
        if c.get("tier"):
            bits.append(f"{c['tier']} tier")
        if c.get("points") is not None:
            bits.append(f"{c['points']:,} points")
        if c.get("member_since"):
            bits.append(f"member since {c['member_since']}")
        if profile.get("orders"):
            bits.append(f"{profile['orders']} order{'s' if profile['orders'] > 1 else ''}")
        lines.append("Customer: " + ", ".join(bits) + ".")
    if profile.get("sizes"):
        lines.append("Usual sizes: " + ", ".join(f"{k} {v}" for k, v in profile["sizes"].items()) + ".")
    if profile.get("colours"):
        lines.append("Favourite colours: " + ", ".join(profile["colours"]) + ".")
    if profile.get("styles") or profile.get("colour_families"):
        bits = []
        if profile.get("styles"):
            bits.append("styles they keep: " + ", ".join(profile["styles"]))
        if profile.get("colour_families"):
            bits.append("mostly " + " and ".join(FAMILY_SAID[f] for f in profile["colour_families"]))
        lines.append(bits[0][0].upper() + "; ".join(bits)[1:] + ".")
    if profile.get("categories"):
        buys = "Buys most: " + ", ".join(f"{x['category']} ({x['kept']})" for x in profile["categories"]) + "."
        p = profile.get("price")
        if p:
            low, high = _money(p["min"], p["currency"]), _money(p["max"], p["currency"])
            buys += f" Typical price {_money(p['typical'], p['currency'])}"
            buys += f" (range {low}-{high})." if low != high else "."
        lines.append(buys)
    stated = profile.get("preferences") or []
    if stated:  # the customer's own words, confirmed by them; ids so a preference can be forgotten
        said = {"likes": "likes {}", "avoids": "avoids {}", "size": "{} (stated size)", "occasion": "shopping for {}"}
        lines.append("Stated preferences (their words, confirmed): " + "; ".join(
            f"{said[p['kind']].format(p['value'])} [{p['memory_id']}]" for p in stated) + ".")
    r = profile.get("returns")
    if r and r["count"]:
        lines.append(f"Returns: {r['count']} (" + ", ".join(f"{k.replace('_', ' ')} {n}"
                                                            for k, n in r["reasons"].items()) + ").")
    if profile.get("unavailable"):
        lines.append("Not available right now: " + ", ".join(sorted(set(profile["unavailable"]))) + ".")
    for item in profile.get("open_items") or []:
        state = item["state"] + (f", waiting for {item['waiting_for']}" if item.get("waiting_for") else "")
        lines.append(f"Open: {item['what']} (case {item['case_id']}): {state}.")
    for text in profile.get("new_updates") or []:
        lines.append(f"Sent to them since their last message: \"{text}\"")
    consents = companion["consents"]
    lines.append(f"Settings: service updates {'on' if consents[SERVICE_UPDATES]['granted'] else 'off'}, "
                 f"Personal Stylist Mode {'on' if consents[STYLIST_MODE]['granted'] else 'off'}.")
    recalled = [f"Earlier: {x['text']}." for x in profile.get("recall") or []]

    budget = settings["digest_max_chars"]
    text = "\n".join(head + lines + recalled)
    while recalled and len(text) > budget:  # the oldest memories go first
        recalled.pop()
        text = "\n".join(head + lines + recalled)
    if len(text) > budget:
        text = text[:budget - 1].rsplit("\n", 1)[0]
    return text


# ---------------------------------------------------------------------------- over the adapter

_DATA_CACHE: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
_STYLE_CACHE: dict[str, tuple[dict[str, Any], float]] = {}


async def load_style_data(adapter: Any, policy: dict[str, Any]) -> dict[str, Any] | None:
    """
    Stylist data (P4), the same for every customer and cached like the catalog: style tags per product, colour
    family per colour and the SKU -> product map. None when a read failed (nothing is guessed).
    """
    from coded_tools.customer_service.recommendations import CatalogUnavailable
    from coded_tools.customer_service.recommendations import load_catalog

    cached = _STYLE_CACHE.get(adapter.name)
    if cached and time.monotonic() - cached[1] < policy["recommendations"]["catalog_cache_s"]:
        return cached[0]
    styles, families = [await adapter.fetch(fn, policy=policy) for fn in ("get_product_styles", "get_colour_families")]
    try:
        catalog = await load_catalog(adapter, policy)
    except CatalogUnavailable:
        return None
    if styles.failed or families.failed:
        return None
    data: dict[str, Any] = {"styles": {}, "families": {r["color"]: r["family"] for r in families.rows},
                            "product_of": catalog.product_of_sku}
    for r in styles.rows:
        data["styles"].setdefault(r["product_id"], []).append(r["style"])
    _STYLE_CACHE[adapter.name] = (data, time.monotonic())
    return data


async def load_data_profile(adapter: Any, policy: dict[str, Any], customer_id: str) -> dict[str, Any]:
    settings = policy["companion"]
    key = (adapter.name, customer_id)
    cached = _DATA_CACHE.get(key)
    if cached and time.monotonic() - cached[1] < settings["profile_cache_s"]:
        return cached[0]
    reads = {fn: await adapter.fetch(fn, customer_id, policy=policy)
             for fn in ("get_customer_profile", "get_purchase_history", "get_customer_context")}
    rows = {fn: None if r.failed else r.rows for fn, r in reads.items()}
    profile = build_profile(rows["get_customer_profile"], rows["get_purchase_history"],
                            rows["get_customer_context"], settings, await load_style_data(adapter, policy))
    if not profile["unavailable"]:  # a failed read is tried again next time
        _DATA_CACHE[key] = (profile, time.monotonic())
    return profile


async def load_view(customer_id: str, *, adapter: Any = None, policy: dict[str, Any] | None = None,
                    cases: CaseStore | None = None, store: CompanionStore | None = None,
                    exclude_case_id: str | None = None) -> dict[str, Any]:
    """{companion, profile, digest} for an identified customer."""
    if not customer_id:
        raise CompanionError("a companion belongs to an identified customer")
    if policy is None:
        from coded_tools.customer_service.policy import load_policy

        policy = load_policy()
    if adapter is None:
        from coded_tools.customer_service.data_adapter import get_adapter

        adapter = get_adapter()
    settings = policy["companion"]
    cases = cases or CaseStore()
    store = store or CompanionStore(cases.path)
    companion = store.companion(customer_id, settings)
    profile = json.loads(json.dumps(await load_data_profile(adapter, policy, customer_id)))  # a copy, not the cache
    names = profile.pop("item_names", {})
    profile["open_items"] = open_items(customer_id, names, cases=cases, exclude_case_id=exclude_case_id)
    profile["recall"] = recall(customer_id, names, cases=cases, since=companion["reset_at"],
                               limit=settings["recall_cases"])
    profile["preferences"] = [{"memory_id": m["memory_id"], "kind": m["kind"], "value": m["value"],
                               "since": (m["confirmed_at"] or "")[:10]} for m in store.memories(customer_id)]
    profile.setdefault("sources", {})["preferences"] = "what you told us and confirmed"
    since = companion["reset_at"]
    updates = store.new_updates(customer_id, since=since)
    profile["new_updates"] = [u["text"] for u in updates][-settings["new_updates"]:]
    profile["returning"] = bool(profile["recall"] or profile["open_items"]
                                or store.thread(customer_id, since=since, limit=1))
    return {"companion": companion, "profile": profile, "digest": digest(companion, profile, settings),
            "greeting": greeting(companion, profile, settings)}


def greeting(companion: dict[str, Any], profile: dict[str, Any], settings: dict[str, Any]) -> str:
    """The gateway's opening line when a conversation starts: fixed wording over facts, no model involved."""
    first = (profile.get("customer") or {}).get("first_name")
    if not profile.get("returning"):
        return (f"Hi{' ' + first if first else ''}, I'm {companion['name']}, your AI companion at "
                f"{settings['retailer']}. How can I help today?")
    text = f"Welcome back{', ' + first if first else ''}."
    news = profile.get("new_updates") or []
    if news:
        text += " Since we last spoke: " + " ".join(news)
    following = sum(1 for i in profile.get("open_items") or [] if i["state"] == "being followed")
    if following:
        text += f" I'm still following {following} of your requests" if following > 1 else \
            " I'm still following one of your requests"
        # The Guardian sends updates only with the customer's consent; without it, promise nothing.
        text += " and will send you an update when anything changes." \
            if companion["consents"][SERVICE_UPDATES]["granted"] else "."
    return text + " How can I help today?"
