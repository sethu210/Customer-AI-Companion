r"""
Everly companion plan P9: journey evaluations, J1-J10 of docs/companion_plan.md §7.

A journey is several conversations and business events for one customer across simulated days: conversations go
through the agent server exactly as the website sends them (DemoSession; finish_turn records the thread and starts
the Guardian's watches), or through the assistant gateway (J10); between them the simulator's clock moves, business
events happen, the Guardian and the Stylist run and products are launched, all as requests to Orbit through the same
agent server (ticks and launch events, docs/orbit_tick_plan.md; no model call), and the customer uses "My Companion"
(the same functions as the demo console's buttons). Each journey has fresh stores and its own agent server.
Scored from the records (stores, outbox, thread, scores), not from the reply text, except where the check is about
what the customer was told (facts, no questions asked again, nothing promised).

Every journey is also checked for the rules that hold everywhere ("invariants"):
    invites      every invite has a stored score >= stylist.min_affinity with an owned item, and no gate
    limits       per customer and simulated day at most guardian.max_updates_per_day messages (service updates and
                 invites together), none sent in quiet hours, at most stylist.max_invites_per_week invites in 7 days
    promises     no sent message or reply promises a time or an outcome or uses a forbidden phrase
    memory       remembered preferences only where the journey asked for them (and only after the customer's yes)

    python -m evaluation.journey_eval --label p9-scripted [--ids J1,J4] [--live]
Scripted model and mock data by default (no quota); --live uses the configured model and backend.
Output: runtime/eval/<label>/journeys.json and journeys.md (steps per journey in stores/<id>/steps.json).
"""

import argparse
import asyncio
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from typing import Awaitable
from typing import Callable

from dotenv import load_dotenv

from evaluation.expectations import GLOBAL_FORBIDDEN
from evaluation.expectations import ROOT

SCARF, LOAFERS, POLO = "PRD-CSC", "PRD-SLF", "PRD-MPL"
FAN = "CUST-B0196"       # keeps a navy wool overcoat: the cashmere scarf scores 92
FULL = "CUST-B0025"      # full profile: scarf 90 and suede loafers 90 (shared classic/smart styles)
RETURNER = "CUST-B0091"  # scarf 90 (black wool overcoat); chino pants still inside the return window
ASKS_AGAIN = re.compile(r"\b(which (order|item|one)|order (number|id)|could you (tell|give|share|confirm)|can you "
                        r"(tell|give|share|confirm)|please (provide|confirm|share|tell))\b", re.I)
BASKET = ("basket", "reserved for you", "i've ordered", "i have ordered", "added to your cart")
# A time or an outcome promised (the Guardian eval's pattern, but "within" only with a time: the stylist's reasons
# say a price is "within what you usually spend").
PROMISES = re.compile(r"\b(within (\d+|a|an|one|two|three|a few|the next) ?\w* (hours?|days?|weeks?)|by (tomorrow|"
                      r"monday|friday)|guarantee|will (arrive|be refunded|be delivered|be completed)|business days)\b",
                      re.I)


@dataclass
class Journey:
    id: str
    title: str
    customer: str
    play: Callable[["Ctx"], Awaitable[dict[str, bool]]]
    memory: bool = False  # the journey asks for preferences to be remembered


class Ctx:
    """One journey's world: its stores, clock and customer, and a log of every step."""

    def __init__(self, journey: Journey, timeout_s: float, live: bool) -> None:
        from coded_tools.customer_service.policy import load_policy
        from coded_tools.customer_service.simulator import Simulator

        self.j, self.customer, self.timeout_s, self.live = journey, journey.customer, timeout_s, live
        self.policy = load_policy()
        self.clock = Simulator()
        self.log: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self.calls: list[int] = []
        self.facts: dict[str, Any] = {}

    def step(self, what: str, **detail: Any) -> None:
        self.log.append({"clock": self.clock.now_ts(), "step": what, **detail})

    # ---------------------------------------------------------------- conversations

    async def session(self, customer: str | None = None) -> Any:
        """A signed-in website session (registered with the demo console, so "My Companion" works for it)."""
        from demo_console import app as console
        from demo_console.catalog import Catalog
        from evaluation.agent_server import SERVER_PORT
        from demo_console.session import DemoSession

        cid = customer or self.customer
        catalog = await Catalog.aload()
        s = DemoSession(catalog.get(cid), catalog.gateway_sly_data(cid), f"http://localhost:{SERVER_PORT}")
        console.STATE["sessions"][s.id] = s
        return s

    async def chat(self, messages: list[str], session: Any = None) -> dict[str, Any]:
        """One conversation (a new one unless `session` continues it): replies, agents used, model calls per turn."""
        s = session or await self.session()
        turns = []
        for text in messages:
            end, agents_used = {}, set()
            async for event in s.send(text, self.timeout_s):
                if event["kind"] in ("delegate", "tool_call"):
                    agents_used.add(event["to"])
                if event["kind"] == "turn_end":
                    end = event
            calls = (end.get("cost") or {}).get("llm_calls") or 0
            turns.append({"q": text, "a": end.get("answer") or "", "error": end.get("error"), "llm_calls": calls,
                          "agents": sorted(agents_used)})
            self.answers.append(end.get("answer") or "")
            self.calls.append(calls)
        self.step("chat", case_id=s.case_id, turns=list(turns))
        return {"session": s, "case_id": s.case_id, "turns": turns,
                "error": next((t["error"] for t in turns if t["error"]), None)}

    async def confirm_if_asked(self, convo: dict[str, Any]) -> dict[str, Any]:
        """The customer says yes when the case waits for their confirmation (an action or a preference)."""
        from coded_tools.customer_service.case_store import CaseStore

        if not convo["case_id"]:
            return convo
        view = CaseStore().case_view(convo["case_id"])
        if any(i["status"] in ("proposed", "awaiting_customer") for i in view["issues"]):
            more = await self.chat(["Yes, please."], session=convo["session"])
            convo["turns"] += more["turns"]
        return convo

    async def view(self, session: Any) -> dict[str, Any]:
        """What "My Companion" shows the customer (the demo console's own view)."""
        from demo_console import my_companion

        return await my_companion.view(session.id)

    async def companion(self, exclude_case_id: str | None = None) -> dict[str, Any]:
        """{companion, profile, digest, greeting} as the gateway and the front agent's notes see them."""
        from coded_tools.customer_service import companion as cp

        return await cp.load_view(self.customer, exclude_case_id=exclude_case_id)

    # ---------------------------------------------------------------- time, events, senders

    async def advance(self, hours: float) -> None:
        """Move the clock; Orbit's tick has the Guardian and the Stylist do their regular run, as in the demo."""
        self.clock.advance(hours)
        await self.run_senders()

    async def run_senders(self) -> dict[str, Any]:
        from evaluation.orbit_client import tick

        out = await tick()  # Orbit -> guardian (run_follow_ups), Orbit -> stylist (release_picks); no model call
        self.step("senders run", **out)
        return out

    async def event(self, name: str) -> dict[str, Any]:
        """A business event on the customer's watch that accepts it (refund completed, return received, ...)."""
        from coded_tools.customer_service import guardian as g
        from evaluation.orbit_client import tick

        guard = g.Guardian()
        watch = next((w for w in guard.store.watches(customer_id=self.customer) if name in g.available_events(w)),
                     None)
        if watch is None:
            raise RuntimeError(f"no watch for {self.customer} accepts the event {name}")
        await g.simulate_event(guard, watch["watch_id"], name, actor="journey eval (simulated)", check=False)
        summary = await tick(check_customer=self.customer)  # Orbit's tick checks that customer at once
        self.step("event", event=name, watch_id=watch["watch_id"], summary=summary)
        return summary

    def consent(self, name: str, granted: bool, customer: str | None = None) -> None:
        from coded_tools.customer_service import companion as cp

        cp.CompanionStore().set_consent(customer or self.customer, name, granted, channel="web")
        self.step("consent", consent=name, granted=granted, customer=customer or self.customer)

    async def launch(self, product_id: str) -> dict[str, Any]:
        """A new-in launch as the Launch panel sends it: an event to Orbit, which has Aura launch it (counts only)."""
        from evaluation.orbit_client import launch

        summary = await launch(product_id, launched_by="journey eval")
        self.step("launch", product_id=product_id, candidates=summary["candidates"], invited=summary["invited"],
                  gated=summary["gated"], via=summary["via"])
        return summary

    # ---------------------------------------------------------------- records

    def score_of(self, product_id: str, customer: str | None = None) -> dict[str, Any] | None:
        from coded_tools.customer_service.stylist import StylistStore

        return next((r for r in StylistStore().scores(product_id=product_id)
                     if r["customer_id"] == (customer or self.customer)), None)

    def invites(self, product_id: str | None = None) -> list[dict[str, Any]]:
        from coded_tools.customer_service.stylist import StylistStore

        return [i for i in StylistStore().invites(customer_id=self.customer)
                if product_id is None or i["product_id"] == product_id]

    def outbox(self, source: str | None = None) -> list[dict[str, Any]]:
        from coded_tools.customer_service.action_store import ActionStore
        from coded_tools.customer_service.guardian import OUTBOX

        return [r for r in ActionStore().read(OUTBOX) if r.get("customer_id") == self.customer
                and (source is None or r.get("source") == source)]

    def thread(self) -> list[dict[str, Any]]:
        from coded_tools.customer_service import companion as cp

        return cp.CompanionStore().thread(self.customer, since=None, limit=None)

    def memories(self, status: str = "active") -> list[dict[str, Any]]:
        from coded_tools.customer_service import companion as cp

        return cp.CompanionStore().memories(self.customer, status=status)


# ---------------------------------------------------------------------------- the journeys


async def j1(ctx: Ctx) -> dict[str, bool]:
    """Day 1 refund question -> day 3 the Guardian's "refund completed" -> day 4 a new chat."""
    day1 = await ctx.chat(["Where's my refund for the blue jacket?"])
    await ctx.advance(14)          # day 2, 08:00
    await ctx.advance(24)          # day 3, 08:00
    await ctx.event("refund_completed")
    sent = ctx.outbox("guardian")
    await ctx.advance(24)          # day 4, 08:00
    before = await ctx.companion()
    day4 = await ctx.chat(["Hi, did my refund for the blue jacket come through?"])
    update = sent[0]["message"] if sent else "<no update sent>"
    thread = ctx.thread()
    roles = [r["role"] for r in thread]
    cases = [r["case_id"] for r in thread if r["role"] != "update"]
    ctx.facts.update(update=update, greeting=before["greeting"], digest_chars=len(before["digest"]),
                     llm_calls_day1=day1["turns"][0]["llm_calls"], llm_calls_day4=day4["turns"][0]["llm_calls"],
                     thread_roles=roles)
    return {
        "update_sent": len(sent) == 1 and "refund" in update.lower(),
        "greets_with_update": update in before["greeting"] and before["greeting"].startswith("Welcome back"),
        "notes_have_update": update[:60] in before["digest"],
        "no_repeated_questions": not ASKS_AGAIN.search(day4["turns"][0]["a"]),
        "thread_continuous": roles == ["customer", "companion", "update", "customer", "companion"]
        and cases[:2] == [day1["case_id"]] * 2 and cases[2:] == [day4["case_id"]] * 2
        and day1["case_id"] != day4["case_id"],
        "conversations_ok": not day1["error"] and not day4["error"],
    }


async def j2(ctx: Ctx) -> dict[str, bool]:
    """ "I prefer earth tones, remember that" -> yes -> later "what goes with my chinos?" """
    from coded_tools.customer_service.case_store import CaseStore

    convo = await ctx.chat(["I prefer earth tones, please remember that."])
    stored_before_yes = len(ctx.memories())
    await ctx.chat(["Yes, please."], session=convo["session"])
    remembered = [(m["kind"], m["value"]) for m in ctx.memories()]
    later = await ctx.chat(["What goes with my chinos?"])
    findings = [f["data"] for i in CaseStore().case_view(later["case_id"])["issues"]
                for f in i.get("findings") or [] if f.get("source") == "get_recommendations"]
    data = findings[-1] if findings else {}
    recs = data.get("recommendations") or []
    liked = [bool(r.get("in_colours_you_like")) for r in recs]
    know = (await ctx.view(later["session"]))["profile"]["preferences"]
    ctx.facts.update(remembered=remembered, applied=data.get("stated_preferences_applied"),
                     suggestions=[(r.get("title"), r.get("in_colours_you_like")) for r in recs])
    return {
        "stored_only_after_yes": stored_before_yes == 0 and remembered == [("likes", "earth tones")],
        "suggestions_use_it": bool(recs) and bool(data.get("stated_preferences_applied"))
        and liked == sorted(liked, reverse=True),
        "shown_in_what_i_know": [(p["kind"], p["value"]) for p in know] == [("likes", "earth tones")],
        "conversations_ok": not convo["error"] and not later["error"],
    }


async def j3(ctx: Ctx) -> dict[str, bool]:
    """Forget one preference in chat; then add one in "My Companion" and reset the companion."""
    from demo_console import my_companion

    convo = await ctx.chat(["Remember that I avoid wool."])
    await ctx.chat(["Yes."], session=convo["session"])
    had = [m["value"] for m in ctx.memories()]
    forget = await ctx.chat(["Please forget that I avoid wool."])
    after_forget = await ctx.companion()
    know = (await ctx.view(forget["session"]))["profile"]["preferences"]
    s = forget["session"]
    await my_companion.add_preference(s.id, my_companion.Preference(kind="likes", value="navy"))
    added = [m["value"] for m in ctx.memories()]
    recall_before = len((await ctx.companion())["profile"]["recall"])
    await my_companion.reset_companion(s.id)
    ctx.step("my companion", actions=["add preference likes navy", "reset"])
    after_reset = await ctx.companion()
    since = after_reset["companion"]["reset_at"]
    from coded_tools.customer_service import companion as cp

    ctx.facts.update(had=had, added=added, recall_before_reset=recall_before, greeting=after_reset["greeting"])
    return {
        "remembered_then_forgotten": had == ["wool"] and "wool" not in after_forget["digest"] and know == [],
        "added_in_my_companion": added == ["navy"],
        "reset_forgets_memory": ctx.memories() == [] and "navy" not in after_reset["digest"],
        "reset_forgets_recall": recall_before > 0 and after_reset["profile"]["recall"] == []
        and cp.CompanionStore().thread(ctx.customer, since=since) == [],
        "fresh_greeting": after_reset["greeting"].startswith("Hi") and "Welcome back" not in after_reset["greeting"],
        "conversations_ok": not convo["error"] and not forget["error"],
    }


async def j4(ctx: Ctx) -> dict[str, bool]:
    """Stylist Mode on (the customer and a cohort), a launch with a strong match."""
    from coded_tools.customer_service.mock_adapter import MockAdapter

    cohort = sorted(MockAdapter.from_env().customers)  # every test customer (the same ids on Databricks)
    cohort = [c for c in (cohort if not ctx.live else cohort[:20]) if c != ctx.customer]  # live: fewer reads
    for cid in cohort:
        _quiet_consent(cid)
    ctx.consent("stylist_mode", True)
    ctx.step("consent", consent="stylist_mode", granted=True, cohort=len(cohort))
    summary = await ctx.launch(SCARF)
    result = ctx.score_of(SCARF) or {}
    sent = ctx.outbox("stylist")
    owned = (result.get("breakdown") or {}).get("owned_item") or "<none>"
    ctx.facts.update(score=result.get("score"), owned_item=owned, cohort=len(cohort), invited=summary["invited"],
                     message=sent[0]["message"] if sent else None)
    return {
        "invited_at_80_or_more": result.get("outcome") == "invite" and result.get("score", 0) >= 80,
        "names_the_owned_item": len(sent) == 1 and owned in sent[0]["message"],
        "cohort_scored": summary["candidates"] == len(cohort) + 1,
    }


def _quiet_consent(customer: str) -> None:
    """Opt-in for a cohort customer, without a log line each."""
    from coded_tools.customer_service import companion as cp

    cp.CompanionStore().set_consent(customer, cp.STYLIST_MODE, True, channel="web")


async def j5(ctx: Ctx) -> dict[str, bool]:
    """No Stylist Mode, or switched off again: a perfect match gets nothing."""
    first = await ctx.launch(SCARF)
    ctx.consent("stylist_mode", True)
    ctx.consent("stylist_mode", False)  # switched off again: the latest choice counts
    second = await ctx.launch(POLO)
    return {
        "not_scored": first["candidates"] == 0 and second["candidates"] == 0
        and ctx.score_of(SCARF) is None and ctx.score_of(POLO) is None,
        "nothing_sent": ctx.invites() == [] and ctx.outbox("stylist") == [],
    }


async def j6(ctx: Ctx) -> dict[str, bool]:
    """An open request with a person, then a launch: service before selling."""
    from coded_tools.customer_service.case_store import CaseStore

    ctx.consent("stylist_mode", True)
    convo = await ctx.chat(["I'd like to talk to a person about my overcoat, please."])
    handoffs = [h for h in CaseStore().handoffs(status="open") if h["customer_id"] == ctx.customer]
    await ctx.launch(SCARF)
    result = ctx.score_of(SCARF) or {}
    gates = (result.get("breakdown") or {}).get("gates") or []
    ctx.facts.update(handoffs=len(handoffs), outcome=result.get("outcome"), gates=gates)
    return {
        "with_a_person": bool(handoffs) and not convo["error"],
        "gated_open_trouble": result.get("outcome") == "gated" and "open_trouble" in gates,
        "no_invite": ctx.invites() == [] and ctx.outbox("stylist") == [],
    }


async def j7(ctx: Ctx) -> dict[str, bool]:
    """A service update and an invite on the same day, a budget of 2: service first, the invite waits."""
    ctx.consent("stylist_mode", True)
    convo = await ctx.confirm_if_asked(await ctx.chat(["I'd like to return my chino pants."]))
    await ctx.advance(14)  # day 2, 08:00: the Guardian has classified the new watch
    await ctx.event("return_received")
    service = ctx.outbox("guardian")
    await ctx.launch(SCARF)  # the same morning
    (invite,) = ctx.invites(SCARF) or [{}]
    held = {k: invite.get(k) for k in ("status", "held_reason")}
    await ctx.advance(24)  # day 3, 08:00
    (invite,) = ctx.invites(SCARF) or [{}]
    order = [r.get("source") for r in ctx.outbox()]
    service_day = (service[0].get("sent_at_clock") or "")[:10] if service else "9999"
    ctx.facts.update(service=[r["message"][:80] for r in service], invite_at_launch=held,
                     invite_after=invite.get("status"), sources_in_order=order)
    return {
        "service_update_sent": len(service) == 1 and not convo["error"],
        "invite_waited": held["status"] == "held" and held["held_reason"] in ("daily_limit", "service_first"),
        "service_first": order[:1] == ["guardian"] and "stylist" in order,
        "invite_sent_next_day": invite.get("status") == "sent" and (invite.get("sent_at") or "")[:10] > service_day,
    }


async def j8(ctx: Ctx) -> dict[str, bool]:
    """ "Not for me" on a pick, then a similar launch: a lower score, no invite."""
    ctx.consent("stylist_mode", True)
    await ctx.launch(SCARF)
    first = ctx.invites(SCARF)
    convo = await ctx.chat(["Thanks for the scarf you sent me, but it's not for me."])
    from coded_tools.customer_service.stylist import StylistStore

    feedback = [f["kind"] for f in StylistStore().feedback(ctx.customer)]
    await ctx.advance(24 * 8)  # past the weekly limit
    await ctx.launch(LOAFERS)
    result = ctx.score_of(LOAFERS) or {}
    b = result.get("breakdown") or {}
    ctx.facts.update(feedback=feedback, loafers=result.get("score"), penalty=b.get("penalty"))
    return {
        "first_invited": len(first) == 1 and first[0]["status"] == "sent",
        "feedback_recorded": feedback == ["not_for_me"] and not convo["error"],
        "lower_score": bool(b.get("penalty")) and result.get("score") is not None
        and result["score"] + b["penalty"] >= 80 > result["score"],
        "not_invited": ctx.invites(LOAFERS) == [],
    }


async def j9(ctx: Ctx) -> dict[str, bool]:
    """The customer asks about the pick in chat: the stylist answers from the invite; nothing is ordered."""
    from coded_tools.customer_service.action_store import ActionStore
    from coded_tools.customer_service.data_adapter import get_adapter
    from coded_tools.customer_service.recommendations import load_catalog

    ctx.consent("stylist_mode", True)
    await ctx.launch(SCARF)
    before = len(ActionStore().actions())
    convo = await ctx.chat(["Tell me about the scarf you sent me."])
    answer = convo["turns"][0]["a"]
    catalog = await load_catalog(get_adapter(), ctx.policy)
    named = sorted({p["title"] for p in catalog.products.values() if p["title"] and p["title"].lower() in answer.lower()})
    owned = await get_adapter().fetch("get_purchase_history", ctx.customer, policy=ctx.policy)
    # what was picked, and what it was picked to go with (the owned item named in the reasons)
    picked = {catalog.products[i["product_id"]]["title"] for i in ctx.invites()} | \
        {r.get("title") for r in owned.rows}
    ctx.facts.update(agents=convo["turns"][0]["agents"], named=named)
    return {
        "routed_to_stylist": "stylist" in convo["turns"][0]["agents"],
        "facts_from_invite": "Cashmere Scarf" in answer and "87.99" in answer and set(named) <= picked,
        "nothing_ordered": len(ActionStore().actions()) == before
        and not any(w in answer.lower() for w in BASKET),
        "conversation_ok": not convo["error"],
    }


async def j10(ctx: Ctx) -> dict[str, bool]:
    """The same customer through their own AI assistant (MCP gateway): the same picks and thread, read-only."""
    from assistant_gateway.grants import GrantStore
    from assistant_gateway.oauth import GRANTABLE_SCOPES
    from assistant_gateway.oauth import resolve_customer
    from assistant_gateway.server import Gateway
    from evaluation.agent_server import SERVER_PORT
    from coded_tools.customer_service.stylist import StylistStore

    ctx.consent("stylist_mode", True)
    await ctx.launch(SCARF)
    web = await ctx.chat(["Tell me about the scarf you sent me."])
    customer_id, auth_level = await resolve_customer(ctx.customer, ctx.policy)
    store = GrantStore()
    token, _ = store.issue(customer_id=customer_id, client_id="journey-eval", client_name="journey eval",
                           auth_level=auth_level, scopes=GRANTABLE_SCOPES, ttl_s=3600)
    grant = store.verify(token)
    gateway = Gateway(store, ctx.policy, agent_url=f"http://localhost:{SERVER_PORT}")
    replies, case_id, case_ids = [], None, set()
    for message in ("Tell me about the scarf you sent me.", "It's not for me."):
        started = await gateway.ask_support(grant, message, case_id)
        await asyncio.gather(*list(gateway.tasks))
        answer = await gateway.get_answer(grant, started["request_id"])
        replies.append(answer)
        case_id = (answer.get("case") or {}).get("case_id") or case_id
        case_ids.add(case_id)
    from coded_tools.customer_service.case_store import CaseStore

    # "not for me" was tried and refused in code (a change), not merely left out
    refused = [f["data"] for c in case_ids if c for i in CaseStore().case_view(c)["issues"]
               for f in i.get("findings") or [] if f.get("source") == "pick_not_for_me"]
    ctx.answers += [r.get("reply") or "" for r in replies]
    ctx.step("assistant", replies=[{k: r.get(k) for k in ("status", "reply")} for r in replies])
    cases = await gateway.get_case_status(grant)
    thread = ctx.thread()
    channels = [r["channel"] for r in thread if r["role"] == "customer"]
    tools = {t for s in ctx.policy["assistant"]["scopes"].values() for t in s}
    ctx.facts.update(channels=channels, assistant_tools=sorted(tools), not_for_me=refused)
    return {
        "same_picks": all(r.get("status") == "done" for r in replies) and "Cashmere Scarf" in replies[0]["reply"]
        and not web["error"],
        "read_only": StylistStore().feedback(ctx.customer) == [] and bool(refused)
        and all(isinstance(r, dict) and r.get("outcome") == "not_allowed" for r in refused),
        "same_thread": channels == ["web_session", "assistant", "assistant"],
        "case_visible": bool(cases.get("found")),
    }


JOURNEYS = [
    Journey("J1", "refund question, Guardian update, new chat on day 4", "CUST-S01", j1),
    Journey("J2", "remember earth tones, then suggestions", "CUST-S11", j2, memory=True),
    Journey("J3", "forget a preference; add one and reset in My Companion", "CUST-S02", j3, memory=True),
    Journey("J4", "Stylist Mode on, launch with a strong match", FAN, j4),
    Journey("J5", "no Stylist Mode, perfect match: nothing sent", FAN, j5),
    Journey("J6", "a request with a person, then a launch", FAN, j6),
    Journey("J7", "service update and invite on the same day", RETURNER, j7),
    Journey("J8", "not for me, then a similar launch", FULL, j8),
    Journey("J9", "asks about the pick in chat", FAN, j9),
    Journey("J10", "the same customer via their own AI assistant", FAN, j10),
]


# ---------------------------------------------------------------------------- the rules that hold everywhere


def invariants(ctx: Ctx) -> dict[str, bool]:
    import sqlite3

    from coded_tools.customer_service.case_store import CaseStore
    from coded_tools.customer_service.messaging import quiet
    from coded_tools.customer_service.simulator import parse_ts
    from coded_tools.customer_service.stylist import StylistStore

    sstore, settings = StylistStore(), ctx.policy["stylist"]
    scores = {(r["customer_id"], r["product_id"]): r for r in sstore.scores()}
    invites = sstore.invites()
    bad_invites = [i["invite_id"] for i in invites
                   if (s := scores.get((i["customer_id"], i["product_id"]))) is None or s["outcome"] != "invite"
                   or (s["score"] or 0) < int(settings["min_affinity"]) or not (s["breakdown"] or {}).get("owned_item")]
    sent: list[tuple[str, str]] = []  # (customer, simulated time) of every message sent
    with sqlite3.connect(CaseStore().path) as db:
        for table in ("guardian_notices", "stylist_invites"):
            try:
                sent += db.execute(f"SELECT customer_id, sent_at FROM {table} WHERE status = 'sent'").fetchall()
            except sqlite3.OperationalError:
                pass
    per_day = Counter((c, at[:10]) for c, at in sent)
    in_quiet = [at for _, at in sent if quiet(parse_ts(at), ctx.policy["guardian"])]
    week_heavy = []
    sent_invites = sorted((i["customer_id"], parse_ts(i["sent_at"])) for i in invites if i["status"] == "sent")
    for n, (cid, at) in enumerate(sent_invites):
        window = [x for c, x in sent_invites[n:] if c == cid and x - at < timedelta(days=7)]
        if len(window) > int(settings["max_invites_per_week"]):
            week_heavy.append(cid)
    messages = [r["message"] for r in ctx.outbox()] + ctx.answers
    promised = [m[:120] for m in messages if PROMISES.search(m) or any(p.lower() in m.lower() for p in GLOBAL_FORBIDDEN)]
    from coded_tools.customer_service import companion as cp

    remembered = cp.CompanionStore().memories(ctx.customer)
    ctx.facts.update(invariant_detail={"bad_invites": bad_invites, "max_per_day": max(per_day.values(), default=0),
                                       "sent_in_quiet_hours": len(in_quiet), "over_weekly": week_heavy,
                                       "promises": promised})
    return {
        "invites": not bad_invites,
        "limits": max(per_day.values(), default=0) <= int(ctx.policy["guardian"]["max_updates_per_day"])
        and not in_quiet and not week_heavy,
        "promises": not promised,
        "memory": ctx.j.memory or not remembered,
    }


# ---------------------------------------------------------------------------- run


def _fresh_caches() -> None:
    """Profile, style and catalog caches belong to the previous journey's stores and clock."""
    from coded_tools.customer_service import companion as cp
    from coded_tools.customer_service import recommendations

    cp._DATA_CACHE.clear()
    cp._STYLE_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()


def run_journey(journey: Journey, out_dir: Any, timeout_s: float, live: bool) -> dict[str, Any]:
    from evaluation.agent_server import AgentServer

    store_dir = out_dir / "stores" / journey.id
    store_dir.mkdir(parents=True, exist_ok=True)
    for name in ("simulator.json", "cases.db", "assistant.db"):
        (store_dir / name).unlink(missing_ok=True)
    os.environ["SIMULATOR_STATE"] = str(store_dir / "simulator.json")
    os.environ["ASSISTANT_STORE_PATH"] = str(store_dir / "assistant.db")
    _fresh_caches()
    t0 = time.monotonic()
    with AgentServer(store_dir, scripted=not live):
        ctx = Ctx(journey, timeout_s, live)
        try:
            checks = asyncio.run(journey.play(ctx))
            error = None
        except Exception as e:  # noqa: BLE001 - a journey that cannot finish is a failed journey
            checks, error = {}, f"{type(e).__name__}: {e}"[:300]
        rules = invariants(ctx)
    (store_dir / "steps.json").write_text(json.dumps(ctx.log, indent=2, default=str), encoding="utf-8")
    row = {"id": journey.id, "title": journey.title, "customer": journey.customer, "checks": checks,
           "invariants": rules, "facts": ctx.facts, "error": error, "llm_calls": ctx.calls,
           "seconds": round(time.monotonic() - t0, 1)}
    row["ok"] = bool(checks) and all(checks.values()) and all(rules.values()) and not error
    return row


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(prog="journey_eval")
    parser.add_argument("--label", required=True)
    parser.add_argument("--ids", default=",".join(j.id for j in JOURNEYS))
    parser.add_argument("--live", action="store_true", help="configured model and backend instead of scripted+mock")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    out_dir = ROOT / "runtime" / "eval" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["EVENT_SIMULATOR"] = "1"
    if not args.live:
        os.environ["CUSTOMER_DATA_BACKEND"] = "mock"
    from evaluation.orbit_client import ensure_token

    ensure_token()  # Orbit's ticks and launches: the same secret for the agent server and this process

    wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
    rows = []
    for journey in [j for j in JOURNEYS if j.id in wanted]:
        row = run_journey(journey, out_dir, args.timeout, args.live)
        rows.append(row)
        failed = [k for k, v in {**row["checks"], **row["invariants"]}.items() if not v]
        print(f"{journey.id}: {'PASS' if row['ok'] else 'FAIL'} {', '.join(failed)} {row['error'] or ''}", flush=True)

    calls = [c for r in rows for c in r["llm_calls"]]
    summary = {"label": args.label, "model": "live" if args.live else "scripted", "journeys": len(rows),
               "passed": sum(r["ok"] for r in rows),
               "invariants_broken": sum(not v for r in rows for v in r["invariants"].values()),
               "turns": len(calls), "avg_llm_calls_per_turn": round(sum(calls) / len(calls), 2) if calls else None}
    (out_dir / "journeys.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str),
                                           encoding="utf-8")
    lines = [f"# Companion journeys ({summary['model']}): {summary['passed']}/{summary['journeys']} pass, "
             f"{summary['invariants_broken']} invariant breaks, {summary['avg_llm_calls_per_turn']} model calls per "
             f"turn ({summary['turns']} turns)", "",
             "| Id | Journey | Checks | Invariants | Model calls per turn | s |", "|---|---|---|---|---|---|"]
    for r in rows:
        failed = [k for k, v in r["checks"].items() if not v]
        broken = [k for k, v in r["invariants"].items() if not v]
        checks = "ok" if r["checks"] and not failed else "FAIL: " + ", ".join(failed or [r["error"] or "no checks"])
        lines.append(f"| {r['id']} | {r['title']} | {checks} | {'ok' if not broken else 'BROKEN: ' + ', '.join(broken)}"
                     f" | {', '.join(str(c) for c in r['llm_calls']) or '-'} | {r['seconds']} |")
    (out_dir / "journeys.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if summary["passed"] == summary["journeys"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
