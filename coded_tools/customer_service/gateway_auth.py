"""
Signed identity between the channel gateways and the agent server (review finding S1).

The agent server accepts any HTTP caller and Neuro SAN cannot bind it to one address, so the identity in sly_data is
only trusted when a gateway signed it. Every gateway in this project (demo console, evaluation runners, assistant
gateway) calls sign() just before sending a turn; identity.ensure_identity() calls verify() and drops identity fields
that are not covered by a valid signature. A caller without the key can still talk to the network, but only as an
unidentified contact: no account data and no changes.

    identity_token   "<claims>.<mac>": base64url JSON of {"channel", "identifier" | "customer_id" + "auth_level",
                     "iat", "exp"} and its HMAC-SHA256 under the gateway key. Valid for TOKEN_TTL_S.
    identity_seal    written by the agent server once the identity is resolved in a request, so later tool calls in
                     the same request reuse the result; an HMAC too, so a caller cannot send one of its own.

The key: GATEWAY_IDENTITY_KEY, else the file GATEWAY_IDENTITY_KEY_FILE (default runtime/secrets/gateway_identity.key),
created on first use. All processes of the demo run from this project, so they share the file; in production the key
comes from a secret manager.

Unit tests may call tools with unsigned sly_data: only when pytest is loaded and GATEWAY_IDENTITY_TRUST_UNSIGNED=1
(tests/conftest.py). The agent server never loads pytest, so this cannot be switched on there.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KEY_ENV = "GATEWAY_IDENTITY_KEY"
KEY_FILE_ENV = "GATEWAY_IDENTITY_KEY_FILE"
TRUST_UNSIGNED_ENV = "GATEWAY_IDENTITY_TRUST_UNSIGNED"
TOKEN = "identity_token"
SEAL = "identity_seal"
CUSTOMER_MESSAGE = "customer_message"
TOKEN_TTL_S = 900  # one turn, with room for a slow model
# The sly_data keys that say who the customer is: only trusted when signed.
IDENTITY_FIELDS = ("channel", "identifier", "customer_id", "auth_level")

_KEY: bytes | None = None


def _key() -> bytes:
    global _KEY  # noqa: PLW0603 - read once per process
    if _KEY is not None:
        return _KEY
    value = os.environ.get(KEY_ENV, "").strip()
    if not value:
        path = Path(os.environ.get(KEY_FILE_ENV) or PROJECT_ROOT / "runtime" / "secrets" / "gateway_identity.key")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as f:  # exclusive: two processes starting together agree
                    f.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        value = path.read_text(encoding="utf-8").strip()
    _KEY = value.encode()
    return _KEY


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(message: str) -> str:
    return _b64(hmac.new(_key(), message.encode(), hashlib.sha256).digest())


def mac(purpose: str, message: str) -> str:
    """An HMAC under the gateway key for another use (e.g. the assistant gateway's session cookie), kept apart from
    identity tokens by its purpose."""
    return _mac(f"{purpose}\n{message}")


def sign(sly_data: dict[str, Any], *, customer_message: str | None = None,
         ttl_s: float = TOKEN_TTL_S) -> dict[str, Any]:
    """
    A copy of the gateway's sly_data with its identity fields signed. System requests carry no identity.
    customer_message: the customer's message exactly as the channel received it, for checks in code that must not
    depend on the model's copy of it (a customer's confirmation, confirmation.py).
    """
    out = {k: v for k, v in sly_data.items() if k not in (TOKEN, SEAL, CUSTOMER_MESSAGE)}
    if customer_message is not None:
        out[CUSTOMER_MESSAGE] = str(customer_message)[:2000]
    claims = {k: out[k] for k in IDENTITY_FIELDS if out.get(k) is not None}
    if not (claims.get("identifier") or claims.get("customer_id")):
        return out
    now = int(time.time())
    body = _b64(json.dumps({**claims, "iat": now, "exp": now + int(ttl_s)}, sort_keys=True).encode())
    out[TOKEN] = f"{body}.{_mac(body)}"
    return out


def verify(sly_data: dict[str, Any]) -> dict[str, Any] | None:
    """The signed identity claims (without iat/exp), or None if there is no valid, unexpired token."""
    token = str(sly_data.get(TOKEN) or "")
    body, _, mac = token.partition(".")
    if not body or not mac or not hmac.compare_digest(mac, _mac(body)):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or float(claims.get("exp") or 0) < time.time():
        return None
    return {k: claims[k] for k in IDENTITY_FIELDS if claims.get(k) is not None}


def trust_unsigned() -> bool:
    """Unit tests only (see the module docstring)."""
    return os.environ.get(TRUST_UNSIGNED_ENV) == "1" and "pytest" in sys.modules


def apply(sly_data: dict[str, Any]) -> bool:
    """
    Makes sly_data's identity fields exactly the signed ones, in place. Returns False when identity fields were sent
    without a valid signature (they are removed, so the contact counts as unidentified).
    """
    if trust_unsigned() and TOKEN not in sly_data:
        return True
    claims = verify(sly_data)
    if claims is None:
        sent = [k for k in ("identifier", "customer_id", "auth_level") if sly_data.get(k) is not None]
        for k in sent:
            sly_data.pop(k, None)
        if sent or sly_data.get(TOKEN):
            LOGGER.warning("unsigned or invalid identity refused (fields: %s)", ", ".join(sent) or "token only")
            return False
        return True
    for k in IDENTITY_FIELDS:
        if k in claims:
            sly_data[k] = claims[k]
        elif k != "channel":
            sly_data.pop(k, None)
    return True


def _seal_message(sly_data: dict[str, Any], status: str) -> str:
    return "|".join(str(sly_data.get(k) or "") for k in (TOKEN, *IDENTITY_FIELDS)) + f"|{status}"


def seal(sly_data: dict[str, Any], status: str) -> None:
    sly_data[SEAL] = _mac(_seal_message(sly_data, status))


def sealed(sly_data: dict[str, Any], status: str | None) -> bool:
    """True when this request's identity was resolved by the agent server itself (not sent by the caller)."""
    mark = str(sly_data.get(SEAL) or "")
    return bool(status and mark) and hmac.compare_digest(mark, _mac(_seal_message(sly_data, status)))
