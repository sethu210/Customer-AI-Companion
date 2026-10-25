"""
Identity contract between the channel gateway and the agent.

The gateway authenticates the customer (logged-in session, verified email sender, matched phone number) and
puts the result in sly_data before the first message. sly_data never reaches the LLM, so the model can neither
see nor change who the customer is. Tools take the customer id from here and ignore anything the model passes.

sly_data keys:
    customer_id   required; absent when the channel could not resolve exactly one account
    channel       "web_session" | "email" | "phone"
    auth_level    "strong" | "medium" | "weak"; write actions will require more than "weak" (Phase 4)
"""

from typing import Any

from coded_tools.customer_service import gateway_auth

CUSTOMER_ID = "customer_id"
CHANNEL = "channel"
AUTH_LEVEL = "auth_level"

# ---------------------------------------------------------------------------- multi-agent identity resolution
#
# The multi-agent gateway may send the raw channel identity instead of a customer id:
#     sly_data {"channel": "phone", "identifier": "+1-299-555-0150", "identity_token": ...}
# ensure_identity() matches it against channel_identities in code (never in the LLM) and, only for exactly one
# verified match, writes customer_id and auth_level into sly_data. A gateway that already authenticated the
# customer can still send customer_id (+ auth_level) directly. Either way the identity fields count only when the
# gateway signed them (gateway_auth.py); unsigned ones are dropped, and the contact is unidentified.

IDENTIFIER = "identifier"
IDENTITY_STATUS = "identity_status"

GATEWAY_AUTHENTICATED = "gateway_authenticated"
VERIFIED = "verified"
AMBIGUOUS = "ambiguous"        # the identifier matches more than one account (shared household phone)
UNVERIFIED = "unverified"      # one account, but that channel identity was never verified
NOT_FOUND = "not_found"
MISSING = "missing"            # the gateway sent nothing usable
UNAVAILABLE = "unavailable"    # the identity lookup failed after retries
IDENTIFIED = {GATEWAY_AUTHENTICATED, VERIFIED}

IDENTITY_HINTS = {
    GATEWAY_AUTHENTICATED: "The customer is identified. Do not ask who they are.",
    VERIFIED: "The customer is identified. Do not ask who they are.",
    AMBIGUOUS: "This channel matches more than one account, so no account can be opened. Share no account "
               "details and do not say how many accounts matched. Ask the customer to log in to the app or website, "
               "or to use the verification link sent to them.",
    UNVERIFIED: "This channel identity is not verified, so no account can be opened. Share no account details. Ask "
                "the customer to log in to the app or website, or to use the verification link sent to them.",
    NOT_FOUND: "No account is linked to this channel. Share no account details. Ask the customer to log in to the "
               "app or website, or to use the verification link sent to them.",
    MISSING: "The customer could not be identified by the channel. Share no account details. Ask the customer to "
             "log in to the app or website, or to use the verification link sent to them.",
    UNAVAILABLE: "The identity check is not available right now. Share no account details; say you are checking "
                 "and offer to connect the customer with a person.",
}


async def ensure_identity(sly_data: dict[str, Any], adapter: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """
    Resolves the customer once per request and returns a summary that is safe to show the LLM (no ids).
    Never takes anything from tool arguments.
    """
    status = sly_data.get(IDENTITY_STATUS)
    # Resolved earlier in this request by the agent server itself (the seal), or else from the signed fields only:
    # a caller can send neither an identity_status nor a customer_id of its own.
    if not gateway_auth.sealed(sly_data, status) and not (gateway_auth.trust_unsigned() and
                                                         gateway_auth.TOKEN not in sly_data):
        status = None
        sly_data.pop(IDENTITY_STATUS, None)
        gateway_auth.apply(sly_data)
    if status not in IDENTITY_HINTS or (status in IDENTIFIED and not sly_data.get(CUSTOMER_ID)):
        status = await _resolve(sly_data, adapter, policy)
        sly_data[IDENTITY_STATUS] = status
        gateway_auth.seal(sly_data, status)
    auth_level = sly_data.get(AUTH_LEVEL) or "weak"
    identified = status in IDENTIFIED
    return {
        "identity_status": status,
        "can_read_account": identified,
        "can_request_changes": identified and auth_level in policy.get("write_auth_levels", []),
        "message_hint": IDENTITY_HINTS[status] + ("" if not identified or auth_level in
                                                   policy.get("write_auth_levels", []) else
                                                   " Account changes need a stronger sign-in (app, website or "
                                                   "verification link); questions can be answered now."),
    }


async def _resolve(sly_data: dict[str, Any], adapter: Any, policy: dict[str, Any]) -> str:
    customer_id = sly_data.get(CUSTOMER_ID)
    if isinstance(customer_id, str) and customer_id.strip():
        sly_data.setdefault(AUTH_LEVEL, "weak")
        return GATEWAY_AUTHENTICATED
    channel, identifier = sly_data.get(CHANNEL), sly_data.get(IDENTIFIER)
    if not isinstance(channel, str) or not isinstance(identifier, str) or not identifier.strip():
        return MISSING
    result = await adapter.fetch("resolve_channel_identity", channel, identifier, policy=policy)
    if result.failed:
        return UNAVAILABLE
    customers = {r["customer_id"] for r in result.rows}
    if not customers:
        return NOT_FOUND
    if len(customers) > 1:
        return AMBIGUOUS
    if not all(r["verified"] for r in result.rows):
        return UNVERIFIED
    levels = {r["auth_level"] for r in result.rows}
    sly_data[CUSTOMER_ID] = customers.pop()
    # Several identity rows for one customer on one channel: use the weakest level.
    sly_data[AUTH_LEVEL] = next(level for level in ("weak", "medium", "strong") if level in levels)
    return VERIFIED
