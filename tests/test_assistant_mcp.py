"""
E2 end to end over HTTP (no LLM): OAuth 2.1 with PKCE and the consent page, then MCP tool calls with the issued
token through the MCP client, and refusals for missing, wrong and revoked tokens. The server runs in a thread on a
free port with the mock data and temporary stores.
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs
from urllib.parse import urlparse

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from assistant_gateway.server import build_app
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.mock_adapter import MockAdapter
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401 - module fixture used by isolated
from test_multi_agent_foundation import start

REDIRECT = "http://localhost:33418/callback"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_STORE_PATH", str(tmp_path / "assistant.db"))
    port = free_port()
    app = build_app(port=port)
    config = uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://localhost:{port}"
    srv.should_exit = True
    thread.join(10)


def email_of(customer_id: str) -> str:
    return next(i["identifier"] for i in MockAdapter.from_env().identities
                if i["customer_id"] == customer_id and i["channel"] == "email" and i["verified"])


def last_code(customer_id: str) -> str:
    sent = [r for r in ActionStore().read("outbox.jsonl") if r.get("source") == "sign_in"
            and r["customer_id"] == customer_id]
    return re.search(r"code is (\d{6})", sent[-1]["message"]).group(1)


def sign_in(http: httpx.Client, customer_id: str, next_path: str = "/grants") -> None:
    """The customer signs in with the code sent to their verified email address (the session cookie stays in http)."""
    page = http.post("/signin", data={"email": email_of(customer_id), "next": next_path})
    attempt = page.text.split("name='attempt' value='")[1].split("'")[0]
    back = http.post("/signin/code", data={"attempt": attempt, "code": last_code(customer_id), "next": next_path})
    assert back.status_code == 303 and back.headers["location"] == next_path, back.text


def csrf_of(page: httpx.Response) -> str:
    return page.text.split("name='csrf' value='")[1].split("'")[0]


def connect(base: str, customer_id: str, decision: str = "allow") -> dict:
    """What an assistant does to connect: register, authorize with PKCE, the customer consents, exchange the code."""
    with httpx.Client(base_url=base, follow_redirects=False, timeout=30) as http:
        client = http.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                               "token_endpoint_auth_method": "none",
                                               "grant_types": ["authorization_code", "refresh_token"],
                                               "response_types": ["code"], "scope": "read_status request_changes"})
        assert client.status_code == 201, client.text
        client_id = client.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = http.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                              "redirect_uri": REDIRECT, "code_challenge": challenge,
                                              "code_challenge_method": "S256", "state": "xyz",
                                              "scope": "read_status request_changes"})
        assert auth.status_code == 302, auth.text
        consent_url = urlparse(auth.headers["location"])
        request_id = parse_qs(consent_url.query)["request"][0]
        first = http.get(f"{consent_url.path}?{consent_url.query}")
        assert first.status_code == 303 and first.headers["location"].startswith("/signin")  # sign in first
        sign_in(http, customer_id, f"/consent?request={request_id}")
        page = http.get(f"{consent_url.path}?{consent_url.query}")
        assert page.status_code == 200 and "Claude" in page.text and "cannot change anything" in page.text
        assert "not verified by ACME Retail" in page.text and "localhost" in page.text  # S3: who and where to
        back = http.post("/consent", data={"request": request_id, "csrf": csrf_of(page), "decision": decision})
        assert back.status_code == 302
        query = parse_qs(urlparse(back.headers["location"]).query)
        assert query["state"] == ["xyz"]
        if decision != "allow":
            return {"error": query["error"][0]}
        token = http.post("/token", data={"grant_type": "authorization_code", "code": query["code"][0],
                                          "redirect_uri": REDIRECT, "client_id": client_id,
                                          "code_verifier": verifier})
        assert token.status_code == 200, token.text
        replay = http.post("/token", data={"grant_type": "authorization_code", "code": query["code"][0],
                                           "redirect_uri": REDIRECT, "client_id": client_id,
                                           "code_verifier": verifier})
        assert replay.status_code == 400  # a code works once
        return {**token.json(), "client_id": client_id}


async def call_tool(base: str, token: str, tool: str, args: dict) -> dict:
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == {"get_case_status", "get_order_status", "ask_support", "get_answer"}
            result = await session.call_tool(tool, args)
            text = result.content[0].text
            return {"error": text} if result.isError else json.loads(text)


def test_consent_then_status_through_mcp(server, mock):
    sly = {"customer_id": "CUST-S01", "channel": "web_session", "auth_level": "strong"}
    start(sly, {"domain": "returns_refunds", "kind": "refund_status", "summary": "Blue jacket refund"})
    token = connect(server, "CUST-S01")
    assert token["scope"] == "read_status"  # request_changes is narrowed to read_status in E2
    cases = asyncio.run(call_tool(server, token["access_token"], "get_case_status", {}))
    assert [c["case_id"] for c in cases["cases"]] == [sly["case_id"]]
    orders = asyncio.run(call_tool(server, token["access_token"], "get_order_status", {"order_ref": "ORD-S01-1"}))
    assert orders["orders"][0]["order_id"] == "ORD-S01-1" and "CUST-" not in json.dumps(orders)


def test_customer_can_refuse_consent(server):
    assert connect(server, "CUST-S01", decision="deny") == {"error": "access_denied"}


def test_missing_wrong_and_revoked_tokens_are_refused(server):
    with httpx.Client(base_url=server, timeout=30) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        headers = {"Accept": "application/json, text/event-stream"}
        assert http.post("/mcp", json=body, headers=headers).status_code == 401
        bad = http.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer acme_nope"})
        assert bad.status_code == 401
        token = connect(server, "CUST-S01")
        # The SDK's revocation form needs the client_secret field even for a public client; empty is accepted.
        revoke = http.post("/revoke", data={"token": token["access_token"], "client_id": token["client_id"],
                                            "client_secret": ""})
        assert revoke.status_code == 200, revoke.text
        gone = http.post("/mcp", json=body, headers={**headers, "Authorization": f"Bearer {token['access_token']}"})
        assert gone.status_code == 401


def test_grants_page_lists_and_disconnects(server):
    connect(server, "CUST-S01")
    with httpx.Client(base_url=server, timeout=30) as http:
        sign_in(http, "CUST-S01")
        page = http.get("/grants")
        assert "Claude" in page.text and "read_status" in page.text
        grant_id = page.text.split("name='grant_id' value='")[1].split("'")[0]
        refused = http.post("/grants/revoke", data={"grant_id": grant_id})  # no CSRF token: not from our page
        assert refused.status_code == 403 and "Claude" in http.get("/grants").text
        http.post("/grants/revoke", data={"grant_id": grant_id, "csrf": csrf_of(page)})
        assert "No assistants are connected" in http.get("/grants").text


# ---------------------------------------------------------------------------- S2 / S3: sign-in and consent

def test_pages_never_take_the_customer_from_the_url_or_a_form(server):
    connect(server, "CUST-S01")
    with httpx.Client(base_url=server, timeout=30, follow_redirects=False) as http:
        page = http.get("/grants", params={"customer_id": "CUST-S01"})
        assert page.status_code == 303 and page.headers["location"].startswith("/signin")
        sign_in(http, "CUST-B0001")
        other = http.get("/grants", params={"customer_id": "CUST-S01"})
        assert "No assistants are connected" in other.text  # another customer sees only their own grants
        assert http.post("/consent", data={"request": "x", "customer_id": "CUST-S01",
                                           "decision": "allow"}).status_code == 403


def test_forged_session_cookie_is_refused(server):
    with httpx.Client(base_url=server, timeout=30, follow_redirects=False) as http:
        http.cookies.set("acme_session", "eyJjdXN0b21lcl9pZCI6IkNVU1QtUzAxIn0.forged")
        assert http.get("/grants").status_code == 303


def test_sign_in_codes_are_single_use_limited_and_never_reveal_accounts(server):
    with httpx.Client(base_url=server, timeout=30, follow_redirects=False) as http:
        before = len(ActionStore().read("outbox.jsonl"))
        unknown = http.post("/signin", data={"email": "nobody@example.invalid"})
        known = http.post("/signin", data={"email": email_of("CUST-S01")})
        assert unknown.status_code == known.status_code == 200
        assert unknown.text.split("<form")[0] == known.text.split("<form")[0]  # the same words either way
        assert len(ActionStore().read("outbox.jsonl")) == before + 1  # a code only for the real account
        attempt = known.text.split("name='attempt' value='")[1].split("'")[0]
        for _ in range(5):
            assert http.post("/signin/code", data={"attempt": attempt, "code": "000000x"}).status_code == 400
        right = http.post("/signin/code", data={"attempt": attempt, "code": last_code("CUST-S01")})
        assert right.status_code == 400  # five wrong tries lock the attempt


def test_open_redirect_after_sign_in_is_not_possible(server):
    with httpx.Client(base_url=server, timeout=30, follow_redirects=False) as http:
        page = http.post("/signin", data={"email": email_of("CUST-S01"), "next": "//evil.example/x"})
        attempt = page.text.split("name='attempt' value='")[1].split("'")[0]
        back = http.post("/signin/code", data={"attempt": attempt, "code": last_code("CUST-S01"),
                                               "next": "https://evil.example/"})
        assert back.headers["location"] == "/grants"


def test_registration_rejects_unsafe_redirects_and_cleans_the_name(server):
    with httpx.Client(base_url=server, timeout=30) as http:
        base = {"token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "scope": "read_status"}
        bad = http.post("/register", json={**base, "redirect_uris": ["http://evil.example/cb"], "client_name": "x"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
        ok = http.post("/register", json={**base, "redirect_uris": ["https://assistant.example/cb"],
                                          "client_name": "<b>ACME</b> Official ✔ Assistant"})
        assert ok.status_code == 201 and ok.json()["client_name"] == "b ACME b Official Assistant"


def test_the_demo_sign_in_is_off_by_default_and_only_on_loopback(server, monkeypatch):
    with httpx.Client(base_url=server, timeout=30) as http:
        assert "choose a test customer" not in http.get("/signin").text
        assert http.post("/signin/demo", data={"customer_id": "CUST-S01"}).status_code == 404
    monkeypatch.setenv("ASSISTANT_DEMO_SIGNIN", "1")
    with pytest.raises(RuntimeError):
        build_app(host="0.0.0.0", port=free_port())
    with pytest.raises(RuntimeError):
        build_app(port=free_port(), public_url="https://everly.example")
    build_app(port=free_port())  # loopback: allowed


def test_sign_in_codes_stay_out_of_the_companion_and_the_review_console(server):
    from coded_tools.customer_service.case_store import CaseStore
    from coded_tools.customer_service.companion import CompanionStore
    from coded_tools.customer_service.policy import load_policy
    from coded_tools.customer_service.review_service import ReviewService

    with httpx.Client(base_url=server, timeout=30) as http:
        http.post("/signin", data={"email": email_of("CUST-S01")})
    code = last_code("CUST-S01")
    thread = CompanionStore().thread("CUST-S01", since=None)
    assert not any(code in (e["text"] or "") for e in thread)
    staff = ReviewService(ActionStore(), CaseStore(), None, load_policy()).outbox()
    assert not any(code in (r.get("message") or "") for r in staff)
