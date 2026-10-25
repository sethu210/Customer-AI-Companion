"""
Product recommendation engine for the multi-agent network. Deterministic code, no LLM.

The engine decides everything a customer is offered: which products, in which order, which colours and sizes are
in stock, the price (from the catalog) and the reason, in plain words. The model only passes the result on, and
nothing can be ordered or changed through it. Settings: "recommendations" in config/service_policy.hocon.

    eligibility()   who may get suggestions at all: an identified customer with a strong or medium sign-in, no
                    case with a person, no risk flags (that refusal never says why);
    recommend()     anchors -> candidates -> filters -> scores -> at most max_items suggestions, each with a reason:
      anchors       the item or product the customer asks about (an item they own, or a catalog match for their
                    words), else their most recent kept purchases;
      candidates    replacement in another size (item returned as wrong_size) or the same item (returned damaged);
                    complement / alternative / upgrade (hand-set relations); bought together (order history);
      filters       active, in stock (in the customer's usual size when known and offered), not already owned,
                    never a product they returned as not_as_described, no category they returned too often;
    get_recommendations()   the above over the adapter's reads (catalog reads cached; the customer's never).
"""

import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from coded_tools.customer_service.identity import IDENTIFIED

REPLACEMENT_SIZE = "replacement_other_size"
REPLACEMENT_SAME = "replacement_same_item"
COMPLEMENT = "complement"
ALTERNATIVE = "alternative"
UPGRADE = "upgrade"
BOUGHT_TOGETHER = "bought_together"
REPLACEMENTS = {REPLACEMENT_SIZE, REPLACEMENT_SAME}
WEIGHT_OF = {REPLACEMENT_SIZE: "replacement", REPLACEMENT_SAME: "replacement", COMPLEMENT: "complement",
             ALTERNATIVE: "alternative", UPGRADE: "upgrade", BOUGHT_TOGETHER: "bought_together"}
REASON_ORDER = [REPLACEMENT_SIZE, REPLACEMENT_SAME, COMPLEMENT, ALTERNATIVE, UPGRADE, BOUGHT_TOGETHER]

NOT_KEPT = {"cancelled", "returned", "return_requested"}
LETTER_SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
BODY_SIZED = {"outerwear", "knitwear", "shirts", "activewear"}  # letter sizes there are the customer's body size
MAX_OPTIONS = 6
STOPWORDS = {"the", "and", "for", "with", "what", "goes", "that", "this", "something", "like", "similar", "need",
             "want", "looking", "recommend", "suggest", "would", "could", "you", "your", "have", "any", "some"}

REFUSAL_HINTS = {
    "not_identified": "The customer is not identified: suggest no products and share no account details.",
    "sign_in_too_weak": "Product suggestions need a stronger sign-in (the app or the website). Suggest no products.",
    "case_with_a_person": "A colleague is handling this case: suggest no products now.",
    # Risk flags: the customer is never told why.
    "not_available": "Product suggestions are not available right now. Suggest no products and do not say why.",
}
NO_ANCHOR_HINTS = {
    "item_not_found": "That item is not on the customer's account; ask which of their items they mean.",
    "no_matching_product": "No product in the catalog matches; ask what they are looking for, or say the store "
                           "does not sell it. Do not invent products.",
    "no_purchase_history": "There are no purchases to base suggestions on; ask what they are looking for.",
}
OK_HINT = ("Offer only these products, in this order, with the reason given (at most {n}). Prices are catalog list "
           "prices; mention only the colours and sizes listed as available. Nothing can be ordered here: point the "
           "customer to the product page. Do not mention stock numbers, scores or other customers.")
NONE_HINT = "Nothing suitable is available right now; say so briefly. Do not invent products."
UNAVAILABLE_HINT = "Product suggestions could not be loaded; say they are not available right now."


class CatalogUnavailable(Exception):
    """A catalog or customer read failed; nothing is suggested rather than guessing."""


@dataclass
class Catalog:
    products: dict[str, dict[str, Any]]
    variants: dict[str, list[dict[str, Any]]]
    product_of_sku: dict[str, str]
    relations: dict[str, list[dict[str, Any]]]
    copurchase: dict[str, list[dict[str, Any]]]


def build_catalog(variant_rows: list[dict], relation_rows: list[dict], copurchase_rows: list[dict]) -> Catalog:
    products: dict[str, dict] = {}
    variants: dict[str, list[dict]] = {}
    for v in variant_rows:
        products.setdefault(v["product_id"], {k: v.get(k) for k in (
            "product_id", "title", "category", "description", "list_price", "currency", "active", "season")})
        variants.setdefault(v["product_id"], []).append({k: v[k] for k in ("sku", "color", "size", "stock")})
    relations: dict[str, list[dict]] = {}
    for r in relation_rows:
        relations.setdefault(r["product_id"], []).append(r)
    copurchase: dict[str, list[dict]] = {}
    for c in copurchase_rows:
        copurchase.setdefault(c["product_id"], []).append(c)
    return Catalog(products, variants, {v["sku"]: v["product_id"] for v in variant_rows}, relations, copurchase)


# ---------------------------------------------------------------------------- eligibility

def eligibility(identity_status: str | None, auth_level: str | None, risk_flags: list[str], case_with_person: bool,
                settings: dict[str, Any]) -> str:
    """'' if suggestions may be made, else the refusal code (REFUSAL_HINTS)."""
    if identity_status not in IDENTIFIED:
        return "not_identified"
    if auth_level not in settings["allowed_auth_levels"]:
        return "sign_in_too_weak"
    if case_with_person:
        return "case_with_a_person"
    if risk_flags:
        return "not_available"
    return ""


def refusal(code: str) -> dict[str, Any]:
    return {"status": "not_offered", "reason": code, "recommendations": [], "message_hint": REFUSAL_HINTS[code]}


# ---------------------------------------------------------------------------- sizes

def size_system(size: str | None) -> str | None:
    if not size:
        return None
    if size.isdigit():
        return "shoe" if int(size) < 20 else "waist"
    return "letter" if size in LETTER_SIZES else None


def _size_key(size: str | None) -> tuple:
    if size is None:
        return (0, 0)
    if size.isdigit():
        return (1, int(size))
    return (2, LETTER_SIZES.index(size)) if size in LETTER_SIZES else (3, 0)


def usual_sizes(kept: list[dict]) -> dict[str, str]:
    """The size the customer keeps most often per size system (newest wins a tie); letter sizes only for clothing."""
    counts: Counter = Counter()
    first_seen: dict[tuple, int] = {}
    for n, item in enumerate(kept):  # newest first
        system = size_system(item["size"])
        if system is None or (system == "letter" and item["category"] not in BODY_SIZED):
            continue
        counts[(system, item["size"])] += 1
        first_seen.setdefault((system, item["size"]), n)
    usual: dict[str, str] = {}
    for (system, size), _ in sorted(counts.items(), key=lambda kv: (-kv[1], first_seen[kv[0]])):
        usual.setdefault(system, size)
    return usual


def _size_for(product: dict, variants: list[dict], usual: dict[str, str]) -> str | None:
    """The customer's usual size for this product, if known and the product is made in it."""
    sizes = {v["size"] for v in variants}
    systems = {size_system(s) for s in sizes} - {None}
    if len(systems) != 1:
        return None
    system = systems.pop()
    if system == "letter" and product["category"] not in BODY_SIZED:
        return None
    size = usual.get(system)
    return size if size in sizes else None


def _options(variants: list[dict], size: str | None = None, sizes: set | None = None,
             color_first: str | None = None) -> list[dict]:
    """In-stock variants (optionally only one size or a set of sizes), the anchor's colour first."""
    rows = [v for v in variants if v["stock"] > 0 and (size is None or v["size"] == size)
            and (sizes is None or v["size"] in sizes)]
    rows.sort(key=lambda v: (v["color"] != color_first, v["color"], _size_key(v["size"])))
    return [{"sku": v["sku"], "color": v["color"], "size": v["size"]} for v in rows[:MAX_OPTIONS]]


# ---------------------------------------------------------------------------- anchors

def words(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    words = [w for w in cleaned.split() if len(w) >= 3 and w not in STOPWORDS]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def match_products(catalog: Catalog, query: str) -> list[str]:
    """Catalog products whose title or category contain most of the query's words (best first)."""
    wanted = words(query)
    scored = []
    for pid, p in catalog.products.items():
        text = f"{p['title']} {p['category']}".lower()
        score = sum(1 for w in wanted if w in text)
        if score:
            scored.append((-score, pid))
    if not scored:
        return []
    best = min(s for s, _ in scored)
    return [pid for s, pid in sorted(scored) if s == best]


def _anchors(catalog: Catalog, history: list[dict], anchor_item_id: str | None,
             product_query: str | None) -> tuple[list[tuple[str, dict | None]], str]:
    """[(product_id, the customer's item of it or None)], or ([], why not)."""
    of = catalog.product_of_sku
    if anchor_item_id:
        item = next((i for i in history if i["item_id"] == anchor_item_id), None)
        if item is None or item["sku"] not in of:
            return [], "item_not_found"
        return [(of[item["sku"]], item)], ""
    if product_query:
        matched = match_products(catalog, product_query)
        if not matched:
            return [], "no_matching_product"
        theirs = {of.get(i["sku"]): i for i in reversed(history) if i["order_status"] != "cancelled"}  # newest wins
        matched.sort(key=lambda pid: (pid not in theirs, pid))  # products the customer has bought first
        return [(pid, theirs.get(pid)) for pid in matched[:2]], ""
    anchors: list[tuple[str, dict | None]] = []
    for item in history:  # newest first
        pid = of.get(item["sku"])
        if pid and item["item_status"] not in NOT_KEPT and pid not in {a for a, _ in anchors}:
            anchors.append((pid, item))
        if len(anchors) == 3:
            break
    return anchors, "" if anchors else "no_purchase_history"


# ---------------------------------------------------------------------------- recommend

def _because(reason: str, anchor_title: str, owned: bool, options: list[dict]) -> str:
    the = "your" if owned else "the"
    if reason == REPLACEMENT_SIZE:
        sizes = sorted({o["size"] for o in options}, key=_size_key)
        return f"The same {anchor_title} in another size ({', '.join(sizes)}), as the one you returned did not fit."
    return {
        REPLACEMENT_SAME: f"A new {anchor_title} to replace the damaged one you returned.",
        COMPLEMENT: f"Goes well with {the} {anchor_title}.",
        ALTERNATIVE: f"An alternative to {the} {anchor_title}.",
        UPGRADE: f"A step up from {the} {anchor_title}.",
        BOUGHT_TOGETHER: f"Often bought together with the {anchor_title}.",
    }[reason]


def _mentions(text: str, wanted: list[str]) -> list[str]:
    found = set(words(text))
    return [w for w in wanted if w in found]


def recommend(catalog: Catalog, history: list[dict], settings: dict[str, Any], *, anchor_item_id: str | None = None,
              product_query: str | None = None, preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Pure function over the catalog and one customer's purchase history (newest first). `preferences` are the
    customer's stated, confirmed ones (companion.preference_signals): a stated size wins over the usual size; a
    product or colour named in "avoids" is never offered (replacements of their own item excepted); colours they
    like come first and add weights.liked_colour to the score.
    """
    prefs = preferences or {"sizes": {}, "likes": [], "avoids": []}
    anchors, why_not = _anchors(catalog, history, anchor_item_id, product_query)
    if not anchors:
        return {"status": "no_anchor", "reason": why_not, "recommendations": [],
                "message_hint": NO_ANCHOR_HINTS[why_not]}
    of = catalog.product_of_sku
    kept = [i for i in history if i["item_status"] not in NOT_KEPT and i["order_status"] != "cancelled"]
    owned = {of.get(i["sku"]) for i in kept}
    never = {of.get(i["sku"]) for i in history if i["return_reason"] == "not_as_described"}
    returned_per_category = Counter(i["category"] for i in history if i["return_reason"])
    tired_of = {c for c, n in returned_per_category.items() if n >= settings["skip_category_after_returns"]}
    usual = {**usual_sizes(kept), **prefs["sizes"]}
    anchor_ids = {pid for pid, _ in anchors}
    weights = settings["weights"]
    best: dict[str, dict] = {}

    def offer(pid: str, reason: str, strength: float, anchor_pid: str, item: dict | None,
              options: list[dict] | None = None) -> None:
        product = catalog.products.get(pid)
        if product is None or not product["active"] or pid in never:
            return
        size = None
        if reason not in REPLACEMENTS:
            if pid in anchor_ids or pid in owned or product["category"] in tired_of:
                return
            if _mentions(f"{product['title']} {product['description']} {product['category']}", prefs["avoids"]):
                return
            size = _size_for(product, catalog.variants[pid], usual)
            options = [o for o in _options(catalog.variants[pid], size=size)
                       if not _mentions(o["color"], prefs["avoids"])]
        if not options:
            return
        liked = sorted({w for o in options for w in _mentions(o["color"], prefs["likes"])})
        options = sorted(options, key=lambda o: not _mentions(o["color"], prefs["likes"]))  # stable: liked first
        score = round(weights[WEIGHT_OF[reason]] * strength + (weights.get("liked_colour", 0) if liked else 0), 4)
        current = best.get(pid)  # one entry per product: the highest score wins, then the earlier reason
        rank = (score, -REASON_ORDER.index(reason))
        if current and (current["_score"], -REASON_ORDER.index(current["reason"])) >= rank:
            return
        anchor_title = catalog.products[anchor_pid]["title"]
        is_owned = item is not None and item["item_status"] not in NOT_KEPT
        best[pid] = {"product_id": pid, "title": product["title"], "category": product["category"],
                     "description": product["description"], "reason": reason, "because": _because(reason, anchor_title, is_owned, options),
                     "list_price": product["list_price"], "currency": product["currency"],
                     "in_your_usual_size": size, "available": options, "_score": score}
        if liked:
            best[pid]["in_colours_you_like"] = liked

    for pid, item in anchors:
        variants = catalog.variants[pid]
        if item and item["return_reason"] == "wrong_size" and item["size"]:
            sizes = sorted({v["size"] for v in variants}, key=_size_key)
            at = sizes.index(item["size"]) if item["size"] in sizes else -1
            near = {sizes[i] for i in (at - 1, at + 1) if at >= 0 and 0 <= i < len(sizes)}
            offer(pid, REPLACEMENT_SIZE, 1.0, pid, item, _options(variants, sizes=near, color_first=item["color"]))
        if item and item["return_reason"] == "damaged":
            offer(pid, REPLACEMENT_SAME, 1.0, pid, item,
                  [o for o in _options(variants, size=item["size"]) if o["sku"] == item["sku"]])
        for r in catalog.relations.get(pid, []):
            offer(r["related_product_id"], r["relation"], r["strength"], pid, item)
        for c in catalog.copurchase.get(pid, []):
            share = c["orders_together"] / c["orders_with_product"] if c["orders_with_product"] else 0.0
            if c["orders_together"] >= settings["min_orders_together"] and share >= settings["min_together_share"]:
                offer(c["related_product_id"], BOUGHT_TOGETHER, share, pid, item)

    ranked = sorted(best.values(), key=lambda r: (-r["_score"], REASON_ORDER.index(r["reason"]), r["product_id"]))
    chosen = [{k: v for k, v in r.items() if k != "_score"} for r in ranked[:settings["max_items"]]]
    based_on = [catalog.products[pid]["title"] for pid, _ in anchors]
    if not chosen:
        return {"status": "none_suitable", "based_on": based_on, "recommendations": [], "message_hint": NONE_HINT}
    return {"status": "ok", "based_on": based_on, "recommendations": chosen,
            "message_hint": OK_HINT.format(n=len(chosen))}


# ---------------------------------------------------------------------------- over the adapter

_CATALOG_CACHE: dict[str, tuple[Catalog, float]] = {}


async def load_catalog(adapter: Any, policy: dict[str, Any]) -> Catalog:
    """The catalog reads, cached per adapter (they hold no customer data). Raises CatalogUnavailable."""
    cached = _CATALOG_CACHE.get(adapter.name)
    if cached and time.monotonic() - cached[1] < policy["recommendations"]["catalog_cache_s"]:
        return cached[0]
    reads = [await adapter.fetch(fn, policy=policy)
             for fn in ("get_catalog_variants", "get_product_relations", "get_product_copurchase")]
    if any(r.failed for r in reads):
        raise CatalogUnavailable(next(r.status for r in reads if r.failed))
    catalog = build_catalog(*(r.rows for r in reads))
    _CATALOG_CACHE[adapter.name] = (catalog, time.monotonic())
    return catalog


async def get_recommendations(adapter: Any, policy: dict[str, Any], *, customer_id: str | None,
                              identity_status: str | None, auth_level: str | None, case_with_person: bool = False,
                              anchor_item_id: str | None = None, product_query: str | None = None,
                              preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    """Eligibility first (risk flags read internally, never returned), then recommend() over fresh history."""
    settings = policy["recommendations"]
    refused = eligibility(identity_status, auth_level, [], case_with_person, settings)
    if refused:
        return refusal(refused)
    signals = await adapter.fetch("get_policy_signals", customer_id, policy=policy)
    if signals.failed:
        return {"status": "unavailable", "recommendations": [], "message_hint": UNAVAILABLE_HINT}
    flags = [f for row in signals.rows for f in (row.get("risk_flags") or "").split(";") if f]
    refused = eligibility(identity_status, auth_level, flags, case_with_person, settings)
    if refused:
        return refusal(refused)
    try:
        catalog = await load_catalog(adapter, policy)
    except CatalogUnavailable:
        return {"status": "unavailable", "recommendations": [], "message_hint": UNAVAILABLE_HINT}
    history = await adapter.fetch("get_purchase_history", customer_id, policy=policy)
    if history.failed:
        return {"status": "unavailable", "recommendations": [], "message_hint": UNAVAILABLE_HINT}
    result = recommend(catalog, history.rows, settings, anchor_item_id=anchor_item_id, product_query=product_query,
                       preferences=preferences)
    if preferences and any(preferences.values()):
        result["stated_preferences_applied"] = preferences
    return result
