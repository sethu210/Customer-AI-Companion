"""
Typed result envelope for data reads, with bounded retries.

Agents must be able to tell "nothing there" from "could not look". Every read through CustomerDataAdapter.fetch
returns a ToolResult whose status is one of:

    ok                  rows were returned
    empty               the query ran and matched nothing (a valid answer, not an error)
    not_found           the caller asked for a specific record that is not on this customer's account
    timeout             the backend did not answer in time                      (retryable)
    permission_denied   credentials missing, expired or refused                 (not retryable; needs a person)
    backend_error       the backend failed; retryable only for transient causes

Retries happen here, bounded by the policy (max_retries, retry_backoff_s, tool_timeout_s), so no agent ever
loops on a failing tool. After the last attempt the result is returned as-is and the caller hands off.
"""

import asyncio
import logging
import subprocess
import urllib.error
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

LOGGER = logging.getLogger(__name__)

OK = "ok"
EMPTY = "empty"
NOT_FOUND = "not_found"
TIMEOUT = "timeout"
PERMISSION_DENIED = "permission_denied"
BACKEND_ERROR = "backend_error"
FAILURES = (TIMEOUT, PERMISSION_DENIED, BACKEND_ERROR)

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


@dataclass
class ToolResult:
    status: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    retryable: bool = False
    error: str | None = None  # short technical reason, for logs and case notes only
    attempts: int = 1

    @property
    def failed(self) -> bool:
        return self.status in FAILURES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(exc: BaseException) -> tuple[str, bool]:
    """Maps an exception from any backend to (status, retryable)."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TIMEOUT, True
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return PERMISSION_DENIED, False
        return BACKEND_ERROR, exc.code in TRANSIENT_HTTP
    if isinstance(exc, subprocess.CalledProcessError):
        return PERMISSION_DENIED, False  # `databricks auth token` failed: the CLI login has expired
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED, False
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return BACKEND_ERROR, True
    return BACKEND_ERROR, False


async def fetch_with_retries(call: Callable[[], Awaitable[list[dict[str, Any]]]], *, label: str,
                             max_retries: int, backoff_s: float, timeout_s: float) -> ToolResult:
    """Runs call() up to 1 + max_retries times; only timeouts and transient errors are retried."""
    attempts = 0
    while True:
        attempts += 1
        try:
            rows = await asyncio.wait_for(call(), timeout=timeout_s)
            return ToolResult(OK if rows else EMPTY, rows, attempts=attempts)
        except Exception as exc:  # noqa: BLE001 - every failure is reported through the envelope
            status, retryable = classify(exc)
            LOGGER.warning("%s attempt %d failed: %s (%s)", label, attempts, status, type(exc).__name__)
            if not retryable or attempts > max_retries:
                return ToolResult(status, [], retryable, f"{type(exc).__name__}: {str(exc)[:200]}", attempts)
            await asyncio.sleep(backoff_s * attempts)


def not_found(detail: str) -> ToolResult:
    return ToolResult(NOT_FOUND, [], False, detail)
