"""
A demo session: one simulated website sign-in and one conversation with the agents.

The session holds what a real channel gateway holds for a visitor: the sly_data it sends (raw website identity, built
here from the catalog, never from the browser), the case_id the network returns (allow.to_upstream) and the chat
context. Each turn streams normalised events (demo_console.stream) and, after any tool that changes the case, a
"case" event with the case as the case store holds it. When a take is attached, every event is also recorded.
"""

import secrets
import time
from typing import Any
from typing import AsyncIterator

from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.gateway import finish_turn
from demo_console import agents
from demo_console.catalog import Customer
from demo_console.recording import Take
from demo_console.stream import Normaliser
from demo_console.stream import stream_turn

CASE_TOOLS = {"open_case", "add_issues", "record_findings", "update_issue", "submit_action", "handoff_to_human",
              "case_state", "get_recommendations"}
CARD_FIELDS = ("product_id", "title", "category", "description", "reason", "because", "list_price", "currency",
               "in_your_usual_size")


def suggestion_events(view: dict[str, Any] | None, shown: set[str]) -> list[dict[str, Any]]:
    """
    Product cards from the case's evidence: what the recommendation engine decided (get_recommendations findings),
    never the model's wording. Each finding is shown once (its key goes into `shown`). No SKUs or stock numbers.
    """
    events = []
    for issue in (view or {}).get("issues") or []:
        found = [f for f in issue.get("findings") or [] if f.get("source") == "get_recommendations"]
        for n, finding in enumerate(found):
            key = f"{issue['issue_id']}#{n}"
            data = finding.get("data")
            if key in shown or not isinstance(data, dict):
                continue
            shown.add(key)
            items = [{**{k: r.get(k) for k in CARD_FIELDS},
                      "options": [{"color": o.get("color"), "size": o.get("size")} for o in r.get("available") or []]}
                     for r in data.get("recommendations") or []]
            why = str(data.get("reason") or data.get("status")).replace("_", " ")
            say = f"Recommendation engine: {len(items)} suggestion(s)" if items else f"Recommendation engine: none ({why})"
            events.append({"id": 0, "kind": "suggestions", "issue_id": issue["issue_id"], "status": data.get("status"),
                           "reason": data.get("reason"), "items": items, "say": say,
                           "tech": "get_recommendations evidence on the case"})
    return events


class DemoSession:
    def __init__(self, customer: Customer, sly_data: dict[str, Any], base_url: str) -> None:
        self.id = secrets.token_hex(8)
        self.customer = customer
        self.gateway_sly = dict(sly_data)
        self.base_url = base_url
        self.case_id: str | None = None
        self.chat_context: dict[str, Any] | None = None
        self.take: Take | None = None
        self.busy = False
        self.shown_suggestions: set[str] = set()

    def reset(self) -> None:
        """New conversation for the same signed-in customer."""
        self.case_id, self.chat_context = None, None
        self.shown_suggestions = set()

    def sly_data(self) -> dict[str, Any]:
        sly = dict(self.gateway_sly)
        if self.case_id:
            sly["case_id"] = self.case_id
        return sly

    def case_view(self) -> dict[str, Any] | None:
        if not self.case_id:
            return None
        try:
            return CaseStore().case_view(self.case_id)
        except CaseError:
            return None

    def start_take(self, scenario: str | None = None) -> Take:
        self.take = Take(self.customer.public(), self.gateway_sly, scenario)
        return self.take

    def finish_take(self) -> dict[str, Any] | None:
        take, self.take = self.take, None
        if take is None or not take.data["turns"]:
            return None
        return take.finish(self.case_view())

    async def send(self, text: str, timeout_s: float = 960) -> AsyncIterator[dict[str, Any]]:
        """One customer message; yields events, then a final 'turn_end' event."""
        if self.busy:
            raise RuntimeError("the agents are still answering the previous message")
        self.busy = True
        normaliser, events, error = Normaliser(), [], None
        held: dict[str, Any] | None = None
        started = time.monotonic()
        since = turn_start()
        try:
            async for event in stream_turn(self.base_url, text, self.sly_data(), self.chat_context, normaliser,
                                           timeout_s):
                if event["kind"] == "answer":  # held back until the gateway's checks have run (finish_turn)
                    held = event
                    continue
                events.append(event)
                yield event
                if event["kind"] == "tool_result" and event["from"] == "open_case" and not self.case_id:
                    result = event["detail"].get("result")
                    if isinstance(result, dict) and result.get("case_id"):
                        self.case_id = result["case_id"]
                if event["kind"] == "tool_result" and event["from"] in CASE_TOOLS:
                    view = self.case_view()
                    if view:
                        snap = {"id": -event["id"], "t": event["t"], "kind": "case", "case": view, "say": "",
                                "tech": "case store"}
                        events.append(snap)
                        yield snap
        except Exception as e:  # noqa: BLE001 - shown to the presenter and recorded
            error = f"{type(e).__name__}: {e}"[:300]
            yield {"id": 0, "t": round(time.monotonic() - started, 2), "kind": "error", "say": error, "tech": error}
        finally:
            self.busy = False
        self.case_id = normaliser.sly_data.get("case_id") or self.case_id
        self.chat_context = normaliser.chat_context or self.chat_context
        answer = normaliser.answer
        reply_ok = not error
        watching: list[str] = []
        if not error:  # the gateway's checks: readable reply (else a fixed message), nothing silently left open
            answer, checked = finish_turn(self.case_id, since, normaliser.answer, customer_message=text)
            watching = checked.get("watching") or []  # watches the Guardian starts (the map: Orbit -> Guardian)
            reply_ok = not checked["reply_problem"]
            if checked["left_open"]:
                tech = "reply was not readable text: replaced; open issues handed to a person" \
                    if checked["reply_problem"] else "end-of-turn check: open issues handed to a person"
                snap = {"id": 0, "t": round(time.monotonic() - started, 2), "kind": "case", "case": self.case_view(),
                        "say": "", "tech": tech}
                events.append(snap)
                yield snap
        if held is not None or answer != normaliser.answer:
            shown = {**(held or {"id": 0, "kind": "answer", "say": f"{agents.label(agents.FRONT)} writes the answer to the customer.",
                                 "tech": ""}), "t": round(time.monotonic() - started, 2), "text": answer}
            events.append(shown)
            yield shown
        # Product cards under the reply, from what the engine recorded on the case. After an unusable reply they are
        # marked as seen but not shown (the customer was told a colleague will pick it up).
        cards = suggestion_events(self.case_view(), self.shown_suggestions)
        for card in cards if reply_ok else []:
            card["t"] = round(time.monotonic() - started, 2)
            events.append(card)
            yield card
        seconds = time.monotonic() - started
        if self.take is not None:
            self.take.add_turn(text, events, answer, seconds, normaliser.cost, error)
        yield {"id": 0, "t": round(seconds, 2), "kind": "turn_end", "answer": answer, "error": error,
               "cost": normaliser.cost, "case_id": self.case_id, "sly_data_after": self.sly_data(),
               "watching": len(watching), "say": "", "tech": ""}
