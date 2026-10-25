"""
Databricks auth handling, offline (no CLI, no network): the CLI token is replaced before its own expiry, a refused
token (HTTP 401/403) is retried once with a fresh one, and DATABRICKS_MCP_TIMEOUT_S=0 means SQL only.
"""

import asyncio
import io
import json
import subprocess
import urllib.error
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from coded_tools.customer_service import databricks_adapter as dba


def _cli_time(when: datetime) -> str:
    """The CLI's expiry format (7 fraction digits), keeping the real fraction so tests have exact bounds."""
    return f"{when:%Y-%m-%dT%H:%M:%S}.{when.microsecond:06d}0+01:00"


def _cli(monkeypatch, *expires_in_s: float) -> list[list[str]]:
    """Fakes `databricks auth token`: each call returns the next token, expiring after the given seconds."""
    calls: list[list[str]] = []

    def run(args, **_):
        n = len(calls)
        calls.append(args)
        when = datetime.now(timezone(timedelta(hours=1))) + timedelta(seconds=expires_in_s[n])
        expiry = _cli_time(when)
        return subprocess.CompletedProcess(args, 0, json.dumps({"access_token": f"tok{n}", "expiry": expiry}), "")

    monkeypatch.setattr(dba.subprocess, "run", run)
    return calls


def test_seconds_left_reads_the_cli_expiry_format():
    soon = datetime.now(timezone(timedelta(hours=1))) + timedelta(minutes=10)
    text = _cli_time(soon)
    assert 590 < dba._seconds_left(text) <= 600
    assert dba._seconds_left(None) == dba.TOKEN_MAX_AGE_S
    assert dba._seconds_left("not a date") == dba.TOKEN_MAX_AGE_S


def test_a_nearly_expired_cli_token_is_force_refreshed(monkeypatch):
    calls = _cli(monkeypatch, 4 * 60, 3600)  # the CLI first hands back a token with 4 minutes left
    tokens = dba._TokenProvider("https://h", None, "databricks")
    assert tokens.get() == "tok1"
    assert "--force-refresh" in calls[1]


def test_a_token_is_kept_only_until_shortly_before_its_expiry(monkeypatch):
    calls = _cli(monkeypatch, 12 * 60, 3600)  # 12 minutes left -> usable for 7
    tokens = dba._TokenProvider("https://h", None, "databricks")
    token, deadline = tokens.get_with_deadline()
    assert token == "tok0" and len(calls) == 1
    assert 6 * 60 < deadline - dba.time.monotonic() <= 7 * 60
    assert tokens.get() == "tok0" and len(calls) == 1  # cached while usable
    monkeypatch.setattr(dba.time, "monotonic", lambda: deadline + 1)
    assert tokens.get() == "tok1" and "--force-refresh" not in calls[1]


def test_a_refused_token_is_retried_once_with_a_fresh_one(monkeypatch):
    _cli(monkeypatch, 3600, 3600, 3600)
    adapter = dba.DatabricksAdapter("https://h", "wh", "workspace", dba._TokenProvider("https://h", None, "db"),
                                    mcp_timeout_s=0)
    seen = []

    def urlopen(req, timeout):
        seen.append(req.headers["Authorization"])
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(dba.urllib.request, "urlopen", urlopen)
    assert adapter._api("GET", "/x") == {"ok": True}
    assert seen == ["Bearer tok0", "Bearer tok1"]


def test_a_second_refusal_is_reported(monkeypatch):
    _cli(monkeypatch, 3600, 3600, 3600)
    adapter = dba.DatabricksAdapter("https://h", "wh", "workspace", dba._TokenProvider("https://h", None, "db"),
                                    mcp_timeout_s=0)

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(dba.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        adapter._api("GET", "/x")


def test_mcp_timeout_zero_uses_sql_only(monkeypatch):
    adapter = dba.DatabricksAdapter("https://h", "wh", "workspace", dba._TokenProvider("https://h", "pat", None),
                                    mcp_timeout_s=0)
    monkeypatch.setattr(adapter.worker, "submit", lambda *a: pytest.fail("MCP must not be called"))
    monkeypatch.setattr(adapter, "_sql_call", lambda function, args: [])
    asyncio.run(adapter.get_stuck_cases("CUST-1"))
    assert adapter.stats == {"mcp": 0, "sql_fallback": 1}
