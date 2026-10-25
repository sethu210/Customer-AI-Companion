r"""
The assistant gateway's MCP server (streamable HTTP). Run: .\scripts\assistant_gateway.ps1  (or python -m
assistant_gateway); the agent server must be running for ask_support.

Every tool call: the bearer token is checked (GrantStore.verify; unknown, expired and revoked tokens are refused by
the MCP layer), then Gateway checks the scope and the rate limits, runs the tool for the grant's customer only,
and writes the audit row. Tool arguments can never name a customer.

Tools (all need scope read_status in E2):
    get_case_status(case_id?)        the customer's cases: requests, statuses, references, next steps
    get_order_status(order_ref?)     orders, shipments, returns and refunds
    ask_support(message, case_id?)   one support turn in the background; returns a request id at once
    get_answer(request_id)           the reply and the case record once the turn is done
"""

import asyncio
import contextlib
import os
import time
from typing import Any
from typing import AsyncIterator

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.auth.settings import RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from assistant_gateway import activity
from assistant_gateway import signin
from assistant_gateway import support
from assistant_gateway.oauth import GRANTABLE_SCOPES
from assistant_gateway.oauth import AcmeOAuth
from assistant_gateway.grants import GrantStore
from assistant_gateway.views import case_status
from assistant_gateway.views import order_status
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.policy import load_policy

MAX_MESSAGE = 2000


class GatewayError(Exception):
    """A refused call; the message is safe to show to the assistant."""


class Gateway:
    """The rules, independent of MCP: scope, rate limits, ownership, audit."""

    def __init__(self, store: GrantStore | None = None, policy: dict[str, Any] | None = None,
                 agent_url: str | None = None) -> None:
        self.store = store or GrantStore()
        self.policy = policy or load_policy()
        self.settings = self.policy["assistant"]
        self.agent_url = agent_url or os.environ.get("AGENT_SERVER_URL", "http://localhost:8080")
        self.tasks: set[asyncio.Task] = set()

    def _check(self, grant: dict[str, Any] | None, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if grant is None:
            raise GatewayError("Not authorised: connect the assistant to ACME Retail again.")
        allowed = {t for s in grant["scopes"] for t in self.settings["scopes"].get(s, [])}
        if tool not in allowed:
            self.store.audit(grant, tool, args, "refused: scope")
            raise GatewayError(f"This assistant was not given access to {tool}.")
        if self.store.calls_since(grant["grant_id"], time.time() - 60) >= self.settings["calls_per_minute"]:
            self.store.audit(grant, tool, args, "refused: rate limit")
            raise GatewayError("Too many requests from this assistant; wait a minute and try again.")
        return grant

    def _own_case(self, grant: dict[str, Any], case_id: str | None) -> str | None:
        if not case_id:
            return None
        row = CaseStore().get_case(case_id.strip().upper())
        if row is None or row["customer_id"] != grant["customer_id"]:
            raise GatewayError("No case with that reference on this account.")
        return row["case_id"]

    async def get_case_status(self, grant: dict[str, Any] | None, case_id: str | None = None) -> dict[str, Any]:
        args = {"case_id": case_id}
        grant = self._check(grant, "get_case_status", args)
        result = case_status(CaseStore(), grant["customer_id"], case_id)
        self.store.audit(grant, "get_case_status", args, "ok" if result["found"] else "not found")
        activity.status_read(grant, "get_case_status", result["found"])
        return result

    async def get_order_status(self, grant: dict[str, Any] | None, order_ref: str | None = None) -> dict[str, Any]:
        args = {"order_ref": order_ref}
        grant = self._check(grant, "get_order_status", args)
        read = await get_adapter().fetch("get_customer_context", grant["customer_id"], policy=self.policy)
        if read.failed:
            self.store.audit(grant, "get_order_status", args, f"failed: {read.status}")
            raise GatewayError("Order data is not available right now; try again later.")
        result = order_status(read.rows, order_ref)
        self.store.audit(grant, "get_order_status", args, "ok" if result["found"] else "not found")
        activity.status_read(grant, "get_order_status", result["found"])
        return result

    async def ask_support(self, grant: dict[str, Any] | None, message: str, case_id: str | None = None
                          ) -> dict[str, Any]:
        message = (message or "").strip()
        args = {"message": message[:200], "case_id": case_id}
        grant = self._check(grant, "ask_support", args)
        if not message:
            raise GatewayError("message is required: the customer's question, in their words.")
        if len(message) > MAX_MESSAGE:
            raise GatewayError(f"message is too long (at most {MAX_MESSAGE} characters).")
        case_id = self._own_case(grant, case_id)
        if self.store.running_requests(grant["grant_id"]):
            self.store.audit(grant, "ask_support", args, "refused: busy")
            raise GatewayError("Support is still working on this assistant's previous request; call get_answer "
                               "first.")
        if self.store.requests_since(grant["grant_id"], time.time() - 3600) >= self.settings["asks_per_hour"]:
            self.store.audit(grant, "ask_support", args, "refused: hourly limit")
            raise GatewayError("This assistant has asked support too often this hour; try again later.")
        request_id = self.store.new_request(grant["grant_id"], message, case_id)
        task = asyncio.create_task(support.run_turn(self.store, grant, request_id, message, case_id, self.agent_url,
                                                    float(self.settings["ask_timeout_s"])))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.store.audit(grant, "ask_support", args, f"started {request_id}")
        return {"request_id": request_id, "status": "running",
                "message": "Support is working on it; this can take a few minutes. Call get_answer with this "
                           "request_id."}

    async def get_answer(self, grant: dict[str, Any] | None, request_id: str) -> dict[str, Any]:
        args = {"request_id": request_id}
        grant = self._check(grant, "get_answer", args)
        request = self.store.get_request(str(request_id or "").strip().upper(), grant["grant_id"])
        if request is None:
            self.store.audit(grant, "get_answer", args, "not found")
            raise GatewayError("No request with that id from this assistant (answers are kept for "
                               f"{self.settings['answers_kept_hours']} hours).")
        self.store.audit(grant, "get_answer", args, request["status"])
        if request["status"] == "running":
            return {"request_id": request["request_id"], "status": "running",
                    "elapsed_s": round(time.time() - request["created_at"]),
                    "message": "Still working; call get_answer again in about a minute."}
        out = {"request_id": request["request_id"], "status": request["status"], "reply": request["answer"]}
        result = {k: v for k, v in (request["result"] or {}).items() if not k.startswith("_")}
        if result.get("case"):
            out["case"] = result["case"]
        return out

    def housekeeping(self, startup: bool = False) -> None:
        """
        At start-up every request still 'running' lost its turn with the old process, so it is failed; later
        (hourly) only those past their time limit. Old answers are deleted: they hold account data.
        """
        self.store.fail_stale_requests(0 if startup else float(self.settings["ask_timeout_s"]) + 120)
        self.store.purge_answers(float(self.settings["answers_kept_hours"]) * 3600)


INSTRUCTIONS = """ACME Retail customer support for the signed-in customer who connected you. You can read their
orders and support cases and ask support questions for them; you cannot change anything (refunds, returns,
cancellations are confirmed by the customer in the ACME Retail app or website). Prefer get_order_status and
get_case_status (instant). ask_support runs a full support conversation and takes minutes: call get_answer with
its request_id until status is done. Pass the case_id from an earlier answer to continue the same case."""


def build_app(gateway: Gateway | None = None, *, host: str = "127.0.0.1", port: int = 8096,
              public_url: str | None = None) -> FastMCP:
    gateway = gateway or Gateway()
    public_url = (public_url or f"http://localhost:{port}").rstrip("/")
    signin.check_demo_allowed(host, public_url)  # the demo's customer picker never runs where others can reach it
    oauth = AcmeOAuth(gateway.store, gateway.policy, public_url)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
        gateway.housekeeping(startup=True)

        async def hourly() -> None:
            while True:
                await asyncio.sleep(3600)
                gateway.housekeeping()

        task = asyncio.create_task(hourly())
        try:
            yield
        finally:
            task.cancel()

    app = FastMCP(
        "ACME Retail support", instructions=INSTRUCTIONS, host=host, port=port, lifespan=lifespan,
        auth_server_provider=oauth,
        # Tokens are opaque grants of this gateway only (GrantStore), never accepted anywhere else, so there is no
        # other resource to tell apart: validate_token_resource is off on purpose.
        auth=AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp",
                          validate_token_resource=False, required_scopes=GRANTABLE_SCOPES,
                          client_registration_options=ClientRegistrationOptions(
                              enabled=True, valid_scopes=["read_status", "request_changes"],
                              default_scopes=GRANTABLE_SCOPES),
                          revocation_options=RevocationOptions(enabled=True)))

    def grant() -> dict[str, Any] | None:
        token = get_access_token()
        return gateway.store.verify(token.token) if token else None

    async def call(method: Any, *args: Any) -> dict[str, Any]:
        try:
            return await method(grant(), *args)
        except GatewayError as e:
            raise ToolError(str(e)) from e

    @app.tool()
    async def get_case_status(case_id: str | None = None) -> dict[str, Any]:
        """The customer's recent support cases (or one, by its CASE-... reference): each request with its status,
        reference and what happens next. Instant; read-only."""
        return await call(gateway.get_case_status, case_id)

    @app.tool()
    async def get_order_status(order_ref: str | None = None) -> dict[str, Any]:
        """The customer's orders with items, shipment tracking, returns and refunds. order_ref: an order number
        (ORD-...) or a few words such as "blue jacket"; omit for all orders. Instant; read-only."""
        return await call(gateway.get_order_status, order_ref)

    @app.tool()
    async def ask_support(message: str, case_id: str | None = None) -> dict[str, Any]:
        """Ask ACME Retail support a question for the customer, in their words (e.g. "Why hasn't my refund
        arrived?"). Runs in the background and takes minutes: returns a request_id; call get_answer with it.
        case_id continues an earlier case. Changes cannot be made through an assistant."""
        return await call(gateway.ask_support, message, case_id)

    @app.tool()
    async def get_answer(request_id: str) -> dict[str, Any]:
        """The result of an ask_support request: status running, done or failed; when done, support's reply and
        the case record (requests, statuses, references, suggested products)."""
        return await call(gateway.get_answer, request_id)

    app.custom_route("/signin", methods=["GET"])(oauth.signin_page)
    app.custom_route("/signin", methods=["POST"])(oauth.signin_submit)
    app.custom_route("/signin/code", methods=["POST"])(oauth.signin_code)
    app.custom_route("/signin/demo", methods=["POST"])(oauth.signin_demo)
    app.custom_route("/signout", methods=["POST"])(oauth.signout)
    app.custom_route("/consent", methods=["GET"])(oauth.consent_page)
    app.custom_route("/consent", methods=["POST"])(oauth.consent_submit)
    app.custom_route("/grants", methods=["GET"])(oauth.grants_page)
    app.custom_route("/grants/revoke", methods=["POST"])(oauth.grants_revoke)
    return app
