"""
Personal Stylist Mode (Everly companion plan P5): when a staged product is launched, customers who switched Stylist
Mode on get an invite only when their own data says it really suits them. Deterministic code, no LLM: the model
never decides who is invited, the score, the reason or the wording.

A launch (Stylist.launch, on the simulator's clock) checks every customer whose stylist_mode consent is on:

    gates      any one fails -> no score, no invite (the reason is stored):
               not_available (risk flags, never said why) · open_trouble (a case with a person or an escalated
               follow-up: service before selling) · already_owned · rejected_category (returned as not as described,
               or returned too often) · avoided (their stated "avoids") · size_unknown / not_in_your_size (in stock
               in their stated or usual size, unless one size) · already_invited · weekly_limit
    signals    each 0-1, times stylist.weights (35/20/15/10/10/10):
               complement   the strongest complement relation between the product and an item they kept
               style        share of the product's style tags that their kept items carry (or they said they like)
               colour       its best in-stock colour against the colour families they keep (colour_harmony), 1.0
                            for a colour they said they like
               category     items kept in its category (2 or more = 1.0)
               price        its price against the most they usually pay
               season       its season against the launch month
    penalty    "not for me" on an earlier pick: same category / 2+ shared style tags (stylist.not_for_me_penalty)
    invite     score >= stylist.min_affinity AND an item they own that it goes with (the reason names it)

Every score is stored with its breakdown, invited or not. Invites are fixed templates filled with facts, sent through
companion messaging (messaging.py: quiet hours, the daily limit shared with the Guardian, service first) to the
outbox, so they appear in the customer's thread. A held invite is sent later or dropped after invite_valid_days.
A launch is recorded once: launching again sends nothing new.
"""

import json
import sqlite3
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import messaging
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.guardian import OUTBOX
from coded_tools.customer_service.guardian import customer_watching
from coded_tools.customer_service.recommendations import BODY_SIZED
from coded_tools.customer_service.recommendations import NOT_KEPT
from coded_tools.customer_service.recommendations import Catalog
from coded_tools.customer_service.recommendations import CatalogUnavailable
from coded_tools.customer_service.recommendations import load_catalog
from coded_tools.customer_service.recommendations import size_system
from coded_tools.customer_service.recommendations import usual_sizes
from coded_tools.customer_service.recommendations import words
from coded_tools.customer_service.simulator import TS
from coded_tools.customer_service.simulator import Simulator
from coded_tools.customer_service.simulator import parse_ts

_LOCK = threading.Lock()
SIGNALS = ("complement", "style", "colour", "category", "price", "season")
FEEDBACK_KINDS = ("opened", "not_for_me", "bought")
SENT, HELD, EXPIRED, SUPPRESSED = "sent", "held", "expired", "suppressed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stylist_launches (
    product_id TEXT PRIMARY KEY, launch_id TEXT, launched_at TEXT NOT NULL, by TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS stylist_scores (
    product_id TEXT NOT NULL, customer_id TEXT NOT NULL, score INTEGER, outcome TEXT NOT NULL, breakdown TEXT,
    scored_at TEXT NOT NULL, PRIMARY KEY (product_id, customer_id)
);
CREATE TABLE IF NOT EXISTS stylist_invites (
    invite_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product_id TEXT NOT NULL, score INTEGER, message TEXT,
    status TEXT NOT NULL, held_reason TEXT, created_at TEXT NOT NULL, sent_at TEXT, expires_at TEXT,
    UNIQUE (customer_id, product_id)
);
CREATE TABLE IF NOT EXISTS stylist_feedback (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, product_id TEXT NOT NULL,
    kind TEXT NOT NULL, at TEXT NOT NULL
);
"""

# The invite, filled with facts by code (decision C5). No promise beyond what the data shows.
INVITE = ("New in at {retailer}: the {title}, {price}. I picked it for you because it goes with your {owned}{style}. "
          "{stock} Not for you? Just say so, or switch Personal Stylist Mode off at any time.")


def _ts(dt: datetime) -> str:
    return dt.strftime(TS)


def _money(amount: float, currency: str | None) -> str:
    return f"${amount:,.2f}" if (currency or "USD") == "USD" else f"{amount:,.2f} {currency}"


# ---------------------------------------------------------------------------- the store


class StylistStore:
    """Launches, scores, invites and feedback, in the case store's SQLite file (CASE_STORE_PATH)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else CaseStore().path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def launched(self, product_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM stylist_launches", ()
        if product_id:
            sql, args = sql + " WHERE product_id = ?", (product_id,)
        with self._connect() as db:
            return [{**dict(r), "summary": json.loads(r["summary"] or "{}")}
                    for r in db.execute(sql + " ORDER BY launched_at", args)]

    def add_launch(self, product_id: str, launch_id: str, at: str, by: str, summary: dict[str, Any]) -> bool:
        """False when the product was already launched (nothing is recorded twice)."""
        with _LOCK, self._connect() as db:
            try:
                db.execute("INSERT INTO stylist_launches VALUES (?, ?, ?, ?, ?)",
                           (product_id, launch_id, at, by, json.dumps(summary)))
            except sqlite3.IntegrityError:
                return False
        return True

    def save_score(self, result: dict[str, Any], at: str) -> None:
        with _LOCK, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO stylist_scores VALUES (?, ?, ?, ?, ?, ?)",
                       (result["product_id"], result["customer_id"], result["score"], result["outcome"],
                        json.dumps(result, default=str), at))

    def scores(self, *, product_id: str | None = None, customer_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM stylist_scores WHERE 1 = 1", []
        for col, value in (("product_id", product_id), ("customer_id", customer_id)):
            if value:
                sql, args = sql + f" AND {col} = ?", [*args, value]
        with self._connect() as db:
            return [{**dict(r), "breakdown": json.loads(r["breakdown"])}
                    for r in db.execute(sql + " ORDER BY score DESC, customer_id", args)]

    def add_invite(self, customer_id: str, product_id: str, score: int, message: str, at: datetime,
                   valid_days: float) -> dict[str, Any] | None:
        """A new invite (status 'new'), or None if this customer was already invited for this product."""
        invite_id = f"INV-{uuid.uuid4().hex[:10].upper()}"
        with _LOCK, self._connect() as db:
            try:
                db.execute("INSERT INTO stylist_invites VALUES (?, ?, ?, ?, ?, 'new', NULL, ?, NULL, ?)",
                           (invite_id, customer_id, product_id, score, message, _ts(at),
                            _ts(at + timedelta(days=valid_days))))
            except sqlite3.IntegrityError:
                return None
            return dict(db.execute("SELECT * FROM stylist_invites WHERE invite_id = ?", (invite_id,)).fetchone())

    def set_invite(self, invite_id: str, status: str, *, held_reason: str | None = None,
                   sent_at: str | None = None) -> None:
        with _LOCK, self._connect() as db:
            db.execute("UPDATE stylist_invites SET status = ?, held_reason = ?, sent_at = COALESCE(?, sent_at) "
                       "WHERE invite_id = ?", (status, held_reason, sent_at, invite_id))

    def invites(self, *, customer_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM stylist_invites WHERE 1 = 1", []
        for col, value in (("customer_id", customer_id), ("status", status)):
            if value:
                sql, args = sql + f" AND {col} = ?", [*args, value]
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY created_at, rowid", args)]

    def add_feedback(self, customer_id: str, product_id: str, kind: str, at: str) -> None:
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"feedback is one of {', '.join(FEEDBACK_KINDS)}")
        with _LOCK, self._connect() as db:
            db.execute("INSERT INTO stylist_feedback (customer_id, product_id, kind, at) VALUES (?, ?, ?, ?)",
                       (customer_id, product_id, kind, at))

    def feedback(self, customer_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM stylist_feedback WHERE customer_id = ? ORDER BY "
                                                "entry_id", (customer_id,))]


# ---------------------------------------------------------------------------- facts and scoring (pure)


@dataclass
class StyleData:
    catalog: Catalog
    styles: dict[str, list[str]]
    families: dict[str, str]
    harmony: dict[tuple[str, str], float]

    def harmony_of(self, a: str | None, b: str | None) -> float:
        if a is None or b is None:
            return 0.0
        return self.harmony.get((min(a, b), max(a, b)), 0.0)


@dataclass
class Facts:
    """One customer, as read for a launch."""
    customer_id: str
    history: list[dict[str, Any]]                 # one row per item (incl. returned ones), newest first
    prices: list[float]                           # what they paid for items they kept
    risk: bool
    trouble: str                                  # '' or why they must not be marketed to now
    memories: list[dict[str, Any]] = field(default_factory=list)
    invited: set[str] = field(default_factory=set)  # products they were invited for already
    invites_this_week: int = 0
    not_for_me: list[str] = field(default_factory=list)

    @property
    def kept(self) -> list[dict[str, Any]]:
        return [i for i in self.history if i["item_status"] not in NOT_KEPT and i["order_status"] != "cancelled"]


def _mentions(text: str, wanted: list[str]) -> list[str]:
    found = set(words(text))
    return [w for w in wanted if w in found]


def _size(product: dict[str, Any], variants: list[dict[str, Any]], sizes: dict[str, str]) -> tuple[bool, str | None]:
    """(size known or not needed, the size): one size needs none; letter sizes only for body-sized categories."""
    systems = {size_system(v["size"]) for v in variants} - {None}
    if not systems:
        return True, None
    if len(systems) != 1:
        return False, None
    system = systems.pop()
    if system == "letter" and product["category"] not in BODY_SIZED:
        return False, None
    return (sizes.get(system) is not None), sizes.get(system)


def score(facts: Facts, product_id: str, data: StyleData, settings: dict[str, Any], *, month: int,
          skip_category_after_returns: int) -> dict[str, Any]:
    """Gates, signals, penalty and the decision for one customer and one launched product. Pure."""
    catalog = data.catalog
    product = catalog.products[product_id]
    of = catalog.product_of_sku
    out: dict[str, Any] = {"product_id": product_id, "customer_id": facts.customer_id, "gates": [], "signals": {},
                           "points": {}, "penalty": 0, "score": None, "owned_item": None, "size": None,
                           "colours": [], "outcome": ""}
    prefs = cp.preference_signals(facts.memories, data.families)
    kept = facts.kept
    kept_pids = [of.get(i["sku"]) for i in kept]

    gates = out["gates"]
    if facts.risk:
        gates.append("not_available")
    if facts.trouble:
        gates.append("open_trouble")
    if product_id in kept_pids:
        gates.append("already_owned")
    returned = [i for i in facts.history if i.get("return_reason") and i.get("category") == product["category"]]
    if any(i["return_reason"] == "not_as_described" for i in returned) or len(returned) >= skip_category_after_returns:
        gates.append("rejected_category")
    if _mentions(f"{product['title']} {product['description']} {product['category']}", prefs["avoids"]):
        gates.append("avoided")
    sizes = {**usual_sizes(kept), **prefs["sizes"]}
    known, size = _size(product, catalog.variants[product_id], sizes)
    options = [v for v in catalog.variants[product_id] if v["stock"] > 0 and (size is None or v["size"] == size)
               and not _mentions(v["color"], prefs["avoids"])]
    if not known:
        gates.append("size_unknown")
    elif not options:
        gates.append("not_in_your_size")
    if product_id in facts.invited:
        gates.append("already_invited")
    if facts.invites_this_week >= settings["max_invites_per_week"]:
        gates.append("weekly_limit")
    out["size"] = size
    if gates:
        out["outcome"] = "gated"
        return out

    # complement: the strongest relation either way between the product and an item they kept
    best, owned = 0.0, None
    for item, pid in zip(kept, kept_pids):
        strengths = [r["strength"] for r in catalog.relations.get(product_id, [])
                     if r["related_product_id"] == pid and r["relation"] == "complement"]
        strengths += [r["strength"] for r in catalog.relations.get(pid or "", [])
                      if r["related_product_id"] == product_id and r["relation"] == "complement"]
        if strengths and max(strengths) > best:
            best, owned = max(strengths), item
    signals = {"complement": best}

    # style: the product's tags that their kept items carry (on two or more, or one when they kept fewer than three)
    tags = Counter(t for pid in set(kept_pids) for t in data.styles.get(pid or "", []))
    floor = 2 if len(set(kept_pids)) >= 3 else 1
    vocabulary = {t for ts in data.styles.values() for t in ts}
    theirs = {t for t, n in tags.items() if n >= floor} | (set(prefs["likes"]) & vocabulary)
    new_tags = data.styles.get(product_id, [])
    shared = sorted(set(new_tags) & theirs)
    signals["style"] = len(shared) / len(new_tags) if new_tags else 0.0

    # colour: each in-stock colour against the families they keep; a colour they said they like counts fully
    kept_families = Counter(data.families.get(str(i.get("color")).lower()) for i in kept if i.get("color"))
    kept_families.pop(None, None)
    colour_scores: dict[str, float] = {}
    for v in options:
        c = v["color"].lower()
        if _mentions(c, prefs["likes"]):
            colour_scores[c] = 1.0
        elif kept_families:
            colour_scores[c] = sum(n * data.harmony_of(data.families.get(c), f) for f, n in kept_families.items()) \
                / sum(kept_families.values())
        else:
            colour_scores[c] = 0.5
    ranked = sorted(colour_scores, key=lambda c: (-colour_scores[c], c))
    signals["colour"] = colour_scores[ranked[0]] if ranked else 0.0
    out["colours"] = ranked[:3]

    in_category = sum(1 for i in kept if i.get("category") == product["category"])
    signals["category"] = min(1.0, in_category / 2)
    if facts.prices:
        ceiling = max(facts.prices) * 1.1
        signals["price"] = 1.0 if product["list_price"] <= ceiling else \
            max(0.0, 1 - (product["list_price"] - ceiling) / max(facts.prices))
    else:
        signals["price"] = 0.5
    signals["season"] = 1.0 if month in settings["season_months"].get(product.get("season") or "all_year", []) \
        else settings["season_off"]

    weights = settings["weights"]
    out["signals"] = {k: round(signals[k], 3) for k in SIGNALS}
    out["points"] = {k: round(weights[k] * signals[k], 1) for k in SIGNALS}
    rule = settings["not_for_me_penalty"]
    penalty = 0
    for other in facts.not_for_me:
        p = catalog.products.get(other)
        if p is None or other == product_id:
            continue
        penalty += rule["same_category"] if p["category"] == product["category"] else 0
        penalty += rule["shared_styles"] if len(set(data.styles.get(other, [])) & set(new_tags)) >= 2 else 0
    out["penalty"] = min(penalty, rule["max"])
    out["score"] = max(0, round(sum(out["points"].values()) - out["penalty"]))
    out["shared_styles"] = shared
    if owned is not None:
        out["owned_item"] = cp.item_name(owned)
    if out["score"] < settings["min_affinity"]:
        out["outcome"] = "below_threshold"
    elif owned is None:
        out["outcome"] = "no_owned_item"  # nothing of theirs to name as the reason
    else:
        out["outcome"] = "invite"
    return out


def invite_message(result: dict[str, Any], product: dict[str, Any], retailer: str) -> str:
    style = f" and suits the {' and '.join(result['shared_styles'][:2])} style you keep" \
        if result["signals"].get("style", 0) >= 0.5 and result["shared_styles"] else ""
    shown = result["colours"]
    colours = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    stock = (f"It's in stock in your size ({result['size']}) in {colours}." if result["size"]
             else f"It's in stock in {colours}.")
    return INVITE.format(retailer=retailer, title=product["title"], owned=result["owned_item"], style=style,
                         stock=stock, price=_money(product["list_price"], product.get("currency")))


# ---------------------------------------------------------------------------- picks as the customer sees them

SEASON_WORDS = {"cold_weather": "cold-weather", "wet_weather": "wet-weather", "warm_weather": "warm-weather",
                "spring_fall": "in-between-season", "all_year": "year-round"}


def reasons(breakdown: dict[str, Any], season: str | None) -> list[str]:
    """Why a pick was chosen, from its stored breakdown, in fixed words (never the score)."""
    s = breakdown.get("signals") or {}
    out = []
    if breakdown.get("owned_item"):
        strength = "very well" if s.get("complement", 0) >= 0.8 else "well"
        out.append(f"It goes {strength} with your {breakdown['owned_item']}.")
    if s.get("style", 0) >= 0.5 and breakdown.get("shared_styles"):
        out.append(f"It shares the {' and '.join(breakdown['shared_styles'][:2])} style of what you keep.")
    if s.get("colour", 0) >= 0.8 and breakdown.get("colours"):
        out.append(f"Its {breakdown['colours'][0]} goes well with the colours you wear.")
    if s.get("category", 0) >= 0.5:
        out.append("You keep pieces like this.")
    if s.get("price", 0) >= 1.0:
        out.append("Its price is within what you usually spend.")
    if s.get("season", 0) >= 1.0 and season:
        out.append(f"It is a {SEASON_WORDS.get(season, 'seasonal')} piece, right for this time of year.")
    return out


def sent_picks(store: StylistStore, customer_id: str) -> list[dict[str, Any]]:
    """The invites really sent to the customer, newest first."""
    return [i for i in reversed(store.invites(customer_id=customer_id)) if i["status"] == SENT]


def pick_view(store: StylistStore, catalog: Catalog, invite: dict[str, Any], feedback: dict[str, str]) -> dict[str, Any]:
    """One sent pick for the customer: product, price, reasons, what is in stock in their size now, their feedback."""
    product = catalog.products[invite["product_id"]]
    b = next(r for r in store.scores(product_id=invite["product_id"], customer_id=invite["customer_id"]))["breakdown"]
    size = b.get("size")
    colours = sorted({v["color"] for v in catalog.variants[invite["product_id"]]
                      if v["stock"] > 0 and (size is None or v["size"] == size)})
    return {"product_id": invite["product_id"], "title": product["title"], "category": product["category"],
            "description": product["description"], "list_price": product["list_price"],
            "currency": product["currency"], "sent_on": (invite["sent_at"] or "")[:10],
            "picked_because": reasons(b, product.get("season")),
            "in_stock_now": {"size": size or "one size", "colours": colours},
            "your_feedback": feedback.get(invite["product_id"])}


# ---------------------------------------------------------------------------- the runner


class Stylist:
    def __init__(self, *, cases: CaseStore | None = None, store: StylistStore | None = None, adapter: Any = None,
                 policy: dict[str, Any] | None = None, simulator: Simulator | None = None) -> None:
        from coded_tools.customer_service.policy import load_policy

        self.cases = cases or CaseStore()
        self.store = store or StylistStore(self.cases.path)
        self.companions = cp.CompanionStore(self.cases.path)
        self.actions = ActionStore()
        self._adapter = adapter
        self.policy = policy or load_policy()
        self.settings = self.policy["stylist"]
        self.clock = simulator or Simulator(reference_date=self.policy["reference_date"])

    @property
    def adapter(self) -> Any:
        if self._adapter is None:
            from coded_tools.customer_service.data_adapter import get_adapter

            self._adapter = get_adapter()
        return self._adapter

    async def staged(self) -> list[dict[str, Any]]:
        """Products staged for launch, with whether they were launched here already."""
        result = await self.adapter.fetch("get_product_launches", policy=self.policy)
        if result.failed:
            raise CatalogUnavailable(result.status)
        done = {r["product_id"]: r for r in self.store.launched()}
        return [{**r, "launched_at": (done.get(r["product_id"]) or {}).get("launched_at")} for r in result.rows]

    async def _data(self) -> StyleData:
        catalog = await load_catalog(self.adapter, self.policy)
        style = await cp.load_style_data(self.adapter, self.policy)
        harmony = await self.adapter.fetch("get_colour_harmony", policy=self.policy)
        if style is None or harmony.failed:
            raise CatalogUnavailable("stylist data unavailable")
        return StyleData(catalog, style["styles"], style["families"],
                         {(r["family"], r["other_family"]): r["strength"] for r in harmony.rows})

    def _trouble(self, customer_id: str) -> str:
        """Service before selling: a case with a person, or a follow-up that is escalated or with a person."""
        if any(h["customer_id"] == customer_id for h in self.cases.handoffs(status="open")):
            return "a colleague is working on a request"
        for w in customer_watching(customer_id, cases=self.cases):
            if w["with_a_person"] or w["state"] == "escalated":
                return "a request is with a colleague"
        return ""

    async def _facts(self, customer_id: str, now: datetime) -> Facts | None:
        reads = {fn: await self.adapter.fetch(fn, customer_id, policy=self.policy)
                 for fn in ("get_purchase_history", "get_customer_context", "get_policy_signals")}
        if any(r.failed for r in reads.values()):
            return None
        history = cp.unique_items(reads["get_purchase_history"].rows)
        prices = [float(r["price"]) for r in cp.unique_items(reads["get_customer_context"].rows)
                  if cp.is_kept(r) and r.get("price") is not None]
        risk = any(f for row in reads["get_policy_signals"].rows for f in (row.get("risk_flags") or "").split(";"))
        invites = self.store.invites(customer_id=customer_id)
        week_ago = _ts(now - timedelta(days=7))
        return Facts(customer_id, history, prices, risk, self._trouble(customer_id),
                     memories=self.companions.memories(customer_id),
                     invited={i["product_id"] for i in invites},
                     invites_this_week=sum(1 for i in invites if i["status"] in (SENT, HELD)
                                           and i["created_at"] >= week_ago),
                     not_for_me=[f["product_id"] for f in self.store.feedback(customer_id)
                                 if f["kind"] == "not_for_me"])

    async def launch(self, product_id: str, *, by: str = "merchandiser") -> dict[str, Any]:
        """Launches a staged product: scores every opted-in customer and sends the invites that are earned."""
        staged = {r["product_id"]: r for r in await self.staged()}
        if product_id not in staged:
            raise ValueError(f"{product_id} is not staged for launch")
        if staged[product_id]["launched_at"]:
            return {"product_id": product_id, "already_launched": True, **self.store.launched(product_id)[0]["summary"]}
        now = self.clock.now()
        data = await self._data()
        product = data.catalog.products[product_id]
        candidates = self.companions.customers_with(cp.STYLIST_MODE, self.policy["companion"])
        summary: dict[str, Any] = {"product_id": product_id, "title": product["title"], "launched_at": _ts(now),
                                   "candidates": len(candidates), "scored": 0, "gated": {}, "invited": [],
                                   "below_threshold": 0, "data_unavailable": []}
        for customer_id in candidates:
            facts = await self._facts(customer_id, now)
            if facts is None:
                summary["data_unavailable"].append(customer_id)
                self.store.save_score({"product_id": product_id, "customer_id": customer_id, "score": None,
                                       "outcome": "data_unavailable"}, _ts(now))
                continue
            result = score(facts, product_id, data, self.settings, month=now.month,
                           skip_category_after_returns=self.policy["recommendations"]["skip_category_after_returns"])
            self.store.save_score(result, _ts(now))
            if result["outcome"] == "gated":
                for gate in result["gates"]:
                    summary["gated"][gate] = summary["gated"].get(gate, 0) + 1
                continue
            summary["scored"] += 1
            if result["outcome"] != "invite":
                summary["below_threshold"] += 1
                continue
            message = invite_message(result, product, self.policy["companion"]["retailer"])
            invite = self.store.add_invite(customer_id, product_id, result["score"], message, now,
                                           self.settings["invite_valid_days"])
            if invite is not None:
                self._deliver(invite, now)
                summary["invited"].append({"customer_id": customer_id, "score": result["score"],
                                           "status": self.store.invites(customer_id=customer_id)[-1]["status"]})
        self.store.add_launch(product_id, staged[product_id]["launch_id"], _ts(now), by, summary)
        return summary

    def _deliver(self, invite: dict[str, Any], now: datetime) -> None:
        """Send now, hold (quiet hours, service first, daily limit) or suppress (Stylist Mode switched off)."""
        consents = self.companions.consents(invite["customer_id"], self.policy["companion"])
        if not consents[cp.STYLIST_MODE]["granted"]:
            self.store.set_invite(invite["invite_id"], SUPPRESSED, held_reason="stylist_mode_off")
            return
        reason = messaging.invite_hold_reason(self.store.path, invite["customer_id"], now, self.policy)
        if reason:
            self.store.set_invite(invite["invite_id"], HELD, held_reason=reason)
            return
        self.store.set_invite(invite["invite_id"], SENT, sent_at=_ts(now))
        self.actions.append(OUTBOX, {"customer_id": invite["customer_id"], "source": "stylist",
                                     "invite_id": invite["invite_id"], "product_id": invite["product_id"],
                                     "kind": "stylist_invite", "message": invite["message"],
                                     "sent_at_clock": _ts(now), "status": "pending_send"})

    async def run_once(self) -> dict[str, Any]:
        """Held invites: send those that may go now, drop those past invite_valid_days (oldest first)."""
        now = self.clock.now()
        out: dict[str, Any] = {"clock": _ts(now), "sent": [], "expired": [], "suppressed": []}
        for invite in self.store.invites(status=HELD):
            if parse_ts(invite["expires_at"]) <= now:
                self.store.set_invite(invite["invite_id"], EXPIRED, held_reason=invite["held_reason"])
                out["expired"].append(invite["invite_id"])
                continue
            self._deliver(invite, now)
            status = next(i["status"] for i in self.store.invites(customer_id=invite["customer_id"])
                          if i["invite_id"] == invite["invite_id"])
            if status in (SENT, SUPPRESSED):
                out[status].append(invite["invite_id"])
        return out

    def feedback(self, customer_id: str, product_id: str, kind: str) -> None:
        """What the customer did with a pick: opened, "not for me" (lowers similar launches) or bought."""
        if not any(i["product_id"] == product_id for i in self.store.invites(customer_id=customer_id)):
            raise ValueError(f"{customer_id} was not invited for {product_id}")
        self.store.add_feedback(customer_id, product_id, kind, self.clock.now_ts())
