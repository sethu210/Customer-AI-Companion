"""
Signing in to ACME Retail on the assistant gateway's own pages (/consent, /grants), review findings S2 and S3.

    session    a signed cookie (HttpOnly, SameSite=Lax, Secure on https) with the customer, their sign-in level and an
               expiry; pages take the customer only from it, never from a form or the URL
    csrf       every form that changes something carries a token bound to the session (double-submit, HMAC)
    sign-in    by a one-time code sent to the customer's verified email address (the channel identities, resolved in
               code like any gateway's): 6 digits, 10 minutes, 5 tries; the page says the same whether or not the
               address belongs to an account. The code goes to the outbox like other messages, marked private, so
               it never reaches the companion's thread or notes (the demo console's Inbox shows it).
    demo       the old "choose a test customer" sign-in, only with ASSISTANT_DEMO_SIGNIN=1 and only on a loopback
               address: build_app refuses to start otherwise
"""

import base64
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response

from assistant_gateway.grants import GrantStore
from coded_tools.customer_service import gateway_auth
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.identity import CUSTOMER_ID
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.identity import ensure_identity

COOKIE = "acme_session"
SESSION_TTL_S = 1800
CODE_TTL_S = 600
CODE_TRIES = 5
DEMO_ENV = "ASSISTANT_DEMO_SIGNIN"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
OUTBOX = "outbox.jsonl"
SIGN_IN_SOURCE = "sign_in"  # private: companion.thread leaves these out


def demo_enabled() -> bool:
    return os.environ.get(DEMO_ENV) == "1"


def is_loopback(host: str | None) -> bool:
    return (host or "").strip().lower() in LOOPBACK


def check_demo_allowed(host: str, public_url: str) -> None:
    """The demo sign-in lets anyone pick any customer: never on an address other machines can reach."""
    if demo_enabled() and not (is_loopback(host) and is_loopback(urlparse(public_url).hostname)):
        raise RuntimeError(f"{DEMO_ENV}=1 is only allowed on a loopback address (host {host}, {public_url})")


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).rstrip(b"=").decode()


def _unb64(text: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


def safe_next(path: str | None, default: str = "/grants") -> str:
    """Only a path on this site (never //host or a full URL): no open redirect."""
    path = (path or "").strip()
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else default


# ------------------------------------------------------------------ the session


def session(request: Request) -> dict[str, Any] | None:
    """The signed-in customer {customer_id, auth_level, exp, sid}, or None."""
    raw = request.cookies.get(COOKIE, "")
    body, _, mark = raw.partition(".")
    if not body or not mark or not hmac.compare_digest(mark, gateway_auth.mac("assistant-session", body)):
        return None
    try:
        data = _unb64(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or float(data.get("exp") or 0) < time.time() or not data.get("customer_id"):
        return None
    return data


def start_session(response: Response, customer_id: str, auth_level: str, *, secure: bool) -> None:
    body = _b64({"customer_id": customer_id, "auth_level": auth_level, "exp": int(time.time()) + SESSION_TTL_S,
                 "sid": secrets.token_urlsafe(12)})
    response.set_cookie(COOKIE, f"{body}.{gateway_auth.mac('assistant-session', body)}", max_age=SESSION_TTL_S,
                        httponly=True, samesite="lax", secure=secure, path="/")


def end_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def csrf_token(current: dict[str, Any]) -> str:
    return gateway_auth.mac("assistant-csrf", f"{current['sid']}|{current['customer_id']}")


def csrf_ok(current: dict[str, Any] | None, token: Any) -> bool:
    return current is not None and isinstance(token, str) and hmac.compare_digest(token, csrf_token(current))


def signin_url(next_path: str) -> str:
    return f"/signin?next={quote(safe_next(next_path), safe='')}"


# ------------------------------------------------------------------ one-time codes by email


async def resolve_email(email: str, policy: dict[str, Any]) -> tuple[str, str] | None:
    """(customer_id, auth_level) for a verified email address of exactly one account, else None. Resolved in code."""
    email = email.strip()
    if not email or len(email) > 254 or "@" not in email:
        return None
    sly = gateway_auth.sign({"channel": "email", "identifier": email})
    summary = await ensure_identity(sly, get_adapter(), policy)
    if summary["identity_status"] not in IDENTIFIED:
        return None
    return sly[CUSTOMER_ID], sly["auth_level"]


def send_code(store: GrantStore, customer: tuple[str, str] | None) -> str:
    """A sign-in attempt id. With a customer, a code is sent to them; without one, nothing is (same page either way)."""
    attempt = secrets.token_urlsafe(18)
    data: dict[str, Any] = {"tries": 0}
    if customer is not None:
        code = f"{secrets.randbelow(10**6):06d}"
        data.update(customer_id=customer[0], auth_level=customer[1],
                    code=gateway_auth.mac("assistant-signin-code", f"{attempt}|{code}"))
        ActionStore().append(OUTBOX, {"customer_id": customer[0], "source": SIGN_IN_SOURCE, "private": True,
                                      "kind": "sign_in_code",
                                      "message": f"Your ACME Retail sign-in code is {code}. It expires in "
                                                 f"{CODE_TTL_S // 60} minutes. If you did not ask for it, ignore this "
                                                 f"message: nobody can sign in without it."})
    store.save_code(f"signin:{attempt}", data, ttl_s=CODE_TTL_S)
    return attempt


def check_code(store: GrantStore, attempt: str, code: str) -> tuple[str, str] | None:
    """(customer_id, auth_level) for the right code, used once; a wrong one counts towards CODE_TRIES."""
    key = f"signin:{attempt}"
    data = store.peek_code(key)
    if data is None or data["tries"] >= CODE_TRIES:
        return None
    given = gateway_auth.mac("assistant-signin-code", f"{attempt}|{(code or '').strip()}")
    if data.get("code") and hmac.compare_digest(given, data["code"]):
        used = store.use_code(key)
        return (used["customer_id"], used["auth_level"]) if used else None
    store.update_code(key, {**data, "tries": data["tries"] + 1})
    return None
