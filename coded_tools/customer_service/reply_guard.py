"""
Checks the front agent's reply against the records, in code (review finding D4). Run by gateway.finish_turn on every
reply before the customer sees it; the prompt rules stay, this is what enforces them.

    references   every ACT-... and CASE-... in the reply is one of this customer's (their cases, their actions'
                 references and review cases); never an invented one
    amounts      every amount with cents ($12.34) is in the case's records (findings, evidence, outcomes), or the
                 sum of two of them
    done claims  "has been refunded", "I've cancelled", ... only when an action on this case was executed and
                 verified (the same rule the evaluation scorer uses, evaluation/score.py)
    forbidden    internal ids and risk words, and promises to contact the customer later

A reply that breaks one is not shown: gateway.finish_turn replaces it with a fixed message built from the case
record (safe_summary), exactly as for an unreadable reply.
"""

import itertools
import json
import re
from typing import Any

from coded_tools.customer_service.case_store import EXECUTED_VERIFIED
from coded_tools.customer_service.case_store import HANDED_OFF
from coded_tools.customer_service.case_store import QUEUED_FOR_REVIEW

REFERENCE = re.compile(r"\b(?:ACT|CASE)-[A-Z0-9]{6,}\b")
AMOUNT = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?!\d)")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
DONE_CLAIMS = ("has been refunded", "have refunded", "i've issued", "i have issued", "has been issued",
               "has been processed", "i've cancelled", "has been cancelled", "trace has been opened",
               "i've opened a trace", "i have opened a trace", "i've started the return", "return has been created")
# A claim in a clause with one of these before it is a denial or a future condition ("no refund has been issued
# yet", "once the return has been created"), not a claim that something was done.
NOT_A_CLAIM = re.compile(r"\b(no|not|never|yet to|once|when|after|until|if|before)\b|n't\b")
CLAUSE_SPLIT = re.compile(r"[.!?;,:\n]+|\bbut\b")
FORBIDDEN = ("CUST-", "tok_", "SKU-", "fraud", "risk flag", "high_refund_rate", "chargeback", "fraud_review",
             "I'll let you know", "I will let you know", "I'll follow up", "I will follow up", "I'll keep you updated",
             "I will keep you updated", "I'll keep an eye")
MAX_SUMMED = 400  # evidence numbers taken into the pairwise sums (bounded work)

ACTION_WORDS = {
    "initiate_refund": "your refund", "retrigger_refund": "your refund",
    "reissue_refund_store_credit": "your store credit", "open_carrier_trace": "a trace with the carrier",
    "create_return": "your return", "cancel_order": "cancelling your order",
    "open_billing_review": "a review of your billing question",
}


def claims_done(text: str) -> bool:
    """True if any clause says something was done, ignoring negated or conditional phrasings."""
    for clause in CLAUSE_SPLIT.split(text.lower()):
        for claim in DONE_CLAIMS:
            at = clause.find(claim)
            if at >= 0 and not NOT_A_CLAIM.search(clause[:at]):
                return True
    return False


def _amounts(text: str) -> set[float]:
    return {float(a.replace(",", "")) for a in AMOUNT.findall(text)}


def _evidence_numbers(view: dict[str, Any] | None) -> set[float]:
    if not view:
        return set()
    numbers = sorted({round(float(n), 2) for n in NUMBER.findall(json.dumps(view, default=str))})
    small = numbers[:MAX_SUMMED]
    return set(numbers) | {round(a + b, 2) for a, b in itertools.combinations(small, 2)}


def problems(answer: str, view: dict[str, Any] | None, allowed_references: set[str]) -> list[str]:
    """What is wrong with the reply against the records; [] when it may be shown."""
    out = []
    unknown = sorted(set(REFERENCE.findall(answer)) - allowed_references)
    if unknown:
        out.append(f"unknown_reference: {', '.join(unknown)[:120]}")
    evidence = _evidence_numbers(view)
    invented = sorted(a for a in _amounts(answer) if round(a, 2) not in evidence)
    if invented:
        out.append(f"unsupported_amount: {', '.join(f'{a:.2f}' for a in invented)[:120]}")
    executed = any(i["status"] == EXECUTED_VERIFIED for i in (view or {}).get("issues") or [])
    if claims_done(answer) and not executed:
        out.append("claims_done_without_execution")
    lowered = answer.lower()
    said = [s for s in FORBIDDEN if s.lower() in lowered]
    if said:
        out.append(f"forbidden: {', '.join(said)}")
    return out


def safe_summary(view: dict[str, Any] | None) -> str:
    """Fixed sentences from the case record, for a reply that could not be shown: only what is recorded."""
    lines = []
    for issue in (view or {}).get("issues") or []:
        outcome = issue.get("outcome") or {}
        what = ACTION_WORDS.get((issue.get("proposal") or {}).get("action"), "your request")
        reference = outcome.get("reference")
        ref = f" (reference {reference})" if reference else ""
        if issue["status"] == EXECUTED_VERIFIED:
            lines.append(f"Done: {what}{ref}.")
        elif issue["status"] == QUEUED_FOR_REVIEW:
            lines.append(f"A colleague will review {what}{ref}.")
        elif issue["status"] == HANDED_OFF:
            lines.append(f"A colleague will pick up {what}{ref}.")
    return " ".join(dict.fromkeys(lines))
