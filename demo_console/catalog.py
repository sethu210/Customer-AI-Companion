"""
The customers the demo can sign in as: every customer with a verified website identity in the active backend
(CUSTOMER_DATA_BACKEND, Databricks for demos), with the evaluation scenarios pinned on top. Website sign-in only:
scenarios whose script uses another channel (S03, S12 by email) are offered through the website as well;
S18 exists only to show an ambiguous shared phone, and R4 only to show that a weak phone sign-in gets no product
suggestions, so both are left out while phone is not offered.
"""

import asyncio
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from coded_tools.customer_service.data_adapter import get_adapter
from evaluation.expectations import Conversation
from evaluation.expectations import conversations

WEBSITE = "web_session"
PHONE_ONLY_SCENARIOS = {"S18", "R4"}
CLASS_LABELS = {"info": "Answer", "action": "Fix it", "deny": "Explain no", "escalate": "Hand to a person",
                "identity": "Not identified"}


@dataclass
class Customer:
    customer_id: str
    name: str
    tier: str
    identifier: str          # the website session identifier the gateway sends
    auth_level: str
    order_count: int
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """What the page may see. The identifier is shown in the sign-in preview: it is test data."""
        return {"customer_id": self.customer_id, "name": self.name, "tier": self.tier, "identifier": self.identifier,
                "auth_level": self.auth_level, "order_count": self.order_count, "scenarios": self.scenarios}


def scenario_info(conv: Conversation) -> dict[str, Any]:
    return {"id": conv.id, "title": conv.title, "cls": conv.cls, "outcome": CLASS_LABELS[conv.cls],
            "messages": conv.messages}


def demo_scenarios() -> list[Conversation]:
    return [c for c in conversations() if c.id not in PHONE_ONLY_SCENARIOS]


class Catalog:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        by_id: dict[str, Customer] = {}
        for r in rows:
            if not r.get("identifier") or not r.get("verified") or r["customer_id"] in by_id:
                continue  # first verified website identity per customer
            by_id[r["customer_id"]] = Customer(r["customer_id"], r.get("full_name") or r["customer_id"],
                                               r.get("tier") or "", r["identifier"], r.get("auth_level") or "weak",
                                               r.get("order_count") or 0)
        for conv in demo_scenarios():
            if conv.customer in by_id:
                by_id[conv.customer].scenarios.append(scenario_info(conv))
        self.customers = by_id

    @classmethod
    def load(cls) -> "Catalog":
        return cls(asyncio.run(get_adapter().list_customers()))

    @classmethod
    async def aload(cls) -> "Catalog":
        return cls(await get_adapter().list_customers())

    def get(self, customer_id: str) -> Customer:
        if customer_id not in self.customers:
            raise KeyError(f"no customer {customer_id} with a verified website sign-in")
        return self.customers[customer_id]

    def search(self, query: str = "", kind: str = "all", offset: int = 0, limit: int = 60) -> dict[str, Any]:
        """kind: all | scenario | other. Scenario customers come first, in scenario order."""
        order: dict[str, int] = {}
        for n, c in enumerate(demo_scenarios()):
            order.setdefault(c.customer, n)  # a customer with two scenarios sorts by the first (S03 before M1)
        q = query.strip().lower()
        found = [c for c in self.customers.values()
                 if (kind == "all" or (kind == "scenario") == bool(c.scenarios))
                 and (not q or q in f"{c.name} {c.customer_id} {' '.join(s['id'] + ' ' + s['title'] for s in c.scenarios)}".lower())]
        found.sort(key=lambda c: (order.get(c.customer_id, 10_000), c.customer_id))
        return {"total": len(found), "customers": [c.public() for c in found[offset:offset + limit]]}

    def gateway_sly_data(self, customer_id: str) -> dict[str, Any]:
        """Exactly what a website gateway sends for a signed-in visitor: the raw channel identity, never an id."""
        return {"channel": WEBSITE, "identifier": self.get(customer_id).identifier}
