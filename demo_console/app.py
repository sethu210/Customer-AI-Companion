r"""
Demo console API and page (FastAPI). Local only: binds to 127.0.0.1. The sign-in is SIMULATED for demos: the
browser picks a customer, and this server builds the website sly_data a real gateway would send. The browser can
never send or edit sly_data.

Uses the same stores and settings as the agent server: ACTION_STORE_DIR, CASE_STORE_PATH, CUSTOMER_DATA_BACKEND.
DEMO_RECORDING=1 enables the Record toggle (scripts\demo.ps1 -Record). Replay pages (?replay=<storyboard>) are served
for the video renderer and need no agent server.

    .\.venv\Scripts\python.exe -m demo_console            # http://127.0.0.1:8095
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from coded_tools.customer_service import companion
from coded_tools.customer_service import guardian
from coded_tools.customer_service import simulator
from coded_tools.customer_service.action_store import ActionStore
from demo_console import agents
from demo_console import my_companion
from demo_console import recording
from demo_console.catalog import WEBSITE
from demo_console.catalog import Catalog
from demo_console.catalog import Customer
from demo_console.orbit_tick import OrbitClock
from demo_console.session import DemoSession
from evaluation.expectations import ROOT

STATIC = Path(__file__).with_name("static")
CONFIG_FILE = ROOT / "demo" / "demo.yaml"
STORYBOARDS = ROOT / "runtime" / "demo" / "render"


def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


STATE: dict[str, Any] = {"catalog": None, "sessions": {}, "config": load_config()}
LOGGER = logging.getLogger(__name__)


def seed_stylist_mode(customer_ids: list[str]) -> int:
    """
    Personal Stylist Mode on for the demo's set-up customers (demo.yaml stylist_mode_on), recorded as their consent.
    A customer with any Stylist Mode choice already recorded keeps it. Returns how many were switched on.
    """
    store = companion.CompanionStore()
    seeded = 0
    for cid in customer_ids:
        if not any(c["consent"] == companion.STYLIST_MODE for c in store.consent_history(cid)):
            store.set_consent(cid, companion.STYLIST_MODE, True, channel="demo set-up")
            seeded += 1
    return seeded


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if os.environ.get("DEMO_REPLAY_ONLY") != "1":  # the video renderer needs no customer data
        STATE["catalog"] = await Catalog.aload()
        seeded = seed_stylist_mode(STATE["config"].get("stylist_mode_on") or [])
        LOGGER.info("Personal Stylist Mode switched on for %d demo customer(s)", seeded)
        # Orbit's clock: Orbit runs the Guardian's checks and releases held picks (service updates first)
        STATE["clock"] = OrbitClock(STATE["config"]["neuro_san_url"],
                                    every_s=float(STATE["config"].get("orbit_tick_every_s", 60)),
                                    outbox=lambda: [r for r in ActionStore().read(guardian.OUTBOX)
                                                    if not r.get("private")])
        task = asyncio.create_task(STATE["clock"].run())
    yield
    if task:
        task.cancel()
    for session in STATE["sessions"].values():
        session.finish_take()


app = FastAPI(title="Everly demo console", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(my_companion.router)  # "My Companion" tabs and the Launch panel (companion plan P7)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def recording_enabled() -> bool:
    return os.environ.get("DEMO_RECORDING") == "1"


def catalog() -> Catalog:
    if STATE["catalog"] is None:
        raise HTTPException(503, "customer data is not loaded (replay-only server)")
    return STATE["catalog"]


def session(session_id: str) -> DemoSession:
    if session_id not in STATE["sessions"]:
        raise HTTPException(404, "session ended; sign in again")
    return STATE["sessions"][session_id]


class SignIn(BaseModel):
    customer_id: str | None = None  # None = "not signed in", only when enabled in demo.yaml


class Record(BaseModel):
    on: bool


@app.get("/")
def page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    cfg = STATE["config"]
    return {"brand": cfg["brand"], "tagline": cfg["tagline"], "retailer": cfg.get("retailer", ""),
            "not_signed_in": bool(cfg.get("not_signed_in")),
            "recording": recording_enabled(), "backend": os.environ.get("CUSTOMER_DATA_BACKEND", "mock"),
            "model": recording.model_name(), "network": agents.NETWORK_PATH, "simulator": simulator.enabled(),
            "agents": [{"name": n, "label": label, "sub": sub} for n, (label, sub) in agents.AGENTS.items()]}


@app.get("/api/customers")
def customers(query: str = "", kind: str = "all", offset: int = 0, limit: int = 60) -> dict[str, Any]:
    if kind not in ("all", "scenario", "other"):
        raise HTTPException(400, "kind is all, scenario or other")
    return catalog().search(query, kind, offset, min(limit, 200))


@app.post("/api/session")
def sign_in(body: SignIn) -> dict[str, Any]:
    if body.customer_id is None:
        if not STATE["config"].get("not_signed_in"):
            raise HTTPException(403, "'not signed in' is switched off in demo/demo.yaml")
        customer = Customer("", "Visitor (not signed in)", "", "", "", 0)
        sly = {"channel": WEBSITE}
    else:
        try:
            customer = catalog().get(body.customer_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        sly = catalog().gateway_sly_data(body.customer_id)
    s = DemoSession(customer, sly, STATE["config"]["neuro_san_url"])
    STATE["sessions"][s.id] = s
    return {"session_id": s.id, "customer": customer.public(), "sly_data_sent": sly}


@app.post("/api/session/{session_id}/reset")
def reset(session_id: str) -> dict[str, Any]:
    s = session(session_id)
    take = s.finish_take()
    s.reset()
    return {"ok": True, "take": take}


@app.delete("/api/session/{session_id}")
def sign_out(session_id: str) -> dict[str, Any]:
    take = session(session_id).finish_take()
    STATE["sessions"].pop(session_id, None)
    return {"ok": True, "take": take}


@app.post("/api/session/{session_id}/record")
def record(session_id: str, body: Record) -> dict[str, Any]:
    if not recording_enabled():
        raise HTTPException(403, "recording is off; start the console with scripts\\demo.ps1 -Record")
    s = session(session_id)
    if body.on:
        if s.take is None:
            s.start_take()
        return {"recording": True}
    return {"recording": False, "take": s.finish_take()}


@app.get("/api/session/{session_id}/case")
def case(session_id: str) -> dict[str, Any] | None:
    return session(session_id).case_view()


# ---------------------------------------------------------------- inbox and Resolution Guardian (E3)


class Advance(BaseModel):
    hours: float


class SimEvent(BaseModel):
    watch_id: str
    event: str


class Updates(BaseModel):
    on: bool


def inbox(s: DemoSession) -> dict[str, Any]:
    """What the customer received (the outbox, as a channel gateway would deliver it) and what is being watched."""
    cid = s.customer.customer_id
    guard = guardian.Guardian()
    out: dict[str, Any] = {"clock": guard.clock.now_ts(), "simulator": simulator.enabled(), "messages": [],
                           "watches": [], "held": [], "updates_on": True, "orbit": orbit_status()}
    if not cid:
        return out
    out["updates_on"] = not guard.store.opt_out(cid)
    out["messages"] = [{"source": r.get("source") or "review", "message": r["message"], "case_id": r.get("support_case_id"),
                        "clock": r.get("sent_at_clock"), "at": r.get("at")}
                       for r in ActionStore().read(guardian.OUTBOX) if r.get("customer_id") == cid]
    out["held"] = [{"kind": n["kind"], "message": n["message"], "why": n["held_reason"]}
                   for n in guard.store.notices(customer_id=cid, status="held")]
    for w in reversed(guard.store.watches(customer_id=cid)):
        out["watches"].append({**guardian.brief(w, guard.store), "watch_id": w["watch_id"],
                               "events": [{"event": e, "label": guardian.SIM_LABELS[e]}
                                          for e in guardian.available_events(w)]})
    return out


@app.get("/api/session/{session_id}/inbox")
def get_inbox(session_id: str) -> dict[str, Any]:
    return inbox(session(session_id))


def orbit_status() -> dict[str, Any]:
    """Orbit's clock as the page shows it: how often it ticks and how the last tick went."""
    clock: OrbitClock | None = STATE.get("clock")
    if clock is None:
        return {"every_s": None, "last_at": None, "last_error": None}
    last = clock.last or {}
    return {"every_s": clock.every_s, "last_at": last.get("at"), "last_error": last.get("error")}


@app.get("/api/session/{session_id}/clock")
def clock_requests(session_id: str, after: int = 0, after_assistant: int = 0) -> dict[str, Any]:
    """
    The map's background lane: Orbit's system requests (ticks, launches) after seq 'after', and what this customer's
    own AI assistants did through the assistant gateway after line 'after_assistant' of its activity file
    (docs/assistant_lane_plan.md). Only whether this customer got a message is shown, never who else did, and only
    this customer's assistant activity.
    """
    cid = session(session_id).customer.customer_id
    clock: OrbitClock | None = STATE.get("clock")
    requests = []
    for r in clock.since(after) if clock else []:
        mine = sorted({d["source"] for d in r["delivered_to"] if cid and d["customer_id"] == cid})
        requests.append({k: r[k] for k in ("seq", "event", "at", "seconds", "events", "summary", "error")}
                        | {"for_you": mine})
    activity, seen = assistant_activity(cid, after_assistant)
    return {**orbit_status(), "requests": requests, "assistant": activity, "assistant_seq": seen,
            "assistants": connected_assistants(cid)}


def assistant_activity(cid: str | None, after: int) -> tuple[list[dict[str, Any]], int]:
    """This customer's records after line 'after' (seq = line number), and how many lines there are."""
    from assistant_gateway import activity

    rows = ActionStore().read(activity.FILE)
    mine = [{k: v for k, v in r.items() if k != "customer_id"} | {"seq": i}
            for i, r in enumerate(rows, 1) if i > after and cid and r.get("customer_id") == cid]
    return mine, len(rows)


def connected_assistants(cid: str | None) -> list[str]:
    """The names of the customer's connected assistants, most recent first (the map's assistant box)."""
    if not cid:
        return []
    from assistant_gateway.grants import GrantStore

    try:
        grants = sorted(GrantStore().customer_grants(cid), key=lambda g: g["created_at"], reverse=True)
    except Exception:  # noqa: BLE001 - no assistant store: no box
        LOGGER.exception("assistant grants unreadable")
        return []
    return list(dict.fromkeys(g["client_name"] or "AI assistant" for g in grants))


@app.post("/api/session/{session_id}/guardian/advance")
async def advance(session_id: str, body: Advance) -> dict[str, Any]:
    s = session(session_id)
    if not simulator.enabled():
        raise HTTPException(403, "the event simulator is off; start the demo with scripts\\demo.ps1")
    try:
        guardian.Guardian().clock.advance(body.hours)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _tick_then_inbox(s)


@app.post("/api/session/{session_id}/guardian/event")
async def sim_event(session_id: str, body: SimEvent) -> dict[str, Any]:
    s = session(session_id)
    if not simulator.enabled():
        raise HTTPException(403, "the event simulator is off; start the demo with scripts\\demo.ps1")
    try:  # only the signed-in customer's own watches, and only what can happen to them now (checked in code)
        await guardian.simulate_event(guardian.Guardian(), body.watch_id, body.event,
                                      customer_id=s.customer.customer_id or "-", check=False)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _tick_then_inbox(s, check_customer=s.customer.customer_id)


async def _tick_then_inbox(s: DemoSession, check_customer: str | None = None) -> dict[str, Any]:
    """Orbit's clock ticks at once (it checks the watches); a failed tick is shown, and the next tick tries again."""
    tick = await STATE["clock"].tick(check_customer=check_customer)
    return {**inbox(s), "tick_error": tick["error"]}


@app.get("/api/session/{session_id}/companion")
async def get_companion(session_id: str) -> dict[str, Any]:
    """The signed-in customer's companion (P1, read-only): record, consents, derived profile and the digest."""
    s = session(session_id)
    if not s.customer.customer_id:
        raise HTTPException(400, "not signed in")
    return await companion.load_view(s.customer.customer_id, exclude_case_id=s.case_id)


@app.get("/api/session/{session_id}/thread")
async def get_thread(session_id: str, limit: int = 30) -> dict[str, Any]:
    """The customer's continuous thread (P2): earlier conversations and updates, and the opening greeting."""
    s = session(session_id)
    if not s.customer.customer_id:
        return {"thread": [], "greeting": None}
    view = await companion.load_view(s.customer.customer_id, exclude_case_id=s.case_id)
    store = companion.CompanionStore()
    return {"thread": store.thread(s.customer.customer_id, since=view["companion"]["reset_at"],
                                   limit=min(max(limit, 1), 200)),
            "greeting": view["greeting"], "name": view["companion"]["name"]}


@app.post("/api/session/{session_id}/guardian/updates")
def updates(session_id: str, body: Updates) -> dict[str, Any]:
    s = session(session_id)
    if not s.customer.customer_id:
        raise HTTPException(400, "not signed in")
    guardian.Guardian().set_opt_out(s.customer.customer_id, not body.on)
    return inbox(s)


@app.websocket("/api/session/{session_id}/chat")
async def chat(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    s = STATE["sessions"].get(session_id)
    if s is None:
        await ws.close(code=4404)
        return
    timeout = float(STATE["config"].get("turn_timeout_s", 960))
    try:
        while True:
            msg = await ws.receive_json()
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if s.busy:
                await ws.send_json({"kind": "error", "say": "The agents are still answering the last message."})
                continue
            async for event in s.send(text, timeout):
                await ws.send_text(json.dumps(event, default=str))
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------- recordings and replay (video renderer)


@app.get("/api/takes")
def takes(scenario: str | None = None) -> list[dict[str, Any]]:
    return recording.list_takes(scenario)


@app.get("/api/storyboard/{name}")
def storyboard(name: str) -> dict[str, Any]:
    path = (STORYBOARDS / f"{name}.json").resolve()
    if path.parent != STORYBOARDS.resolve() or not path.exists():
        raise HTTPException(404, "no such storyboard")
    return json.loads(path.read_text(encoding="utf-8"))
