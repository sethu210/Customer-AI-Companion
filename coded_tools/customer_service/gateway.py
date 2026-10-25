"""
What a channel gateway does after each reply of the multi-agent network, in code (no LLM).

finish_turn() is called by every gateway in this project (evaluation runner, nsflow UI runner, demo console):
    1. check the reply is something a customer can read: not empty, not JSON or tool arguments (the free model has
       written a tool call's arguments as its answer instead of calling the tool), and that it agrees with the
       records (reply_guard: no invented references or amounts, no "done" without a verified execution, no
       internal ids or promises to follow up);
    2. run the case store's end-of-turn check (G4): issues neither closed nor waiting for the customer are handed to
       a person. After an unusable reply nothing counts as waiting (the customer never saw the question), so every
       open issue is handed off;
    3. replace an unusable reply with a fixed message that only promises what step 2 actually did, plus fixed
       sentences from the case record about actions already done or with a colleague (reply_guard.safe_summary);
    4. start the Resolution Guardian's watches (guardian.watch_case) for issues whose business outcome is still to
       come. A failure here is logged and recorded; it never changes the reply;
    5. add the turn to the customer's companion thread (companion.CompanionStore.record_turn): the message and the
       reply actually shown, for an identified customer only. Also never changes the reply.
"""

import json
import logging
import re
from typing import Any

from coded_tools.customer_service import companion
from coded_tools.customer_service import guardian
from coded_tools.customer_service import reply_guard
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore

LOGGER = logging.getLogger(__name__)

_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$")


def reply_problem(answer: str | None) -> str:
    """'' if the reply can be shown to the customer, else why not."""
    text = _FENCE.sub("", (answer or "").strip()).strip()
    if not text:
        return "empty"
    if text[0] in "{[":
        try:
            json.loads(text.replace("{{", "{").replace("}}", "}"))
            return "json"
        except ValueError:
            if text[-1] in "}]":
                return "json"
    return ""


def fallback_reply(case_id: str | None, handed_off: bool, summary: str = "") -> str:
    """summary: fixed sentences from the case record (reply_guard.safe_summary) about what was already done."""
    known = f" {summary}" if summary else ""
    if handed_off:
        return (f"Sorry, something went wrong on our side and I could not finish your request.{known} A colleague will "
                f"pick it up from your case {case_id}, so you will not need to repeat anything.")
    reference = f" Your case reference is {case_id}." if case_id else ""
    return f"Sorry, something went wrong on our side.{known} Please send your message again.{reference}"


def allowed_references(store: CaseStore, case: dict[str, Any] | None, case_id: str | None) -> set[str]:
    """The references a reply may name: this customer's cases, their actions' references and review cases."""
    allowed = {case_id} if case_id else set()
    customer_id = (case or {}).get("customer_id")
    if customer_id:
        allowed |= {c["case_id"] for c in store.customer_cases(customer_id, limit=1000)}
        allowed |= ActionStore().customer_references(customer_id)
    return allowed


def unsafe_problem(store: CaseStore | None, case_id: str | None, answer: str) -> str:
    """'' if the reply agrees with the records (reply_guard), else what is wrong, as 'unsafe: ...'."""
    case = store.get_case(case_id) if store is not None and case_id else None
    view = store.case_view(case_id) if case else None
    found = reply_guard.problems(answer, view, allowed_references(store, case, case_id) if store else set())
    if found:
        LOGGER.warning("reply not shown for case %s: %s", case_id, "; ".join(found))
    return f"unsafe: {'; '.join(found)}"[:300] if found else ""


def finish_turn(case_id: str | None, since: str, answer: str | None,
                customer_message: str | None = None) -> tuple[str, dict[str, Any]]:
    """
    (the reply to show, a record of the checks). `since` is case_store.turn_start() taken before sending;
    `customer_message` is what the customer wrote (for the thread; gateways that pass None record nothing).
    """
    problem = reply_problem(answer)
    checked: dict[str, Any] = {"left_open": [], "handoff_id": None, "completed": False}
    store = CaseStore() if case_id else None
    if not problem:  # D4: what it says must agree with the records, or it is not shown
        problem = unsafe_problem(store, case_id, answer or "")
    if case_id:
        checked = store.end_of_turn(case_id, since, reply_delivered=not problem)
        try:
            checked["watching"] = guardian.watch_case(case_id, cases=store)
        except Exception as e:  # noqa: BLE001 - the reply must still go out; the gap is visible in the record
            LOGGER.exception("guardian.watch_case failed for %s", case_id)
            checked["watching_error"] = f"{type(e).__name__}: {e}"[:200]
    checked["reply_problem"] = problem
    summary = reply_guard.safe_summary(store.case_view(case_id)) if problem and store is not None else ""
    shown = (answer or "") if not problem else fallback_reply(case_id, bool(checked["handoff_id"]), summary)
    if store is not None and customer_message is not None:
        try:
            case = store.get_case(case_id) or {}
            companion.CompanionStore(store.path).record_turn(
                case.get("customer_id"), case_id=case_id, channel=case.get("channel"), message=customer_message,
                reply=shown, asked_at=since)
            checked["thread_recorded"] = bool(case.get("customer_id"))
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("companion thread not recorded for %s", case_id)
            checked["thread_error"] = f"{type(e).__name__}: {e}"[:200]
    return shown, checked
