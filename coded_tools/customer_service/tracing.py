"""
Business-level tracing spans for the customer service code (action layer, policy, hand-offs, reviews).

Uses only the OpenTelemetry API, which is already installed. Without an SDK the spans are no-ops and cost
nothing; when the Phoenix plugin is enabled (config/plugins.hocon, PHOENIX_ENABLED=true) its tracer provider
exports them next to Neuro SAN's own LLM and agent spans, so one trace shows the conversation, the decision and
the outcome.

Only allow-listed attributes are recorded: ids of cases, issues and actions, outcomes and reason codes. Never
customer ids, names, contact details, risk flags or free text.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_TRACER = trace.get_tracer("customer_service")

SAFE_ATTRIBUTES = {
    "case_id", "issue_id", "domain", "action", "target_id", "outcome", "reason", "decision", "recommendation",
    "kind", "reference", "review_id", "handoff_id", "mode", "identity_status", "policy_version", "status",
    "reviewer_role",
}


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drops anything not allow-listed and anything that is not a plain scalar."""
    return {f"cs.{k}": v for k, v in attributes.items()
            if k in SAFE_ATTRIBUTES and isinstance(v, (str, int, float, bool)) and v != ""}


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """with span("action_layer.submit", case_id=..., action=...) as s: ... s.set_attribute(...)"""
    with _TRACER.start_as_current_span(f"customer_service.{name}") as current:
        for key, value in safe_attributes(attributes).items():
            current.set_attribute(key, value)
        yield current


def record(current: Any, **attributes: Any) -> None:
    """Adds allow-listed attributes to an open span (e.g. the outcome, once known)."""
    for key, value in safe_attributes(attributes).items():
        current.set_attribute(key, value)
