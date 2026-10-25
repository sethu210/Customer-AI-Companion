"""
The human side of the loop: one review queue for everything the agents handed to people, and the decisions
people make on it.

Queue items
    action review   an action the policy queued for a person (approval above the automatic limit, specialist
                    review, billing review, or shadow mode). Approve = execute it now; reject = decline it.
    hand-off        an issue handed over by the multi-agent network (case store), or a customer request from
                    the single-agent network (escalate_to_human). Resolve = a person dealt with it.

Approving never trusts the queued snapshot: the action is re-checked against fresh data. A person's approval
replaces the approval limit and the specialist review (that is what they are for), but not the rules of the
action itself: a refund that has meanwhile completed, or a return window that has closed, cannot be approved.
The execution is verified by read-back, linked back to the case and issue, audited with the reviewer's name,
and a customer notification is queued in the outbox for the channel gateway to send.
"""

from typing import Any

from coded_tools.customer_service import action_service
from coded_tools.customer_service import tracing
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.action_store import MockSystemOfRecord
from coded_tools.customer_service.action_store import idempotency_key
from coded_tools.customer_service.case_store import EXECUTED_VERIFIED
from coded_tools.customer_service.case_store import QUEUED_FOR_REVIEW
from coded_tools.customer_service.case_store import REVIEW_REJECTED
from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.policy import ALLOW
from coded_tools.customer_service.policy import DENY
from coded_tools.customer_service.policy import CUSTOMER_HINTS
from coded_tools.customer_service.policy import PolicyDecision
from coded_tools.customer_service.policy import decide

REVIEWS = "reviews.jsonl"
OUTBOX = "outbox.jsonl"

ACTION_WORDS = {
    "initiate_refund": "your refund", "retrigger_refund": "your refund",
    "reissue_refund_store_credit": "your store credit", "open_carrier_trace": "a trace with the carrier",
    "create_return": "your return", "cancel_order": "the cancellation of your order",
    "open_billing_review": "the review of your billing question",
}
STARTS_REFUND = {"initiate_refund", "retrigger_refund"}


class ReviewError(ValueError):
    """The request cannot be carried out; the message is safe to show to the reviewer."""


class ReviewService:
    def __init__(self, actions: ActionStore, cases: CaseStore, adapter: CustomerDataAdapter | None,
                 policy: dict[str, Any]) -> None:
        """adapter None = connect to the configured backend only when a decision needs fresh data."""
        self.actions = actions
        self.cases = cases
        self._adapter = adapter
        self.policy = policy

    @property
    def adapter(self) -> CustomerDataAdapter:
        if self._adapter is None:
            self._adapter = get_adapter()
        return self._adapter

    # ------------------------------------------------------------------ queue

    def _decisions(self) -> dict[str, dict[str, Any]]:
        return {r["review_id"]: r for r in self.actions.read(REVIEWS)}

    def queue(self, include_decided: bool = False) -> list[dict[str, Any]]:
        decided = self._decisions()
        items = []
        for c in self.actions.cases():
            proposal = c.get("proposal") or {}
            items.append({
                "review_id": c["case_id"], "type": "action_review" if proposal else "customer_request",
                "kind": c["kind"], "created_at": c["at"], "customer_id": c["customer_id"], "summary": c["summary"],
                "action": proposal.get("action"), "target_id": proposal.get("target_id"),
                "amount": proposal.get("amount"), "reference": proposal.get("reference"),
                "support_case_id": proposal.get("support_case_id"),
                "support_issue_id": proposal.get("support_issue_id"),
                "status": decided[c["case_id"]]["decision"] if c["case_id"] in decided else "open",
            })
        for h in self.cases.handoffs(status=None):
            packet = h["packet"]
            items.append({
                "review_id": h["handoff_id"], "type": "handoff", "kind": h["reason"], "created_at": h["created_at"],
                "customer_id": h["customer_id"], "summary": packet.get("agent_summary") or packet.get("detail") or "",
                "support_case_id": h["case_id"], "issue_ids": h["issue_ids"],
                "identity_status": packet.get("identity_status"), "status": h["status"],
            })
        items.sort(key=lambda i: i["created_at"])
        return items if include_decided else [i for i in items if i["status"] == "open"]

    def item(self, review_id: str) -> dict[str, Any]:
        found = [i for i in self.queue(include_decided=True) if i["review_id"] == review_id]
        if not found:
            raise ReviewError(f"{review_id} is not in the review queue")
        return found[0]

    def detail(self, review_id: str) -> dict[str, Any]:
        """The item plus everything a reviewer needs: the support case record or hand-off packet."""
        item = self.item(review_id)
        if item["type"] == "handoff":
            item["packet"] = self.cases.handoff_packet(review_id)
        elif item.get("support_case_id"):
            item["case"] = self.cases.case_view(item["support_case_id"])
        return item

    def _open(self, review_id: str, wanted_type: str | None = None) -> dict[str, Any]:
        item = self.item(review_id)
        if item["status"] != "open":
            raise ReviewError(f"{review_id} was already {item['status']}")
        if wanted_type and item["type"] != wanted_type:
            raise ReviewError(f"{review_id} is a {item['type'].replace('_', ' ')}; use "
                              f"{'resolve' if item['type'] != 'action_review' else 'approve or reject'}")
        return item

    @staticmethod
    def _require_reviewer(reviewer: str) -> str:
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ReviewError("reviewer name is required; every decision is recorded with it")
        return reviewer[:80]

    def _notify(self, item: dict[str, Any], message: str) -> None:
        self.actions.append(OUTBOX, {"customer_id": item["customer_id"], "review_id": item["review_id"],
                                     "reference": item.get("reference"), "support_case_id": item.get("support_case_id"),
                                     "message": message, "status": "pending_send"})

    def _close_issue(self, item: dict[str, Any], status: str, outcome: dict[str, Any]) -> None:
        if not item.get("support_issue_id"):
            return
        issue = self.cases.get_issue(item["support_case_id"], item["support_issue_id"])
        if issue["status"] == QUEUED_FOR_REVIEW:
            self.cases.set_status(item["support_case_id"], item["support_issue_id"], status, by="review",
                                  outcome=outcome)

    # ------------------------------------------------------------------ decisions

    async def approve(self, review_id: str, *, reviewer: str, note: str = "") -> dict[str, Any]:
        reviewer = self._require_reviewer(reviewer)
        item = self._open(review_id, "action_review")
        with tracing.span("review.approve", review_id=review_id, action=item["action"], reviewer_role="staff",
                          case_id=item.get("support_case_id"), issue_id=item.get("support_issue_id")) as current:
            result = await self._approve(item, reviewer, note)
            tracing.record(current, outcome=result["outcome"], reason=result.get("reason"))
            return result

    async def _approve(self, item: dict[str, Any], reviewer: str, note: str) -> dict[str, Any]:
        customer_id, action, target_id = item["customer_id"], item["action"], item["target_id"]
        context, stuck, signals = await action_service.fresh_facts(self.adapter, self.policy, customer_id)
        # Staff act with full authority and the customer already asked for this; the action's own rules still apply.
        fresh = decide(action, target_id, "strong", context, stuck, signals, self.policy, customer_confirmed=True)
        link = {"case_id": item.get("support_case_id"), "issue_id": item.get("support_issue_id"),
                "reviewer": reviewer}
        if fresh.decision == DENY:
            self.actions.audit(customer_id, "review_console", "staff", fresh, "review", "approval_not_possible",
                               item["reference"], **link)
            return {"outcome": "not_possible", "reason": fresh.reason_code, "review_id": item["review_id"],
                    "explanation": CUSTOMER_HINTS.get(fresh.reason_code, ""),
                    "next_step": "The data changed since this was queued. Reject it with a message to the customer."}

        if not self.actions.claim_review(item["review_id"], reviewer):  # two reviewers at once: only one acts (P4)
            raise ReviewError(f"{item['review_id']} was already decided by another reviewer")
        approved = PolicyDecision(action, target_id, ALLOW, "human_approved", fresh.amount,
                                  facts={**fresh.facts, "reviewer": reviewer, "original_decision": fresh.decision,
                                         "original_reason": fresh.reason_code})
        receipt = MockSystemOfRecord(self.actions).execute(approved, item["reference"])
        verified = self.actions.executed_receipt(item["reference"]) is not None
        outcome = "executed" if verified else "executed_unverified"
        key = idempotency_key(customer_id, action, target_id)
        self.actions.audit(customer_id, "review_console", "staff", approved, "review", f"approved_{outcome}",
                           item["reference"], **link)
        self.actions.record_action(key, customer_id, approved, outcome, item["reference"], item["review_id"],
                                   receipt if verified else None,
                                   link={"case_id": link["case_id"], "issue_id": link["issue_id"]})
        self.actions.append(REVIEWS, {"review_id": item["review_id"], "decision": "approved", "reviewer": reviewer,
                                      "note": note[:1000], "outcome": outcome, "amount": approved.amount})
        if verified:
            self._close_issue(item, EXECUTED_VERIFIED, {"outcome": "executed", "reference": item["reference"],
                                                        "reviewer": reviewer})
            amount = f" of ${approved.amount:.2f}" if approved.amount else ""
            # A refund that was started has not reached the customer yet: the Resolution Guardian tells them when the
            # records show it completed.
            done = "has been approved and started" if action in STARTS_REFUND else "has been completed"
            self._notify(item, f"Good news: {ACTION_WORDS.get(action, 'your request')}{amount} {done}. "
                               f"Reference {item['reference']}.")
        return {"outcome": outcome, "review_id": item["review_id"], "reference": item["reference"],
                "amount": approved.amount, "reason": "human_approved"}

    def reject(self, review_id: str, *, reviewer: str, reason: str, customer_message: str = "") -> dict[str, Any]:
        reviewer = self._require_reviewer(reviewer)
        if not (reason or "").strip():
            raise ReviewError("a reason is required to reject")
        item = self._open(review_id, "action_review")
        if not self.actions.claim_review(review_id, reviewer):
            raise ReviewError(f"{review_id} was already decided by another reviewer")
        with tracing.span("review.reject", review_id=review_id, action=item["action"], reviewer_role="staff",
                          case_id=item.get("support_case_id"), issue_id=item.get("support_issue_id")):
            declined = PolicyDecision(item["action"], item["target_id"], DENY, "review_rejected", item["amount"] or 0.0,
                                      facts={"reviewer": reviewer, "reason": reason[:1000]})
            self.actions.audit(item["customer_id"], "review_console", "staff", declined, "review", "review_rejected",
                               item["reference"], case_id=item.get("support_case_id"),
                               issue_id=item.get("support_issue_id"), reviewer=reviewer)
            self.actions.append(REVIEWS, {"review_id": review_id, "decision": "rejected", "reviewer": reviewer,
                                          "note": reason[:1000]})
            self._close_issue(item, REVIEW_REJECTED, {"outcome": "review_rejected", "reviewer": reviewer})
            self._notify(item, (customer_message.strip() or
                                f"We have reviewed {ACTION_WORDS.get(item['action'], 'your request')} and could not "
                                f"complete it. Please contact us with reference {item['reference']} if you have "
                                f"questions.")[:1000])
        return {"outcome": "rejected", "review_id": review_id, "reference": item["reference"]}

    def resolve(self, review_id: str, *, reviewer: str, note: str, customer_message: str = "") -> dict[str, Any]:
        """Closes a hand-off or a customer request after a person dealt with it."""
        reviewer = self._require_reviewer(reviewer)
        if not (note or "").strip():
            raise ReviewError("a note is required: what was done")
        item = self._open(review_id)
        if item["type"] == "action_review":
            raise ReviewError(f"{review_id} is an action review; use approve or reject")
        with tracing.span("review.resolve", review_id=review_id, kind=item["kind"], reviewer_role="staff",
                          case_id=item.get("support_case_id")):
            if item["type"] == "handoff":
                try:
                    self.cases.resolve_handoff(review_id, reviewer=reviewer, note=note[:1000])
                except CaseError as e:
                    raise ReviewError(str(e)) from e
            else:
                self.actions.append(REVIEWS, {"review_id": review_id, "decision": "resolved", "reviewer": reviewer,
                                              "note": note[:1000]})
            if customer_message.strip() and item.get("customer_id"):
                self._notify(item, customer_message.strip()[:1000])
        return {"outcome": "resolved", "review_id": review_id}

    def outbox(self) -> list[dict[str, Any]]:
        """Messages to customers, for staff: never private ones (a customer's sign-in code)."""
        return [r for r in self.actions.read(OUTBOX) if not r.get("private")]
