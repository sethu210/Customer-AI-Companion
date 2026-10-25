"""
Databricks adapter: Unity Catalog functions through the managed MCP server, with a SQL fallback.

Why it is built this way (measured on Free Edition, see test_data/README.md):
  * The first tool call of an MCP session can take 90 s or time out while Databricks provisions compute;
    later calls on the same session take 2-6 s. So ONE session is kept open per server process, on a
    dedicated background event loop, and warmed up at start.
  * If MCP does not answer within DATABRICKS_MCP_TIMEOUT_S, the same UC function is called through the
    SQL Statement API (1-5 s on a warm warehouse) with bound parameters. The MCP call keeps running in the
    background, which finishes warming the session for the next request.
  * The multi-agent read layer (identity, payments, loyalty, contact history, config) has no UC function, so it
    runs fixed, parameterised, read-only SELECTs (READ_QUERIES) over the existing silver tables through the
    same Statement API. Nothing is created or changed in Databricks.

Environment:
    DATABRICKS_HOST            https://<workspace>.cloud.databricks.com   (required)
    DATABRICKS_WAREHOUSE_ID    SQL warehouse for the fallback path        (required)
    DATABRICKS_CATALOG         catalog holding the agent_api schema       (default "workspace")
    DATABRICKS_TOKEN           PAT; if unset, the Databricks CLI OAuth login is used
    DATABRICKS_CLI_PATH        path to databricks.exe when it is not on PATH
    DATABRICKS_MCP_TIMEOUT_S   seconds to wait for MCP before falling back (default 12); 0 = SQL only, no MCP

Auth: with the CLI login, the token is replaced 5 minutes before its own expiry (the CLI may hand back a token
with only minutes left), and a request refused with HTTP 401/403 is retried once with a fresh token.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from coded_tools.customer_service.data_adapter import CustomerDataAdapter
from coded_tools.customer_service.data_adapter import Rows
from coded_tools.customer_service.schemas import normalise_rows
from coded_tools.customer_service.schemas import rows_from_columns

LOGGER = logging.getLogger(__name__)
SESSION_MAX_AGE_S = 45 * 60  # reconnect before an OAuth token (about 1 h) expires
TOKEN_MAX_AGE_S = 30 * 60
TOKEN_REFRESH_MARGIN_S = 5 * 60  # replace a token this long before its own expiry


def _seconds_left(expiry: str | None) -> float:
    """Seconds until the CLI's ISO expiry (e.g. 2026-10-20T11:13:11.5490485+01:00); TOKEN_MAX_AGE_S if unknown."""
    if not expiry:
        return TOKEN_MAX_AGE_S
    try:
        when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", expiry).replace("Z", "+00:00"))
    except ValueError:
        return TOKEN_MAX_AGE_S
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


class _TokenProvider:
    """The CLI hands back its stored OAuth token as is (it may have only minutes left), so the token is kept until
    TOKEN_REFRESH_MARGIN_S before its own expiry, at most TOKEN_MAX_AGE_S, and dropped when Databricks refuses it."""

    def __init__(self, host: str, token: str | None, cli_path: str | None) -> None:
        self.host = host
        self.static_token = token
        self.cli_path = cli_path or shutil.which("databricks") or "databricks"
        self._cached: tuple[str, float] | None = None  # token, monotonic time it must be replaced by
        self._lock = threading.Lock()

    def get(self) -> str:
        return self.get_with_deadline()[0]

    def get_with_deadline(self) -> tuple[str, float]:
        """The token and the monotonic time by which it must be replaced (inf for a static token)."""
        if self.static_token:
            return self.static_token, float("inf")
        with self._lock:
            if self._cached and time.monotonic() < self._cached[1]:
                return self._cached
            out = subprocess.run([self.cli_path, "auth", "token", "--host", self.host, "-o", "json"],
                                 capture_output=True, text=True, check=True, timeout=60)
            data = json.loads(out.stdout)
            usable_s = min(TOKEN_MAX_AGE_S, _seconds_left(data.get("expiry")) - TOKEN_REFRESH_MARGIN_S)
            if usable_s <= 0:
                # The CLI returned a token about to expire; ask it to refresh by forcing a new login token.
                out = subprocess.run([self.cli_path, "auth", "token", "--host", self.host, "--force-refresh",
                                      "-o", "json"], capture_output=True, text=True, check=True, timeout=60)
                data = json.loads(out.stdout)
                usable_s = min(TOKEN_MAX_AGE_S, _seconds_left(data.get("expiry")) - TOKEN_REFRESH_MARGIN_S)
            self._cached = (data["access_token"], time.monotonic() + max(usable_s, 0))
            return self._cached

    def invalidate(self, token: str) -> None:
        """Databricks refused this token: fetch a new one next time."""
        with self._lock:
            if self._cached and self._cached[0] == token:
                self._cached = None


class _McpSessionWorker:
    """Owns one MCP ClientSession on a private event loop thread; safe to use from any event loop."""

    def __init__(self, url: str, tokens: _TokenProvider) -> None:
        self.url = url
        self.tokens = tokens
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="databricks-mcp", daemon=True)
        self.thread.start()
        self._session: ClientSession | None = None
        self._opened_at = 0.0
        self._token_deadline = float("inf")  # the session's token must be replaced by then
        self._session_token = ""
        self._close_event: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None

    def submit(self, tool: str, args: dict[str, Any]) -> Future:
        return asyncio.run_coroutine_threadsafe(self._call(tool, args), self.loop)

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        session = await self._ensure_session()
        try:
            result = await session.call_tool(tool, args)
        except Exception:
            await self._reset()
            if self._session_token:
                self.tokens.invalidate(self._session_token)  # in case the failure was a refused token
            raise
        text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.isError:
            raise RuntimeError(text[:300])
        return json.loads(text) if text.strip() else {"columns": [], "rows": []}

    async def _ensure_session(self) -> ClientSession:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._session is not None and (time.monotonic() - self._opened_at > SESSION_MAX_AGE_S
                                              or time.monotonic() >= self._token_deadline):
                await self._reset()
            if self._session is None:
                ready: asyncio.Future = self.loop.create_future()
                self._close_event = asyncio.Event()
                self.loop.create_task(self._hold_session(ready, self._close_event))
                self._session = await ready
                self._opened_at = time.monotonic()
            return self._session

    async def _hold_session(self, ready: asyncio.Future, close_event: asyncio.Event) -> None:
        """Keeps the transport and session context managers open in a single task until reset."""
        held: ClientSession | None = None
        try:
            token, self._token_deadline = await asyncio.to_thread(self.tokens.get_with_deadline)
            self._session_token = token
            headers = {"Authorization": f"Bearer {token}"}
            async with streamablehttp_client(self.url, headers=headers, timeout=180) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    held = session
                    ready.set_result(session)
                    await close_event.wait()
        except BaseException as e:  # noqa: BLE001 - report any transport failure to the waiting caller
            if not ready.done():
                ready.set_exception(RuntimeError(f"MCP session failed: {e}"))
            LOGGER.warning("Databricks MCP session closed: %s", e)
        finally:
            if held is not None and self._session is held:
                self._session = None

    async def _reset(self) -> None:
        if self._close_event is not None:
            self._close_event.set()
        self._session = None


class DatabricksAdapter(CustomerDataAdapter):
    name = "databricks"

    def __init__(self, host: str, warehouse_id: str, catalog: str, tokens: _TokenProvider,
                 mcp_timeout_s: float, warm_up: bool = True) -> None:
        self.host = host.rstrip("/")
        self.warehouse_id = warehouse_id
        self.catalog = catalog
        self.tokens = tokens
        self.mcp_timeout_s = mcp_timeout_s
        self.tool_prefix = f"{catalog}__agent_api__"
        self.worker = _McpSessionWorker(f"{self.host}/api/2.0/mcp/functions/{catalog}/agent_api", tokens)
        self.stats = {"mcp": 0, "sql_fallback": 0}
        if warm_up and mcp_timeout_s > 0:
            # Fire and forget: provisions MCP function compute before the first real request.
            self.worker.submit(self.tool_prefix + "get_customer_profile", {"customer_id": "__warmup__"})

    @classmethod
    def from_env(cls) -> "DatabricksAdapter":
        return cls(
            host=os.environ["DATABRICKS_HOST"],
            warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"],
            catalog=os.environ.get("DATABRICKS_CATALOG", "workspace"),
            tokens=_TokenProvider(os.environ["DATABRICKS_HOST"].rstrip("/"), os.environ.get("DATABRICKS_TOKEN"),
                                  os.environ.get("DATABRICKS_CLI_PATH")),
            mcp_timeout_s=float(os.environ.get("DATABRICKS_MCP_TIMEOUT_S", "12")),
        )

    # ------------------------------------------------------------------ transport

    async def _run(self, function: str, args: dict[str, str], prefer_sql: bool = False) -> Rows:
        if not prefer_sql and self.mcp_timeout_s > 0:
            future = asyncio.wrap_future(self.worker.submit(self.tool_prefix + function, args))
            try:
                data = await asyncio.wait_for(asyncio.shield(future), timeout=self.mcp_timeout_s)
                self.stats["mcp"] += 1
                return normalise_rows(function, rows_from_columns(data.get("columns", []), data.get("rows", [])))
            except Exception as e:  # noqa: BLE001 - any MCP problem falls back to SQL
                LOGGER.info("MCP %s unavailable (%s); using SQL fallback", function, type(e).__name__)
        rows = await asyncio.to_thread(self._sql_call, function, args)
        self.stats["sql_fallback"] += 1
        return normalise_rows(function, rows)

    def _sql_call(self, function: str, args: dict[str, str], schema: str = "agent_api") -> list[dict[str, Any]]:
        placeholders = ", ".join(f":{name}" for name in args)
        return self._execute(function, f"SELECT * FROM {self.catalog}.{schema}.{function}({placeholders})", args)

    def _execute(self, label: str, statement: str, args: dict[str, str]) -> list[dict[str, Any]]:
        body = {
            "warehouse_id": self.warehouse_id,
            "statement": statement,
            "parameters": [{"name": k, "value": v, "type": "STRING"} for k, v in args.items()],
            "wait_timeout": "30s",
        }
        res = self._api("POST", "/api/2.0/sql/statements", body)
        while res["status"]["state"] in ("PENDING", "RUNNING"):
            time.sleep(1)
            res = self._api("GET", f"/api/2.0/sql/statements/{res['statement_id']}")
        if res["status"]["state"] != "SUCCEEDED":
            raise RuntimeError(f"{label} failed: {res['status'].get('error', {}).get('message', '')[:300]}")
        columns = [c["name"] for c in res["manifest"]["schema"]["columns"]]
        return rows_from_columns(columns, (res.get("result") or {}).get("data_array") or [])

    async def _read(self, name: str, args: dict[str, str]) -> Rows:
        """Runs one fixed statement from READ_QUERIES; only bound parameter values vary."""
        statement = READ_QUERIES[name].format(c=self.catalog)
        rows = await asyncio.to_thread(self._execute, name, statement, args)
        self.stats["sql_read"] = self.stats.get("sql_read", 0) + 1
        return normalise_rows(name, rows)

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in (1, 2):
            token = self.tokens.get()
            req = urllib.request.Request(
                self.host + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403) or attempt == 2:
                    raise
                # A refused token is usually an expired one: retry once with a fresh token.
                LOGGER.warning("Databricks refused the token (HTTP %d); retrying with a fresh one", e.code)
                self.tokens.invalidate(token)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ the five functions

    async def get_customer_profile(self, customer_id: str, prefer_sql: bool = False) -> Rows:
        return await self._run("get_customer_profile", {"customer_id": customer_id}, prefer_sql)

    async def get_customer_context(self, customer_id: str, prefer_sql: bool = False) -> Rows:
        return await self._run("get_customer_context", {"customer_id": customer_id}, prefer_sql)

    async def find_order_items(self, customer_id: str, item_query: str, prefer_sql: bool = False) -> Rows:
        return await self._run("find_order_items", {"customer_id": customer_id, "item_query": item_query}, prefer_sql)

    async def get_case_timeline(self, customer_id: str, order_id: str, prefer_sql: bool = False) -> Rows:
        return await self._run("get_case_timeline", {"customer_id": customer_id, "order_id": order_id}, prefer_sql)

    async def get_stuck_cases(self, customer_id: str, prefer_sql: bool = False) -> Rows:
        return await self._run("get_stuck_cases", {"customer_id": customer_id}, prefer_sql)

    async def get_policy_signals(self, customer_id: str, prefer_sql: bool = True) -> Rows:
        # Always SQL: policy_api is deliberately not published on the agent's MCP endpoint.
        rows = await asyncio.to_thread(self._sql_call, "get_policy_signals", {"customer_id": customer_id},
                                       "policy_api")
        return normalise_rows("get_policy_signals", rows)

    # ------------------------------------------------------------------ multi-agent read layer (read-only SQL)

    async def resolve_channel_identity(self, channel: str, identifier: str) -> Rows:
        return await self._read("resolve_channel_identity", {"channel": channel, "identifier": identifier or ""})

    async def get_payment_summary(self, customer_id: str) -> Rows:
        return await self._read("get_payment_summary", {"customer_id": customer_id})

    async def get_loyalty_profile(self, customer_id: str) -> Rows:
        return await self._read("get_loyalty_profile", {"customer_id": customer_id})

    async def get_contact_history(self, customer_id: str) -> Rows:
        return await self._read("get_contact_history", {"customer_id": customer_id})

    async def get_reference_config(self) -> Rows:
        return await self._read("get_reference_config", {})

    async def get_catalog_variants(self) -> Rows:
        return await self._read("get_catalog_variants", {})

    async def get_product_relations(self) -> Rows:
        return await self._read("get_product_relations", {})

    async def get_product_copurchase(self) -> Rows:
        return await self._read("get_product_copurchase", {})

    async def get_product_launches(self) -> Rows:
        return await self._read("get_product_launches", {})

    async def get_product_styles(self) -> Rows:
        return await self._read("get_product_styles", {})

    async def get_colour_families(self) -> Rows:
        return await self._read("get_colour_families", {})

    async def get_colour_harmony(self) -> Rows:
        return await self._read("get_colour_harmony", {})

    async def get_purchase_history(self, customer_id: str) -> Rows:
        return await self._read("get_purchase_history", {"customer_id": customer_id})

    async def list_customers(self) -> Rows:
        return await self._read("list_customers", {})


# Fixed, read-only statements over the existing silver tables and the product catalog schema. They create nothing
# in Databricks.
# {c} is the catalog from the environment; every value that varies is a bound :parameter, and no text from
# the LLM or the customer is ever placed into a statement. Mirrored by mock_adapter.MockAdapter.
_CFG = "cfg AS (SELECT to_date(value) AS as_of FROM {c}.silver.config WHERE key = 'reference_date')"
READ_QUERIES: dict[str, str] = {
    "resolve_channel_identity": """
        SELECT identity_id, customer_id, channel, verified, auth_level
        FROM {c}.silver.channel_identities
        WHERE channel = :channel AND lower(identifier) = lower(trim(:identifier))
        ORDER BY identity_id
        LIMIT 10""",
    "get_payment_summary": """
        WITH """ + _CFG + """,
        f AS (
          SELECT order_id, count(*) AS refund_count,
                 round(coalesce(sum(CASE WHEN status = 'completed' THEN amount END), 0.0), 2) AS completed,
                 round(coalesce(sum(CASE WHEN status IN ('pending', 'processing') THEN amount END), 0.0), 2) AS open_total,
                 round(coalesce(sum(CASE WHEN status = 'failed' THEN amount END), 0.0), 2) AS failed
          FROM {c}.silver.refunds GROUP BY order_id
        ),
        p AS (
          SELECT order_id, payment_id, status, last4, count(*) OVER (PARTITION BY order_id) AS n
          FROM {c}.silver.payments
        )
        SELECT o.order_id, to_date(o.placed_at) AS placed_on, o.status AS order_status, o.total AS order_total,
               o.currency, p.payment_id, p.status AS payment_status, p.last4 AS payment_last4,
               coalesce(p.n, 0) AS payments_on_order, coalesce(f.refund_count, 0) AS refund_count,
               coalesce(f.completed, 0.0) AS refunds_completed_total, coalesce(f.open_total, 0.0) AS refunds_open_total,
               coalesce(f.failed, 0.0) AS refunds_failed_total, cfg.as_of
        FROM {c}.silver.orders o
        CROSS JOIN cfg
        LEFT JOIN p ON p.order_id = o.order_id
        LEFT JOIN f ON f.order_id = o.order_id
        WHERE o.customer_id = :customer_id
        ORDER BY o.placed_at DESC, o.order_id, p.payment_id
        LIMIT 200""",
    "get_loyalty_profile": """
        WITH """ + _CFG + """
        SELECT c.customer_id, l.points, l.tier AS loyalty_tier, c.tier AS customer_tier,
               to_date(c.created_at) AS member_since, l.last_adjustment_at, cfg.as_of
        FROM {c}.silver.customers c
        CROSS JOIN cfg
        LEFT JOIN {c}.silver.loyalty l ON l.customer_id = c.customer_id
        WHERE c.customer_id = :customer_id""",
    "get_contact_history": """
        WITH """ + _CFG + """
        SELECT s.contact_id, s.order_id, s.channel, s.topic, s.notes, s.created_at, s.resolution, cfg.as_of
        FROM {c}.silver.support_contacts s
        CROSS JOIN cfg
        WHERE s.customer_id = :customer_id
        ORDER BY s.created_at DESC, s.contact_id
        LIMIT 20""",
    "get_reference_config": """
        SELECT key, value FROM {c}.silver.config ORDER BY key""",
    # Product catalog: its own schema (test_data/databricks/catalog_tables.sql), no customer data.
    "get_catalog_variants": """
        SELECT v.sku, p.product_id, p.title, p.category, p.description, p.material, p.fit, p.season,
               v.color, v.size, p.list_price, p.currency, p.active, v.stock
        FROM {c}.catalog.product_variants v
        JOIN {c}.catalog.products p ON p.product_id = v.product_id
        ORDER BY p.product_id, v.sku""",
    "get_product_relations": """
        SELECT relation_id, product_id, related_product_id, relation, strength
        FROM {c}.catalog.product_relations
        ORDER BY product_id, strength DESC, related_product_id""",
    "get_product_copurchase": """
        SELECT product_id, related_product_id, orders_together, orders_with_product
        FROM {c}.catalog.product_copurchase
        ORDER BY product_id, orders_together DESC, related_product_id""",
    # Stylist data (companion plan P4): same catalog schema, no customer data.
    "get_product_launches": """
        SELECT launch_id, product_id, planned_at, headline
        FROM {c}.catalog.product_launches
        ORDER BY planned_at, launch_id""",
    "get_product_styles": """
        SELECT product_id, style FROM {c}.catalog.product_styles ORDER BY product_id, style""",
    "get_colour_families": """
        SELECT color, family FROM {c}.catalog.colour_families ORDER BY color""",
    "get_colour_harmony": """
        SELECT family, other_family, strength FROM {c}.catalog.colour_harmony ORDER BY family, other_family""",
    "get_purchase_history": """
        WITH """ + _CFG + """
        SELECT i.item_id, i.order_id, to_date(o.placed_at) AS placed_on, o.status AS order_status, i.sku, i.title,
               i.category, i.color, i.size, i.item_status, r.reason AS return_reason, r.status AS return_status,
               cfg.as_of
        FROM {c}.silver.order_items i
        JOIN {c}.silver.orders o ON o.order_id = i.order_id
        CROSS JOIN cfg
        LEFT JOIN {c}.silver.returns r ON r.item_id = i.item_id
        WHERE o.customer_id = :customer_id
        ORDER BY o.placed_at DESC, i.item_id, r.status
        LIMIT 200""",
    # Demo console only (customer picker), not an agent tool.
    "list_customers": """
        SELECT c.customer_id, c.full_name, c.tier, i.identifier, i.verified, i.auth_level,
               (SELECT count(*) FROM {c}.silver.orders o WHERE o.customer_id = c.customer_id) AS order_count
        FROM {c}.silver.customers c
        LEFT JOIN {c}.silver.channel_identities i ON i.customer_id = c.customer_id AND i.channel = 'web_session'
        ORDER BY c.customer_id, i.identity_id""",
}
