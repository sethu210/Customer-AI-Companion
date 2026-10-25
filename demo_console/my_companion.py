"""
"My Companion" (Everly companion plan P7): what the signed-in customer sees and controls, plus the business side's
Launch panel. Everything is read from and written to the same stores the agents use; the customer's actions here are
explicit clicks in their own signed-in account (channel "web"), so they take effect at once:

    GET    /api/session/{id}/my-companion                 chat greeting aside, every tab's data in one view
    POST   /api/session/{id}/my-companion/consent         {consent, granted}  service updates, Stylist Mode, marketing
    POST   /api/session/{id}/my-companion/name            {name}              rename the companion ("" = Everly)
    POST   /api/session/{id}/my-companion/memory          {kind, value}       add a preference (the click is the yes)
    DELETE /api/session/{id}/my-companion/memory/{mid}                        delete one
    POST   /api/session/{id}/my-companion/reset                               name, memory and recall forgotten
    GET    /api/session/{id}/my-companion/export                              "Download my companion data" (JSON)
    POST   /api/session/{id}/my-companion/pick-feedback   {product_id, kind}  opened / not_for_me on a pick
    POST   /api/session/{id}/my-companion/assistants/{grant_id}/revoke        disconnect a personal AI assistant

    GET    /api/launches                                  staged products, launched or not (business side)
    POST   /api/launches/{product_id}                     launch, as an event to Orbit (Orbit -> Aura): score
                                                          opted-in customers, send earned invites
    GET    /api/launches/{product_id}/scores              scores with their breakdown, best first
"""

import json
from datetime import datetime
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import guardian
from coded_tools.customer_service import stylist as sty
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.recommendations import CatalogUnavailable
from coded_tools.customer_service.recommendations import load_catalog

router = APIRouter()
CHANNEL = "web"
MAX_UPDATES = 20


class Consent(BaseModel):
    consent: str
    granted: bool


class Name(BaseModel):
    name: str = ""


class Preference(BaseModel):
    kind: str
    value: str


class PickFeedback(BaseModel):
    product_id: str
    kind: str


def _signed_in(session_id: str) -> tuple[Any, str]:
    from demo_console.app import session

    s = session(session_id)
    if not s.customer.customer_id:
        raise HTTPException(400, "not signed in")
    return s, s.customer.customer_id


async def view(session_id: str) -> dict[str, Any]:
    """Every "My Companion" tab for the signed-in customer, from the stores and the data (no model)."""
    s, cid = _signed_in(session_id)
    policy, adapter, cases = load_policy(), get_adapter(), CaseStore()
    store = cp.CompanionStore(cases.path)
    companion = await cp.load_view(cid, adapter=adapter, policy=policy, cases=cases, exclude_case_id=s.case_id)
    names = (await cp.load_data_profile(adapter, policy, cid)).get("item_names", {})
    since = companion["companion"]["reset_at"]

    followed = []
    gstore = guardian.GuardianStore(cases.path)
    for w in reversed(gstore.watches(customer_id=cid)):
        try:
            issue = next(i for i in cases.case_view(w["case_id"])["issues"] if i["issue_id"] == w["issue_id"])
        except (CaseError, StopIteration):
            continue
        followed.append({**guardian.brief(w, gstore), "what": cp.issue_words(issue, names)})
    updates = [m for m in store.thread(cid, since=since, limit=None) if m["role"] == cp.UPDATE][::-1][:MAX_UPDATES]

    sstore = sty.StylistStore(cases.path)
    feedback = {f["product_id"]: f["kind"] for f in sstore.feedback(cid)}
    try:
        catalog = await load_catalog(adapter, policy)
        picks = [sty.pick_view(sstore, catalog, i, feedback) for i in sty.sent_picks(sstore, cid)]
    except CatalogUnavailable:
        picks = None

    from assistant_gateway.grants import GrantStore

    assistants = [{k: g[k] for k in ("grant_id", "client_name", "scopes", "created_at", "expires_at")}
                  for g in GrantStore().customer_grants(cid)]
    return {"companion": companion["companion"], "profile": companion["profile"],
            "consent_history": store.consent_history(cid)[::-1],
            "updates": {"followed": followed, "messages": updates},
            "stylist": {"on": companion["companion"]["consents"][cp.STYLIST_MODE]["granted"], "picks": picks},
            "assistants": assistants,
            "limits": {"quiet_hours": policy["guardian"]["quiet_hours"],
                       "per_day": policy["guardian"]["max_updates_per_day"],
                       "invites_per_week": policy["stylist"]["max_invites_per_week"]},
            "memory_kinds": list(cp.MEMORY_KINDS)}


@router.get("/api/session/{session_id}/my-companion")
async def get_view(session_id: str) -> dict[str, Any]:
    return await view(session_id)


@router.post("/api/session/{session_id}/my-companion/consent")
async def set_consent(session_id: str, body: Consent) -> dict[str, Any]:
    _, cid = _signed_in(session_id)
    try:
        cp.CompanionStore().set_consent(cid, body.consent, body.granted, channel=CHANNEL)
    except cp.CompanionError as e:
        raise HTTPException(400, str(e)) from e
    return await view(session_id)


@router.post("/api/session/{session_id}/my-companion/name")
async def rename(session_id: str, body: Name) -> dict[str, Any]:
    _, cid = _signed_in(session_id)
    try:
        cp.CompanionStore().rename(cid, body.name, load_policy()["companion"])
    except cp.CompanionError as e:
        raise HTTPException(400, str(e)) from e
    return await view(session_id)


@router.post("/api/session/{session_id}/my-companion/memory")
async def add_preference(session_id: str, body: Preference) -> dict[str, Any]:
    """The customer adds a preference themselves: the click in their own account is their yes."""
    _, cid = _signed_in(session_id)
    settings = load_policy()["companion"]
    store = cp.CompanionStore()
    try:
        value = cp.check_preference(body.kind, body.value, settings)
        if len(store.memories(cid)) >= settings["memory"]["max_active"]:
            raise cp.CompanionError("the most preferences that can be kept are saved; delete one first")
        pending = store.propose_memory(cid, body.kind, value, case_id=None, issue_id=None, turn_id=None,
                                       channel=CHANNEL, pending_hours=settings["memory"]["pending_hours"])
        keep = settings["memory"]["occasion_keep_days" if body.kind == "occasion" else "keep_days"]
        store.confirm_memory(cid, pending["memory_id"], keep_days=keep)
    except cp.CompanionError as e:
        raise HTTPException(400, str(e)) from e
    return await view(session_id)


@router.delete("/api/session/{session_id}/my-companion/memory/{memory_id}")
async def delete_preference(session_id: str, memory_id: str) -> dict[str, Any]:
    _, cid = _signed_in(session_id)
    if not cp.CompanionStore().forget(cid, memory_id.upper()):
        raise HTTPException(404, f"{memory_id} is not one of your preferences")
    return await view(session_id)


@router.post("/api/session/{session_id}/my-companion/reset")
async def reset_companion(session_id: str) -> dict[str, Any]:
    _, cid = _signed_in(session_id)
    cp.CompanionStore().reset(cid, load_policy()["companion"])
    return await view(session_id)


@router.get("/api/session/{session_id}/my-companion/export")
async def export(session_id: str) -> Response:
    """"Download my companion data": what the companion store holds plus the profile derived from the data."""
    _, cid = _signed_in(session_id)
    current = await view(session_id)
    data = {"exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **cp.CompanionStore().export(cid, load_policy()["companion"]),
            "profile": current["profile"], "stylist_picks": current["stylist"]["picks"],
            "followed_requests": current["updates"]["followed"]}
    return Response(json.dumps(data, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="my-companion-data.json"'})


@router.post("/api/session/{session_id}/my-companion/pick-feedback")
async def pick_feedback(session_id: str, body: PickFeedback) -> dict[str, Any]:
    _, cid = _signed_in(session_id)
    try:
        sty.Stylist().feedback(cid, body.product_id, body.kind)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await view(session_id)


@router.post("/api/session/{session_id}/my-companion/assistants/{grant_id}/revoke")
async def revoke(session_id: str, grant_id: str) -> dict[str, Any]:
    from assistant_gateway.grants import GrantStore

    _, cid = _signed_in(session_id)
    if not GrantStore().revoke(grant_id, cid):
        raise HTTPException(404, "not one of your connected assistants")
    return await view(session_id)


# ---------------------------------------------------------------- Launch panel (business side)


@router.get("/api/launches")
async def launches() -> dict[str, Any]:
    stylist = sty.Stylist()
    try:
        staged = await stylist.staged()
    except CatalogUnavailable as e:
        raise HTTPException(503, f"stylist data unavailable: {e}") from e
    catalog = await load_catalog(stylist.adapter, stylist.policy)
    done = {r["product_id"]: r["summary"] for r in stylist.store.launched()}
    rows = [{**r, "title": catalog.products[r["product_id"]]["title"],
             "list_price": catalog.products[r["product_id"]]["list_price"], "summary": done.get(r["product_id"])}
            for r in staged]
    return {"clock": stylist.clock.now_ts(), "launches": rows,
            "opted_in": len(stylist.companions.customers_with(cp.STYLIST_MODE, stylist.policy["companion"])),
            "min_affinity": stylist.settings["min_affinity"]}


@router.post("/api/launches/{product_id}")
async def launch(product_id: str) -> dict[str, Any]:
    """The launch goes to Orbit as an event; Orbit has Aura launch it (docs/orbit_tick_plan.md L1-L2)."""
    from demo_console.app import STATE  # the console's Orbit clock (created at start-up)

    out = await STATE["clock"].launch(product_id, launched_by="launch panel")
    if out["error"]:
        raise HTTPException(400 if "not_launched" in out["error"] else 503, out["error"])
    return await launches()


@router.get("/api/launches/{product_id}/scores")
def scores(product_id: str, limit: int = 15) -> dict[str, Any]:
    store = sty.StylistStore()
    rows = store.scores(product_id=product_id)
    invites = {i["customer_id"]: i for i in store.invites() if i["product_id"] == product_id}
    out = []
    for r in rows[:max(1, min(limit, 200))]:
        b = r["breakdown"]
        invite = invites.get(r["customer_id"])
        out.append({"customer_id": r["customer_id"], "score": r["score"], "outcome": r["outcome"],
                    "gates": b.get("gates") or [], "points": b.get("points") or {}, "penalty": b.get("penalty", 0),
                    "owned_item": b.get("owned_item"), "invite": invite and {k: invite[k] for k in (
                        "status", "held_reason", "message")}})
    return {"product_id": product_id, "scores": out, "total": len(rows)}
