"""
Coded tools for the stylist agent (companion plan P6): the customer asks about the picks Personal Stylist Mode sent
them. Everything comes from code: the invites that were really sent (stylist store), the reasons from the stored
score breakdown, and price, colours and sizes from the catalog now. The model never sees scores or other customers,
and nothing can be ordered, reserved or added to a basket here.

    get_stylist_picks   the customer's sent picks (newest first), or why there are none (Stylist Mode off)
    explain_pick        why one pick was chosen, in plain words built from its breakdown
    pick_not_for_me     "not for me": recorded as feedback, so similar launches score lower (a change: needs a
                        sign-in that may change things, like companion memory)
    release_picks       on Orbit's clock tick only (system_events, docs/orbit_tick_plan.md): sends held invites that
                        may go now and drops expired ones; never offered to the model
    launch_new_in       on a new-in launch event only (the product comes from the event's sly_data): scores the
                        opted-in customers and sends the invites they earn (stylist.Stylist.launch); never offered to
                        the model

Except release_picks and launch_new_in, only for a "stylist" issue (intake: the customer asks about a pick or says one is not for them). An answer closes the
issue as information only; the evidence is filed on it.
"""

import logging
from typing import Any

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import stylist as sty
from coded_tools.customer_service.case_store import RESOLVED_INFO_ONLY
from coded_tools.customer_service.case_store import TERMINAL
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.companion_tools import may_not_change
from coded_tools.customer_service.recommendations import CatalogUnavailable
from coded_tools.customer_service.recommendations import load_catalog
from coded_tools.customer_service.system_events import LAUNCH
from coded_tools.customer_service.system_events import TICK
from coded_tools.customer_service.system_events import system_event
from coded_tools.customer_service.tool_support import ToolContext
from coded_tools.customer_service.tool_support import ToolError
from coded_tools.customer_service.tool_support import error
from coded_tools.customer_service.tool_support import tool_context

LOGGER = logging.getLogger(__name__)

STYLIST = ("stylist",)
MAX_PICKS = 3
PICKS_HINT = ("Present only these facts: the product, its price, why it was picked and what is in stock in their "
              "size. Nothing can be ordered, reserved or added to a basket here: point the customer to the product "
              "page. Never mention scores, other customers or anything not listed.")
NO_PICKS_HINT = {
    "stylist_mode_off": "Personal Stylist Mode is off, so no picks were sent. Say they can switch it on in My "
                        "Companion (the app or website) if they would like picks for new launches; do not push it.",
    "none_sent": "No picks have been sent to the customer yet. Say so briefly; do not suggest products here.",
}
def _issue(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    issue = ctx.require_issue(args.get("issue_id"), STYLIST)
    ctx.require_customer()
    return issue


def _close(ctx: ToolContext, issue: dict[str, Any], source: str, data: dict[str, Any]) -> bool:
    ctx.store.add_finding(issue["case_id"], issue["issue_id"], source=source, author="tool", data=data)
    current = ctx.store.get_issue(issue["case_id"], issue["issue_id"])
    if current["status"] in TERMINAL:
        return False
    ctx.store.set_status(issue["case_id"], issue["issue_id"], RESOLVED_INFO_ONLY, by="system",
                         outcome={"outcome": "answered", "source": source})
    return True


def _sent_picks(ctx: ToolContext) -> list[dict[str, Any]]:
    return sty.sent_picks(sty.StylistStore(ctx.store.path), ctx.customer_id)


async def _pick_view(ctx: ToolContext, invite: dict[str, Any], feedback: dict[str, str]) -> dict[str, Any]:
    catalog = await load_catalog(ctx.adapter, ctx.policy)
    return sty.pick_view(sty.StylistStore(ctx.store.path), catalog, invite, feedback)


class GetStylistPicks(CodedTool):
    """The picks Personal Stylist Mode sent this customer, newest first, with why and what is in stock."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = _issue(ctx, args)
            picks = _sent_picks(ctx)[:MAX_PICKS]
            on = cp.CompanionStore(ctx.store.path).consents(
                ctx.customer_id, ctx.policy["companion"])[cp.STYLIST_MODE]["granted"]
            feedback = {f["product_id"]: f["kind"]
                        for f in sty.StylistStore(ctx.store.path).feedback(ctx.customer_id)}
            out: dict[str, Any] = {"issue_id": issue["issue_id"], "stylist_mode": "on" if on else "off",
                                   "picks": [await _pick_view(ctx, p, feedback) for p in picks]}
            out["message_hint"] = PICKS_HINT if out["picks"] else \
                NO_PICKS_HINT["none_sent" if on else "stylist_mode_off"]
            # The prices go on the case too: the gateway's reply check finds every amount the agent may say there.
            out["closed"] = _close(ctx, issue, "get_stylist_picks",
                                   {"picks": [p["product_id"] for p in out["picks"]], "stylist_mode": out["stylist_mode"],
                                    "list_prices": [p["list_price"] for p in out["picks"]]})
            return out
        except (ToolError, CaseError) as e:
            return error(str(e))
        except CatalogUnavailable:
            return error("picks_unavailable: the catalog could not be read. Say the picks cannot be shown right now.")
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("get_stylist_picks failed")
            return error(f"picks_unavailable ({type(e).__name__}). Say the picks cannot be shown right now.")


class ExplainPick(CodedTool):
    """Why one of the customer's picks was chosen, from the stored breakdown (never the score)."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = _issue(ctx, args)
            product_id = str(args.get("product_id") or "").strip().upper()
            invite = next((p for p in _sent_picks(ctx) if p["product_id"] == product_id), None)
            if invite is None:
                raise ToolError(f"{product_id or 'that product'} is not one of the picks sent to this customer; call "
                                "get_stylist_picks to see them")
            view = await _pick_view(ctx, invite, {})
            out = {"issue_id": issue["issue_id"], "product_id": product_id, "title": view["title"],
                   "picked_because": view["picked_because"], "in_stock_now": view["in_stock_now"],
                   "message_hint": "Explain with these reasons only, in plain words. Never mention a score or how it "
                                   "is calculated beyond them. Nothing can be ordered here."}
            out["closed"] = _close(ctx, issue, "explain_pick", {"product_id": product_id})
            return out
        except (ToolError, CaseError) as e:
            return error(str(e))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("explain_pick failed")
            return error(f"picks_unavailable ({type(e).__name__}).")


class PickNotForMe(CodedTool):
    """Records that a pick is not for the customer: similar launches will score lower for them."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        try:
            ctx = await tool_context(sly_data)
            issue = _issue(ctx, args)
            why = may_not_change(ctx)
            if why:
                _close(ctx, issue, "pick_not_for_me", {"outcome": "not_allowed", "why": why})
                return {"issue_id": issue["issue_id"], "status": "not_allowed", "closed": True,
                        "message_hint": f"Nothing was recorded: {why}. Tell the customer they can say so in the "
                                        "ACME Retail app or website."}
            product_id = str(args.get("product_id") or "").strip().upper()
            if not any(p["product_id"] == product_id for p in _sent_picks(ctx)):
                raise ToolError(f"{product_id or 'that product'} is not one of the picks sent to this customer; call "
                                "get_stylist_picks to see them")
            stylist = sty.Stylist(cases=ctx.store, adapter=ctx.adapter, policy=ctx.policy)
            stylist.feedback(ctx.customer_id, product_id, "not_for_me")
            closed = _close(ctx, issue, "pick_not_for_me", {"outcome": "recorded", "product_id": product_id})
            return {"issue_id": issue["issue_id"], "status": "recorded", "closed": closed,
                    "message_hint": "Thank them. Similar pieces will be picked less often for them; they can switch "
                                    "Personal Stylist Mode off at any time in My Companion. Do not argue or suggest "
                                    "other products here."}
        except (ToolError, CaseError, ValueError) as e:
            return error(f"{e}. Nothing was recorded.")
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("pick_not_for_me failed")
            return error(f"feedback_unavailable ({type(e).__name__}). Nothing was recorded.")


class ReleasePicks(CodedTool):
    """Held invites (quiet hours, daily limit, service updates first) that may go now; on Orbit's clock tick only."""

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        if system_event(sly_data) != TICK:
            return error("held picks are released only on Orbit's clock tick")
        try:
            return await sty.Stylist().run_once()
        except Exception as e:  # noqa: BLE001 - the next tick tries again
            LOGGER.exception("release_picks failed")
            return error(f"picks_unavailable ({type(e).__name__}); the next tick tries again")


class LaunchNewIn(CodedTool):
    """
    A new-in launch, on Orbit's launch event only. The product comes from the event's sly_data. Returns counts only:
    who was invited stays in the stylist store (the Launch panel reads it there).
    """

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> dict[str, Any] | str:
        if system_event(sly_data) != LAUNCH:
            return error("a launch runs only on Orbit's launch event")
        product_id = str(sly_data.get("product_id") or "").strip().upper()
        try:
            out = await sty.Stylist().launch(product_id, by=str(sly_data.get("launched_by") or "launch event"))
        except ValueError as e:
            return error(f"not_launched: {e}")
        except CatalogUnavailable:
            return error("stylist_data_unavailable: the catalog could not be read. Nothing was launched.")
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("launch_new_in failed")
            return error(f"stylist_data_unavailable ({type(e).__name__}). Nothing was launched.")
        return {"product_id": out["product_id"], "title": out.get("title"),
                "already_launched": bool(out.get("already_launched")), "candidates": out.get("candidates"),
                "scored": out.get("scored"), "below_threshold": out.get("below_threshold"),
                "gated": out.get("gated"), "invited": len(out.get("invited") or []),
                "data_unavailable": len(out.get("data_unavailable") or [])}
