"""
Customer-data adapter interface and factory.

Every adapter implements the same read functions and returns rows normalised by schemas.normalise_rows:
  * the five agent functions in test_data/databricks/uc_functions.sql (MCP on Databricks) plus get_policy_signals;
  * the multi-agent read layer: fixed, parameterised, read-only SELECTs over the existing silver tables;
  * the product catalog (<catalog>.catalog schema), for recommendations and the stylist data (launches, style
    tags, colour families and harmony): no customer data.
Nothing here writes to the data, and no SQL text ever comes from the LLM.

Backend selection (environment):
    CUSTOMER_DATA_BACKEND = mock (default) | databricks
    EVENT_SIMULATOR = 1    shows the local event simulator's events on top (simulator.py); the data never changes
"""

import os
from abc import ABC
from abc import abstractmethod
from typing import Any

from coded_tools.customer_service.results import ToolResult
from coded_tools.customer_service.results import fetch_with_retries

Rows = list[dict[str, Any]]


class CustomerDataAdapter(ABC):
    """Read-only access to one customer's data. customer_id always comes from the caller, never the LLM."""

    name: str = "abstract"

    @abstractmethod
    async def get_customer_profile(self, customer_id: str) -> Rows:
        """Profile row (zero or one)."""

    @abstractmethod
    async def get_customer_context(self, customer_id: str) -> Rows:
        """One row per ordered item with order, shipment, return, refund and payment status."""

    @abstractmethod
    async def find_order_items(self, customer_id: str, item_query: str) -> Rows:
        """Best-matching items for a short description."""

    @abstractmethod
    async def get_case_timeline(self, customer_id: str, order_id: str) -> Rows:
        """Chronological events for one of the customer's orders."""

    @abstractmethod
    async def get_stuck_cases(self, customer_id: str) -> Rows:
        """Returns, refunds and shipments that are stuck."""

    @abstractmethod
    async def get_policy_signals(self, customer_id: str) -> Rows:
        """Internal risk/history signals for the policy engine. Never passed to the LLM."""

    # ---------------------------------------------------------------- multi-agent read layer (read-only SQL)

    @abstractmethod
    async def resolve_channel_identity(self, channel: str, identifier: str) -> Rows:
        """channel_identities rows matching the gateway's channel and identifier (zero, one or several)."""

    @abstractmethod
    async def get_payment_summary(self, customer_id: str) -> Rows:
        """One row per order and payment: order total, payment status, card ending and refund totals."""

    @abstractmethod
    async def get_loyalty_profile(self, customer_id: str) -> Rows:
        """Points and both tier fields (loyalty.tier and customers.tier)."""

    @abstractmethod
    async def get_contact_history(self, customer_id: str) -> Rows:
        """The 20 most recent support contacts, newest first. notes is untrusted free text."""

    @abstractmethod
    async def get_reference_config(self) -> Rows:
        """The data's config table (key, value): reference_date and the thresholds the data was built with."""

    # ---------------------------------------------------------------- product catalog (read-only, no customer data)

    @abstractmethod
    async def get_catalog_variants(self) -> Rows:
        """Every product variant (SKU) with its product's details, list price, active flag and stock."""

    @abstractmethod
    async def get_product_relations(self) -> Rows:
        """Hand-set relations between products: complement, alternative or upgrade, with a 0-1 strength."""

    @abstractmethod
    async def get_product_copurchase(self) -> Rows:
        """Per product pair: orders containing both, and orders containing the first (not cancelled orders)."""

    # ---------------------------------------------------------------- stylist data (catalog schema, no customer data)

    @abstractmethod
    async def get_product_launches(self) -> Rows:
        """Products staged for launch (inactive until launched) with the planned launch time, soonest first."""

    @abstractmethod
    async def get_product_styles(self) -> Rows:
        """1-3 style tags per product from a fixed list (smart, casual, classic, ...)."""

    @abstractmethod
    async def get_colour_families(self) -> Rows:
        """The colour family of every catalog colour (navy -> blue, camel -> earth, ...)."""

    @abstractmethod
    async def get_colour_harmony(self) -> Rows:
        """How well two colour families go together, 0-1; one row per unordered pair (family <= other_family)."""

    @abstractmethod
    async def get_purchase_history(self, customer_id: str) -> Rows:
        """One row per ordered item (and return) with its SKU and return reason, newest first."""

    async def list_customers(self) -> Rows:
        """Demo console only (customer picker), never an agent tool: every customer with its website identities."""
        raise NotImplementedError(f"{self.name} has no customer list")

    async def fetch(self, function: str, *args: Any, policy: dict[str, Any] | None = None) -> ToolResult:
        """Calls one read function and wraps the outcome in a ToolResult, retrying transient failures."""
        policy = policy or {}
        return await fetch_with_retries(
            lambda: getattr(self, function)(*args), label=f"{self.name}.{function}",
            max_retries=int(policy.get("max_retries", 2)), backoff_s=float(policy.get("retry_backoff_s", 1.0)),
            timeout_s=float(policy.get("tool_timeout_s", 90)))


_ADAPTER: CustomerDataAdapter | None = None


def get_adapter() -> CustomerDataAdapter:
    """Process-wide adapter, created on first use so the Databricks session is shared across tool calls."""
    global _ADAPTER  # noqa: PLW0603 - deliberate per-process singleton
    if _ADAPTER is None:
        backend = os.environ.get("CUSTOMER_DATA_BACKEND", "mock").strip().lower()
        if backend == "databricks":
            from coded_tools.customer_service.databricks_adapter import DatabricksAdapter

            _ADAPTER = DatabricksAdapter.from_env()
        elif backend == "mock":
            from coded_tools.customer_service.mock_adapter import MockAdapter

            _ADAPTER = MockAdapter.from_env()
        else:
            raise ValueError(f"Unknown CUSTOMER_DATA_BACKEND {backend!r}; use 'mock' or 'databricks'")
        faults = os.environ.get("CUSTOMER_DATA_FAULTS", "").strip()
        if faults:  # test-only failure simulation; see fault_injection.py
            from coded_tools.customer_service.fault_injection import FaultInjectingAdapter
            from coded_tools.customer_service.fault_injection import parse_faults

            _ADAPTER = FaultInjectingAdapter(_ADAPTER, parse_faults(faults))
        from coded_tools.customer_service import simulator

        if simulator.enabled():  # local business events on top of the data (Resolution Guardian demo and tests)
            _ADAPTER = simulator.SimulatedAdapter(_ADAPTER)
    return _ADAPTER
