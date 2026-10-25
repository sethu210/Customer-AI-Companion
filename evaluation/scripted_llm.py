"""
A scripted stand-in for the LLM, for testing the multi-agent network through the real Neuro SAN server
without any model provider (no OpenRouter quota used).

It is a LangChain chat model that, instead of generating text, follows each agent's instructions with simple
deterministic rules: the orchestrator calls intake, routes issues to specialists (several at once, to exercise
parallel tool calls), consults the policy agent, submits actions, closes issues and completes the case; each
specialist calls its tools and records findings. The agent is recognised by the tools bound to it.

What this proves: the network definition loads, coded tools resolve, sub-agents are called with arguments,
sly_data is shared between agents and the case id goes back to the gateway, several tool calls in one turn
work, accounting messages stream, and the evaluation runner and scorer work end to end.
What it does not prove: anything about a real model's judgement, wording or instruction following.

Enable for the multi-agent networks by starting the server with
    NS_LLM_CLASS=evaluation.scripted_llm.ScriptedSupportLlm   NS_LLM_MODEL=scripted
(config/llm_config.hocon reads both; see scripts/eval.ps1 -Scripted).
"""

import ast
import json
import re
import uuid
from typing import Any
from typing import ClassVar
from typing import Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult
from pydantic import ConfigDict
from pydantic import Field

ISSUE_RE = re.compile(r"CASE-[0-9A-F]{10}-I\d+")
ITEM_WORDS = ("jacket", "shoes", "sweater", "shirt", "belt", "overcoat", "coat", "chinos", "pants", "dress",
              "boots", "sneakers", "jeans", "scarf", "hoodie")
COLOURS = ("blue", "red", "grey", "gray", "black", "white", "green", "navy", "brown", "beige", "wool", "running",
           "rain", "puffer")
SPECIALISTS = {"order_delivery", "returns_refunds", "payments_billing", "loyalty_account"}
MEMORY_TOOLS = {"remember_preference", "forget_preference"}
NOTE_ID = re.compile(r"\[(P\d+)\]")  # a remembered preference's id in the companion notes (system message)


def preference(text: str) -> tuple[str, str]:
    """(kind, value) the customer asked to be remembered, read with simple rules."""
    t = text.lower()
    size = re.search(r"size (\d+|xs|s|m|l|xl|xxl) in (shoes|footwear|clothing|trousers)", t)
    if size:
        return "size", f"{size.group(2)} {size.group(1)}"
    for kind, pattern in (("avoids", r"\b(?:avoid|don't like|do not like)\s+([a-z ]+)"),
                          ("occasion", r"\bshopping for\s+([a-z ]+)"),
                          ("likes", r"\b(?:like|prefer|love)\s+([a-z ]+)")):
        match = re.search(pattern, t)
        if match:
            return kind, match.group(1).strip()
    return "likes", t[:40]


def parse(content: Any) -> Any:
    """Tool results arrive as text: JSON, a Python dict repr, or prose with an embedded object."""
    text = content if isinstance(content, str) else json.dumps(content)
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError, TypeError):
            pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(match.group(0))
            except (ValueError, SyntaxError, TypeError):
                pass
    return text


def item_query(text: str) -> str | None:
    words = re.findall(r"[a-z]+", text.lower())
    for i, word in enumerate(words):
        if word.rstrip("s") in [w.rstrip("s") for w in ITEM_WORDS]:
            return f"{words[i - 1]} {word}" if i and words[i - 1] in COLOURS else word
    return None


class ScriptedSupportLlm(BaseChatModel):
    model_name: str = Field(default="scripted", alias="model")
    tool_names: list[str] = Field(default_factory=list)
    model_config = ConfigDict(populate_by_name=True)

    # ------------------------------------------------------------------ LangChain plumbing

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedSupportLlm":
        names = []
        for tool in tools:
            name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            if name is None and isinstance(tool, dict):
                name = (tool.get("function") or {}).get("name")
            names.append(name)
        return self.model_copy(update={"tool_names": [n for n in names if n]})

    @property
    def _llm_type(self) -> str:
        return "scripted-support"

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        calls, text = self._decide(messages)
        message = AIMessage(content=text if not calls else "",
                            tool_calls=[{"name": n, "args": a, "id": f"call_{uuid.uuid4().hex[:12]}",
                                         "type": "tool_call"} for n, a in calls],
                            usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
                            response_metadata={"model_name": self.model_name})
        return ChatResult(generations=[ChatGeneration(message=message)])

    # ------------------------------------------------------------------ conversation state

    @staticmethod
    def _notes(messages: list[BaseMessage]) -> str:
        """The agent's instructions as the server sent them (with the companion notes added by code)."""
        return "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))

    @staticmethod
    def _this_turn(messages: list[BaseMessage]) -> tuple[str, dict[str, list[Any]], list[str]]:
        """The latest human text, results per tool since then, and earlier human messages."""
        last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
        names = {}
        results: dict[str, list[Any]] = {}
        for m in messages[last_human:]:
            if isinstance(m, AIMessage):
                for c in m.tool_calls or []:
                    names[c["id"]] = c["name"]
            if isinstance(m, ToolMessage):
                name = names.get(m.tool_call_id) or getattr(m, "name", None) or "unknown"
                results.setdefault(name, []).append(parse(m.content))
        earlier = [str(m.content) for m in messages[:last_human] if isinstance(m, HumanMessage)]
        return str(messages[last_human].content) if messages else "", results, earlier

    def _decide(self, messages: list[BaseMessage]) -> tuple[list[tuple[str, dict]], str]:
        human, results, earlier = self._this_turn(messages)
        tools = set(self.tool_names)
        if "intake_identity" in tools:
            return self._orchestrator(human, results, earlier, self._notes(messages))
        if "open_case" in tools:
            return self._intake(human, results)
        if "get_recommendations" in tools:
            return self._advisor(human, results)
        if "get_stylist_picks" in tools:
            return self._stylist(human, results)
        if "evaluate_policy" in tools:
            return self._policy(human, results)
        if "get_orders" in tools:
            return self._order(human, results)
        if "get_returns_refunds" in tools:
            return self._returns(human, results)
        if "get_payment_summary" in tools:
            return self._payments(human, results)
        if "get_loyalty_profile" in tools:
            return self._loyalty(human, results)
        return [], "I can help with orders, deliveries, returns and refunds."

    # ------------------------------------------------------------------ orchestrator

    def _orchestrator(self, human: str, r: dict, earlier: list[str], notes: str = "") -> tuple[list, str]:
        confirming = bool(earlier) and re.search(r"\b(yes|please do|go ahead|confirm)\b", human, re.I)
        if confirming and "case_state" not in r and "intake_identity" not in r:
            return [("case_state", {"op": "get"})], ""
        if confirming:
            return self._after_confirmation(r)
        if "intake_identity" not in r:
            return [("intake_identity", {"customer_message": human})], ""
        intake = r["intake_identity"][-1]
        if not isinstance(intake, dict) or not intake.get("can_read_account"):
            hint = intake.get("message_hint") if isinstance(intake, dict) else ""
            return [], hint or "I could not identify your account. Please log in or use the verification link."
        issues = intake.get("issues") or []
        if not any(k in r for k in SPECIALISTS | MEMORY_TOOLS | {"handoff_to_human", "stylist"}):
            calls = []
            for i in issues:
                if i.get("status") not in ("open", "investigating"):
                    continue
                if i["domain"] in SPECIALISTS:
                    calls.append((i["domain"], {"issue_id": i["issue_id"], "request": i.get("summary") or human}))
                elif i["domain"] == "shopping":
                    continue  # suggestions come last, once the other requests are closed (_close)
                elif i["domain"] == "companion":
                    calls.append(self._memory_call(i["issue_id"], human, notes))
                elif i["domain"] == "stylist":
                    calls.append(("stylist", {"issue_id": i["issue_id"], "request": human}))
                else:
                    reason = "customer_requested" if i.get("kind") == "human_requested" else "not_supported"
                    calls.append(("handoff_to_human", {"issue_ids": [i["issue_id"]], "reason": reason,
                                                        "summary": i.get("summary") or human}))
            if calls:
                return calls, ""  # several specialists in one turn: exercises parallel tool calls
        if "case_state" not in r:
            return [("case_state", {"op": "get"})], ""
        case = r["case_state"][0] if isinstance(r["case_state"][0], dict) else {}
        proposed = [i for i in case.get("issues", []) if i.get("status") == "proposed" and i.get("proposal")]
        lean = "policy_risk_escalation" not in self.tool_names  # lean variant: submit directly
        if proposed and not lean and "policy_risk_escalation" not in r:
            return [("policy_risk_escalation", {"issue_id": i["issue_id"]}) for i in proposed], ""
        if proposed and lean and "submit_action" not in r:
            return [("submit_action", {"issue_id": i["issue_id"]}) for i in proposed], ""
        if proposed and "submit_action" not in r:
            calls = []
            for rec in r["policy_risk_escalation"]:
                rec = rec if isinstance(rec, dict) else {}
                if rec.get("recommendation") == "NEEDS_MORE_INFORMATION" and \
                        rec.get("missing") == "customer_confirmation":
                    continue  # ask first; a weak sign-in (step_up_auth) is submitted so the refusal is recorded
                calls.append(("submit_action", {"issue_id": rec.get("issue_id")}))
            if calls:
                return calls, ""
        return self._close(r, case)

    @staticmethod
    def _memory_call(issue_id: str, human: str, notes: str) -> tuple[str, dict]:
        """Forget: the preference's id from the companion notes (proves the notes reach the agent). Else remember."""
        if re.search(r"\bforget\b", human, re.I):
            ids = NOTE_ID.findall(notes)
            return "forget_preference", {"issue_id": issue_id, "memory_id": ids[-1] if ids else "ALL"}
        kind, value = preference(human)
        return "remember_preference", {"issue_id": issue_id, "kind": kind, "value": value}

    def _after_confirmation(self, r: dict) -> tuple[list, str]:
        case = r["case_state"][0] if isinstance(r["case_state"][0], dict) else {}
        waiting = [i for i in case.get("issues", []) if i.get("status") == "proposed"]
        if waiting and "submit_action" not in r:
            return [("submit_action", {"issue_id": i["issue_id"], "customer_confirmed": True}) for i in waiting], ""
        asked = [i for i in case.get("issues", []) if i.get("status") == "awaiting_customer"
                 and (i.get("outcome") or {}).get("outcome") == "awaiting_preference_confirmation"]
        if asked and "remember_preference" not in r:
            return [("remember_preference", {"issue_id": i["issue_id"], "customer_confirmed": True})
                    for i in asked], ""
        return self._close(r, case)

    def _close(self, r: dict, case: dict) -> tuple[list, str]:
        """Close answered issues, complete the case (refused if anything is still open), then reply."""
        submitted = {s.get("issue_id") or "" for s in r.get("submit_action", []) if isinstance(s, dict)}
        denied_ids = {s.get("issue_id") for s in r.get("submit_action", [])
                      if isinstance(s, dict) and s.get("outcome") == "denied"}
        info = [i for i in case.get("issues", []) if i.get("domain") != "shopping"
                and (i.get("status") == "investigating" or i["issue_id"] in denied_ids)]
        if info and "update_issue" not in r:
            return [("update_issue", {"issue_id": i["issue_id"], "status": "resolved_info_only"}) for i in info], ""
        shopping = [i for i in case.get("issues", []) if i.get("domain") == "shopping"
                    and i.get("status") in ("open", "investigating")]
        if shopping and "product_advisor" not in r:
            return [("product_advisor", {"issue_id": i["issue_id"],
                                         "request": (i.get("refs") or {}).get("item_query") or ""})
                    for i in shopping], ""
        closed = {u.get("issue_id") for u in r.get("update_issue", []) if isinstance(u, dict)}
        suggested = [g["issue_id"] for g in r.get("product_advisor", [])
                     if isinstance(g, dict) and not g.get("error") and g.get("issue_id") not in closed]
        if suggested:
            return [("update_issue", {"issue_id": iid, "status": "resolved_info_only"}) for iid in suggested], ""
        if len(r.get("case_state", [])) < 2:
            return [("case_state", {"op": "complete"})], ""
        lines = []
        for rec in r.get("policy_risk_escalation", []):
            if isinstance(rec, dict) and rec.get("recommendation") == "NEEDS_MORE_INFORMATION":
                lines.append(f"{rec.get('explanation') or ''} Shall I go ahead?".strip())
        for s in r.get("submit_action", []):
            if isinstance(s, dict):
                ref = f" (reference {s['reference']})" if s.get("reference") else ""
                lines.append(f"{s.get('action', 'Request')}: {s.get('outcome')}{ref}. {s.get('message_hint', '')}")
            else:
                lines.append(str(s)[:200])
        for i in case.get("issues", []):
            if i["issue_id"] not in submitted:
                lines += [c for c in i.get("conclusions") or [] if c]
        for m in r.get("remember_preference", []) + r.get("forget_preference", []):
            if isinstance(m, dict):
                lines.append(m.get("question") or (f"I'll remember that {m['remembered']}." if m.get("remembered")
                                                   else f"Done, I've forgotten that {m.get('forgotten')}."))
            else:
                lines.append(str(m)[:200])
        for s in r.get("stylist", []):
            if not isinstance(s, dict) or s.get("error"):
                lines.append(str((s or {}).get("error") if isinstance(s, dict) else s)[:200])
                continue
            for p in s.get("picks") or []:
                lines.append(f"{p['title']} ({p['list_price']} {p['currency']}): " + " ".join(p["picked_because"]))
            if s.get("feedback_recorded"):
                lines.append("Thanks, noted: I'll pick pieces like this less often.")
            elif not s.get("picks"):
                lines.append(s.get("message_hint") or "")
        for g in r.get("product_advisor", []):
            if not isinstance(g, dict) or g.get("error"):
                continue
            for p in g.get("recommendations") or []:
                lines.append(f"{p['title']} ({p['list_price']} {p['currency']}): {p['because']}")
            if not g.get("recommendations"):
                lines.append(g.get("message_hint", ""))
        return [], "\n".join(lines) or "I have checked your account."

    # ------------------------------------------------------------------ intake

    def _intake(self, human: str, r: dict) -> tuple[list, str]:
        if "open_case" not in r:
            return [("open_case", {})], ""
        opened = r["open_case"][0] if isinstance(r["open_case"][0], dict) else {}
        if "add_issues" not in r:
            return [("add_issues", {"issues": self._classify(human)})], ""
        added = r["add_issues"][0] if isinstance(r["add_issues"][0], dict) else {}
        out = {k: opened.get(k) for k in ("identity_status", "can_read_account", "can_request_changes",
                                          "message_hint", "case_id")}
        out["issues"] = added.get("issues", [])
        out["notes"] = added.get("notes", [])
        return [], json.dumps(out)

    @staticmethod
    def _classify(text: str) -> list[dict]:
        t = text.lower()
        query = item_query(text)
        issues = []

        def add(domain: str, kind: str, summary: str, with_item: bool = True) -> None:
            issue = {"domain": domain, "kind": kind, "summary": summary}
            if with_item and query:
                issue["item_query"] = query
            issues.append(issue)

        if re.search(r"\bforget\b|\bremember\b|keep (that|it) in mind", t):
            return [{"domain": "companion", "kind": "preference", "summary": text[:200]}]
        if re.search(r"not for me|you sent|picked for me|your pick", t):
            kind = "pick_feedback" if "not for me" in t else "pick_question"
            return [{"domain": "stylist", "kind": kind, "summary": text[:200]}]
        if re.search(r"charged twice|double charge|charged two|billed twice", t):
            add("payments_billing", "duplicate_charge", "Customer says they were charged twice.", False)
        if re.search(r"\breturn\b", t) and not re.search(r"return (label|status)|my return", t):
            add("returns_refunds", "return_request", "Customer wants to return an item.")
        elif re.search(r"refund|money|my return|store credit", t):
            add("returns_refunds", "refund_status", "Customer asks about a refund.")
        if re.search(r"points|loyalty|tier", t):
            add("loyalty_account", "points_balance", "Customer asks about loyalty points.", False)
        if re.search(r"go(es)? (well )?with|recommend|suggest|(some|any)thing like|similar to", t):
            add("shopping", "product_question", "Customer asks for product suggestions.")
        if re.search(r"cancel", t):
            add("order_delivery", "cancel_request", "Customer wants to cancel an order.")
        elif re.search(r"where|arriv|parcel|moved|deliver|order|shipment", t) and not issues:
            add("order_delivery", "where_is_order", "Customer asks where their order is.")
        if re.search(r"\b(person|human|someone|agent)\b", t):
            add("other", "human_requested", "Customer asks to talk to a person.", False)
        return issues or [{"domain": "other", "kind": "question", "summary": text[:200]}]

    # ------------------------------------------------------------------ specialists and policy

    @staticmethod
    def _issue(human: str) -> str:
        match = ISSUE_RE.search(human)
        return match.group(0) if match else ""

    def _record(self, iid: str, conclusion: str, proposal: dict | None) -> tuple[list, str]:
        args = {"issue_id": iid, "conclusion": conclusion}
        if proposal:
            args["proposed_action"] = proposal
        else:
            args["answered"] = True  # nothing to do: close it in the same step (code may refuse)
        return [("record_findings", args)], ""

    @staticmethod
    def _final(iid: str, r: dict) -> tuple[list, str]:
        recorded = r["record_findings"][0] if isinstance(r["record_findings"][0], dict) else {}
        return [], json.dumps({"issue_id": iid, "facts": [], "proposed_action": recorded.get("proposal"),
                               "needs_from_customer": None, "closed": bool(recorded.get("closed")),
                               "not_closed_because": recorded.get("not_closed_because")})

    def _order(self, human: str, r: dict) -> tuple[list, str]:
        iid = self._issue(human)
        if "get_orders" not in r:
            return [("get_orders", {"issue_id": iid}), ("get_stuck_cases", {"issue_id": iid})], ""
        if "record_findings" not in r:
            stuck = (r["get_stuck_cases"][0] or {}).get("stuck_cases", []) if isinstance(r["get_stuck_cases"][0], dict) else []
            items = (r["get_orders"][0] or {}).get("items", []) if isinstance(r["get_orders"][0], dict) else []
            proposal = None
            if stuck:
                proposal = {"action": "open_carrier_trace", "target_id": stuck[0]["order_id"]}
            elif "cancel" in human.lower() and items:
                proposal = {"action": "cancel_order", "target_id": items[0]["order_id"]}
            latest = items[0] if items else {}
            conclusion = (f"Order {latest.get('order_id')} is {latest.get('order_status')}; last carrier event "
                          f"{latest.get('last_shipment_event')} at {latest.get('last_shipment_location')}.")
            return self._record(iid, conclusion, proposal)
        return self._final(iid, r)

    def _returns(self, human: str, r: dict) -> tuple[list, str]:
        iid = self._issue(human)
        query = item_query(human)
        if "find_order_items" not in r:  # as the prompt says: identify the item first (without words from the
            # request, the tool uses the issue's item_query); the action layer refuses a guessed target (D2)
            return [("find_order_items", {"issue_id": iid, **({"item_query": query} if query else {})})], ""
        found = r.get("find_order_items", [{}])[0]
        items = found.get("items", []) if isinstance(found, dict) else []
        if "get_returns_refunds" not in r:
            args = {"issue_id": iid}
            if len(items) == 1:
                args["item_id"] = items[0]["item_id"]
            return [("get_returns_refunds", args), ("get_stuck_cases", {"issue_id": iid})], ""
        rows = r["get_returns_refunds"][0].get("items", []) if isinstance(r["get_returns_refunds"][0], dict) else []
        if len(rows) > 1 and len(items) != 1:
            target = next((x for x in rows if x.get("return_status") or x.get("refund_status")), rows[0])
        else:
            target = rows[0] if rows else {}
        item_id = target.get("item_id")
        if item_id and "check_return_eligibility" not in r:
            return [("check_return_eligibility", {"issue_id": iid, "item_id": item_id})], ""
        if "record_findings" not in r:
            checks = (r.get("check_return_eligibility", [{}])[0] or {}).get("checks", {})
            wants_return = bool(re.search(r"\breturn\b", human.lower())) and "refund" not in human.lower()
            order = (["create_return"] if wants_return else
                     ["retrigger_refund", "reissue_refund_store_credit", "initiate_refund"])
            action = next((a for a in order if (checks.get(a) or {}).get("applicable")), None)
            if wants_return and not action:
                action = "create_return"  # explicitly asked: submit so the refusal and its reason are recorded
            conclusion = (f"Item {item_id}: return {target.get('return_status')}, refund {target.get('refund_status')}"
                          f" {target.get('refund_amount') or ''}, delivered {target.get('delivered_on')}.")
            return self._record(iid, conclusion, {"action": action, "target_id": item_id} if action else None)
        return self._final(iid, r)

    def _payments(self, human: str, r: dict) -> tuple[list, str]:
        iid = self._issue(human)
        if "check_payment_consistency" not in r:
            return [("check_payment_consistency", {"issue_id": iid}), ("get_payment_summary", {"issue_id": iid})], ""
        if "record_findings" not in r:
            checks = r["check_payment_consistency"][0].get("checks", []) if isinstance(
                r["check_payment_consistency"][0], dict) else []
            disputed = re.search(r"charged|charge|billing", human.lower())
            target = checks[0]["order_id"] if checks else None
            proposal = {"action": "open_billing_review", "target_id": target} if (disputed and target) else None
            return self._record(iid, "The records show one payment per order; a duplicate cannot be confirmed "
                                     "or ruled out from them.", proposal)
        return self._final(iid, r)

    def _advisor(self, human: str, r: dict) -> tuple[list, str]:
        """Product advisor: one get_recommendations call, its result passed on unchanged."""
        iid = self._issue(human)
        if "get_recommendations" not in r:
            match = re.search(r"""['"]request['"]\s*:\s*['"]([^'"]*)""", human)
            query = (match.group(1) if match else "") or item_query(human) or ""
            return [("get_recommendations", {"issue_id": iid, "product_query": query})], ""
        result = r["get_recommendations"][0]
        if not isinstance(result, dict):
            return [], json.dumps({"issue_id": iid, "recommendations": [], "error": str(result)})
        keys = ("issue_id", "status", "reason", "recommendations", "message_hint")
        return [], json.dumps({**{k: result.get(k) for k in keys}, "error": None})

    def _stylist(self, human: str, r: dict) -> tuple[list, str]:
        """Personal stylist: read the picks; on "not for me", record it for the newest pick; pass the facts on."""
        iid = self._issue(human)
        if "get_stylist_picks" not in r:
            return [("get_stylist_picks", {"issue_id": iid})], ""
        raw = r["get_stylist_picks"][0]
        got = raw if isinstance(raw, dict) else {}
        picks = got.get("picks") or []
        if re.search(r"not for me", human, re.I) and picks and "pick_not_for_me" not in r:
            return [("pick_not_for_me", {"issue_id": iid, "product_id": picks[0]["product_id"]})], ""
        feedback = r.get("pick_not_for_me", [{}])[0]
        recorded = isinstance(feedback, dict) and feedback.get("status") == "recorded"
        return [], json.dumps({"issue_id": iid, "stylist_mode": got.get("stylist_mode"),
                               "picks": [] if recorded else picks, "explanation": None,
                               "feedback_recorded": recorded, "message_hint": got.get("message_hint"),
                               "closed": bool(got.get("closed") or recorded),
                               "error": None if got else str(raw)[:300]})

    def _loyalty(self, human: str, r: dict) -> tuple[list, str]:
        iid = self._issue(human)
        if "get_loyalty_profile" not in r:
            return [("get_loyalty_profile", {"issue_id": iid})], ""
        if "record_findings" not in r:
            p = r["get_loyalty_profile"][0] if isinstance(r["get_loyalty_profile"][0], dict) else {}
            return self._record(iid, f"{p.get('points')} points, {p.get('tier')} tier.", None)
        return self._final(iid, r)

    def _policy(self, human: str, r: dict) -> tuple[list, str]:
        iid = self._issue(human)
        if "evaluate_policy" not in r:
            confirmed = bool(re.search(r'"?customer_confirmed"?\s*[:=]\s*true', human, re.I))
            return [("evaluate_policy", {"issue_id": iid, "customer_confirmed": confirmed})], ""
        ev = r["evaluate_policy"][0] if isinstance(r["evaluate_policy"][0], dict) else {}
        return [], json.dumps({"issue_id": iid, **{k: ev.get(k) for k in ("recommendation", "kind", "missing",
                                                                         "reason", "explanation")},
                               "escalate_for_exception": False})


class NoModelLlm(BaseChatModel):
    """For checks that a path makes no model call (Orbit's clock ticks): every call is recorded and fails."""

    model_name: str = Field(default="none", alias="model")
    model_config = ConfigDict(populate_by_name=True)
    calls: ClassVar[list[str]] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "NoModelLlm":
        return self

    @property
    def _llm_type(self) -> str:
        return "no-model"

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        NoModelLlm.calls.append(str(messages[-1].content)[:80] if messages else "")
        raise RuntimeError("NoModelLlm: a model call was made")
