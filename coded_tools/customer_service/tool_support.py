"""
Shared plumbing for the multi-agent network's coded tools.

Every tool builds a ToolContext from sly_data only: identity (resolved in code), the case id (kept in sly_data
and returned to the gateway so later turns continue the same case), the policy and the stores. Tool arguments
from the LLM never decide who the customer is or which case is used.
"""

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coded_tools.customer_service.case_store import CaseError
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.identity import AUTH_LEVEL
from coded_tools.customer_service.identity import CHANNEL
from coded_tools.customer_service.identity import CUSTOMER_ID
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.identity import ensure_identity
from coded_tools.customer_service.policy import load_policy

CASE_ID = "case_id"  # sly_data key; the only key returned to the gateway (allow.to_upstream)
# Set by the server on the first tool call of a request and shared by every agent in that request. It is not
# returned to the gateway, so the next customer message always gets a new one: "a later turn" can be checked.
TURN_ID = "turn_id"

_STORES: dict[str, CaseStore] = {}


def case_store() -> CaseStore:
    """One CaseStore per database path per process (the path can change between tests)."""
    path = os.environ.get("CASE_STORE_PATH", "")
    if path not in _STORES:
        _STORES[path] = CaseStore(Path(path) if path else None)
    return _STORES[path]


class ToolError(Exception):
    """Reported to the agent as 'Error: ...'; nothing was changed."""


def error(message: str) -> str:
    return message if message.startswith("Error:") else f"Error: {message}"


def compact(rows: list[dict[str, Any]], keep: tuple[str, ...] | None = None,
            drop: tuple[str, ...] = ("as_of",)) -> list[dict[str, Any]]:
    """Drops nulls (and the listed columns), optionally keeping only `keep`, to keep agent context small."""
    return [{k: v for k, v in row.items() if v is not None and k not in drop and (keep is None or k in keep)}
            for row in rows]


@dataclass
class ToolContext:
    sly_data: dict[str, Any]
    adapter: CustomerDataAdapter
    policy: dict[str, Any]
    store: CaseStore
    identity: dict[str, Any]

    @property
    def customer_id(self) -> str | None:
        return self.sly_data.get(CUSTOMER_ID) if self.identity["can_read_account"] else None

    @property
    def channel(self) -> str | None:
        return self.sly_data.get(CHANNEL)

    @property
    def auth_level(self) -> str | None:
        return self.sly_data.get(AUTH_LEVEL)

    @property
    def case_id(self) -> str | None:
        return self.sly_data.get(CASE_ID)

    @property
    def turn_id(self) -> str:
        return self.sly_data[TURN_ID]

    def require_customer(self) -> str:
        if self.identity["identity_status"] not in IDENTIFIED or not self.customer_id:
            raise ToolError(f"identity_unavailable. {self.identity['message_hint']}")
        return self.customer_id

    def require_case(self) -> dict[str, Any]:
        """The current case, which must belong to the current customer (or be an unidentified contact)."""
        case = self.store.get_case(self.case_id) if self.case_id else None
        if case is None:
            raise ToolError("no case is open for this conversation; the intake agent must open one first.")
        if case["customer_id"] != self.customer_id:
            raise ToolError("this case does not belong to the current customer.")
        return case

    def require_issue(self, issue_id: Any, domains: tuple[str, ...] | None = None) -> dict[str, Any]:
        case = self.require_case()
        try:
            issue = self.store.get_issue(case["case_id"], str(issue_id or "").strip())
        except CaseError as e:
            raise ToolError(str(e)) from e
        if domains and issue["domain"] not in domains:
            raise ToolError(f"issue {issue['issue_id']} belongs to {issue['domain']}, not to this agent.")
        return issue


async def tool_context(sly_data: dict[str, Any]) -> ToolContext:
    sly_data.setdefault(TURN_ID, uuid.uuid4().hex)
    policy = load_policy()
    adapter = get_adapter()
    identity = await ensure_identity(sly_data, adapter, policy)
    return ToolContext(sly_data, adapter, policy, case_store(), identity)


async def read(ctx: ToolContext, function: str, *args: Any) -> list[dict[str, Any]]:
    """One adapter read through the envelope; a failure becomes a ToolError the agent must not guess around."""
    result = await ctx.adapter.fetch(function, *args, policy=ctx.policy)
    if result.failed:
        raise ToolError(f"customer_data_unavailable ({result.status}). Do not guess; tell the orchestrator so the "
                        "issue can be handed to a person.")
    return result.rows
