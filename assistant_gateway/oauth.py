"""
OAuth 2.1 for assistants (authorization code + PKCE, as in the MCP authorization spec). The MCP SDK serves the
endpoints (/.well-known metadata, /register, /authorize, /token, /revoke) and checks PKCE; this module stores the
clients, codes and grants (GrantStore) and serves the customer's pages:

    /signin     signing in to ACME Retail (signin.py): a one-time code to the customer's verified email address;
                the demo's "choose a test customer" only with ASSISTANT_DEMO_SIGNIN=1 on a loopback address
    /consent    "Allow <assistant> to see your orders and support cases?" for the signed-in customer
    /grants     the signed-in customer's connected assistants, each with a Disconnect button

The customer always comes from the signed session cookie, never from a form or the URL, and every form that changes
something carries a CSRF token (review finding S2). Client registration is checked and the consent page shows where
the customer is sent back to and whether ACME Retail verified the assistant (S3). Production would put the real ACME
Retail sign-in behind /signin.

E2 grants read_status only: a request for request_changes is narrowed to read_status (changes come with E4).
"""

import html
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.provider import AuthorizationCode
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.provider import RefreshToken
from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.provider import TokenError
from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull
from mcp.shared.auth import OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response

from assistant_gateway import signin
from assistant_gateway.grants import GrantStore
from coded_tools.customer_service import gateway_auth
from coded_tools.customer_service.data_adapter import get_adapter
from coded_tools.customer_service.identity import CUSTOMER_ID
from coded_tools.customer_service.identity import IDENTIFIED
from coded_tools.customer_service.identity import ensure_identity
from demo_console.catalog import Catalog

GRANTABLE_SCOPES = ["read_status"]  # E2; request_changes arrives with the confirm link (E4)
SCOPE_WORDS = {"read_status": "see your orders, deliveries, returns, refunds and support cases, and ask support "
                              "questions for you. It cannot change anything."}
MAX_CLIENT_NAME = 40
_NAME_JUNK = re.compile(r"[^\w .'&()+-]")  # a client's name is shown on the consent page: plain words only


class AcmeCode(AuthorizationCode):
    customer_id: str
    auth_level: str


async def resolve_customer(customer_id: str, policy: dict[str, Any]) -> tuple[str, str]:
    """(customer_id, auth_level) from the customer's website sign-in, resolved in code; KeyError if not possible."""
    catalog = await Catalog.aload()
    sly = gateway_auth.sign(catalog.gateway_sly_data(customer_id))
    summary = await ensure_identity(sly, get_adapter(), policy)
    if summary["identity_status"] not in IDENTIFIED:
        raise KeyError(f"the website sign-in could not be verified ({summary['identity_status']})")
    return sly[CUSTOMER_ID], sly["auth_level"]


class AcmeOAuth:
    """OAuthAuthorizationServerProvider backed by GrantStore."""

    def __init__(self, store: GrantStore, policy: dict[str, Any], public_url: str) -> None:
        self.store = store
        self.policy = policy
        self.settings = policy["assistant"]
        self.public_url = public_url.rstrip("/")

    # ------------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """
        Open registration (MCP clients register themselves), so what a client says about itself is checked (S3):
        redirects only to https or to this machine, and a plain short name. The consent page also shows where the
        customer will be sent and whether ACME Retail verified the assistant.
        """
        for uri in client_info.redirect_uris or []:
            parts = urlparse(str(uri))
            if parts.fragment or not (parts.scheme == "https" or
                                      (parts.scheme == "http" and signin.is_loopback(parts.hostname))):
                raise RegistrationError("invalid_redirect_uri",
                                        "redirect URIs must use https, or http on a loopback address, without a fragment")
        name = " ".join(_NAME_JUNK.sub(" ", client_info.client_name or "").split())[:MAX_CLIENT_NAME]
        client_info.client_name = name or None
        self.store.save_client(client_info.client_id, client_info.model_dump(mode="json"))

    def verified(self, client_id: str) -> bool:
        return client_id in (self.settings.get("verified_clients") or [])

    # ------------------------------------------------------------------ authorization code flow

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Sends the customer to the consent page; the request waits there under a one-time id."""
        consent_id = secrets.token_urlsafe(24)
        self.store.save_code(f"consent:{consent_id}", {
            "client_id": client.client_id, "client_name": client.client_name or client.client_id,
            "scopes": params.scopes or GRANTABLE_SCOPES, "state": params.state, "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource}, ttl_s=float(self.settings["auth_code_ttl_s"]) * 2)
        return f"{self.public_url}/consent?request={consent_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str
                                      ) -> AcmeCode | None:
        data = self.store.peek_code(f"code:{authorization_code}")
        if data is None or data["client_id"] != client.client_id:
            return None
        return AcmeCode(code=authorization_code, **{k: data[k] for k in (
            "scopes", "expires_at", "client_id", "code_challenge", "redirect_uri", "redirect_uri_provided_explicitly",
            "resource", "customer_id", "auth_level")})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AcmeCode) -> OAuthToken:
        data = self.store.use_code(f"code:{authorization_code.code}")  # single use, even in a race
        if data is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        ttl_s = float(self.settings["token_ttl_days"]) * 86400
        token, grant = self.store.issue(customer_id=data["customer_id"], client_id=client.client_id,
                                        client_name=data["client_name"], auth_level=data["auth_level"],
                                        scopes=data["scopes"], ttl_s=ttl_s)
        self.store.audit(grant, "oauth_token", {"scopes": data["scopes"]}, "issued")
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=int(ttl_s), scope=" ".join(data["scopes"]))

    # No refresh tokens: an access token lasts token_ttl_days, then the customer consents again.
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        raise TokenError("invalid_grant", "refresh tokens are not issued; connect the assistant again")

    # ------------------------------------------------------------------ access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        grant = self.store.verify(token)
        if grant is None:
            return None
        return AccessToken(token=token, client_id=grant["client_id"], scopes=grant["scopes"],
                           expires_at=int(grant["expires_at"]), subject=grant["grant_id"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        grant = self.store.verify(token.token)
        if grant:
            self.store.revoke(grant["grant_id"], grant["customer_id"])
            self.store.audit(grant, "oauth_revoke", {}, "revoked by the assistant")

    # ------------------------------------------------------------------ signing in (signin.py)

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    async def signin_page(self, request: Request) -> Response:
        nxt = signin.safe_next(request.query_params.get("next"))
        body = (f"<p>Sign in to ACME Retail to connect or manage your assistants.</p>"
                f"<form method='post' action='/signin'><input type='hidden' name='next' value='{_e(nxt)}'>"
                f"<label>Your email address<br><input type='email' name='email' required autocomplete='email'>"
                f"</label><div class='row'><button>Send me a code</button></div></form>")
        if signin.demo_enabled():
            body += (f"<hr><form method='post' action='/signin/demo'><input type='hidden' name='next' "
                     f"value='{_e(nxt)}'><label>Demo only, on this computer: choose a test customer<br>"
                     f"<select name='customer_id' required>{await _customer_options()}</select></label>"
                     f"<div class='row'><button class='quiet'>Sign in as this customer</button></div></form>")
        return _page("Sign in", body)

    async def signin_submit(self, request: Request) -> Response:
        form = await request.form()
        nxt = signin.safe_next(str(form.get("next") or ""))
        customer = await signin.resolve_email(str(form.get("email") or ""), self.policy)
        attempt = signin.send_code(self.store, customer)
        self.store.audit(None, "signin_code", {}, "sent" if customer else "no matching account")
        body = (f"<p>If that address belongs to an ACME Retail account, we have sent it a 6-digit code. It expires in "
                f"{signin.CODE_TTL_S // 60} minutes.</p>"
                f"<form method='post' action='/signin/code'><input type='hidden' name='attempt' value='{_e(attempt)}'>"
                f"<input type='hidden' name='next' value='{_e(nxt)}'><label>Code<br><input name='code' required "
                f"inputmode='numeric' autocomplete='one-time-code' maxlength='6'></label>"
                f"<div class='row'><button>Sign in</button></div></form>")
        return _page("Check your email", body)

    async def signin_code(self, request: Request) -> Response:
        form = await request.form()
        nxt = signin.safe_next(str(form.get("next") or ""))
        customer = signin.check_code(self.store, str(form.get("attempt") or ""), str(form.get("code") or ""))
        if customer is None:
            return _page("Sign-in failed", f"<p>That code is not right or has expired. <a href='"
                                           f"{_e(signin.signin_url(nxt))}'>Try again</a>.</p>", status=400)
        return self._signed_in(customer, nxt)

    async def signin_demo(self, request: Request) -> Response:
        if not signin.demo_enabled():
            return _page("Not available", "<p>Sign in with your email address.</p>", status=404)
        form = await request.form()
        try:
            customer = await resolve_customer(str(form.get("customer_id", "")), self.policy)
        except KeyError as e:
            return _page("Sign-in failed", f"<p>{_e(str(e))}</p>", status=400)
        return self._signed_in(customer, signin.safe_next(str(form.get("next") or "")))

    def _signed_in(self, customer: tuple[str, str], nxt: str) -> Response:
        self.store.audit(None, "signin", {"customer": customer[0][:4] + "…"}, "signed in")
        response = RedirectResponse(nxt, status_code=303)
        signin.start_session(response, customer[0], customer[1], secure=self.secure)
        return response

    async def signout(self, request: Request) -> Response:
        response = RedirectResponse("/signin", status_code=303)
        signin.end_session(response)
        return response

    # ------------------------------------------------------------------ the customer's pages

    async def consent_page(self, request: Request) -> Response:
        consent_id = request.query_params.get("request", "")
        pending = self.store.peek_code(f"consent:{consent_id}")
        if pending is None:
            return _page("Link expired", "<p>This sign-in link has expired or was already used. Start connecting the "
                                         "assistant again.</p>", status=400)
        current = signin.session(request)
        if current is None:
            return RedirectResponse(signin.signin_url(f"/consent?request={consent_id}"), status_code=303)
        badge = "" if self.verified(pending["client_id"]) else \
            " <span class='small'>(not verified by ACME Retail)</span>"
        host = urlparse(pending["redirect_uri"]).hostname or "?"
        body = (f"<p><b>{_e(pending['client_name'])}</b>{badge} wants to connect to your ACME Retail account.</p>"
                f"<p>If you allow it, it can {_e(SCOPE_WORDS['read_status'])}</p>"
                f"<p class='small'>After your answer you go back to <b>{_e(host)}</b>. Only allow it if you started "
                f"connecting this assistant yourself.</p>"
                f"<form method='post' action='/consent'>"
                f"<input type='hidden' name='request' value='{_e(consent_id)}'>"
                f"<input type='hidden' name='csrf' value='{_e(signin.csrf_token(current))}'>"
                f"<div class='row'><button name='decision' value='allow'>Allow</button>"
                f"<button name='decision' value='deny' class='quiet'>Don't allow</button></div></form>"
                f"<p class='small'>You can disconnect it at any time on the <a href='/grants'>Connected assistants</a> "
                f"page.</p>")
        return _page("Connect an assistant", body, signed_in=True)

    async def consent_submit(self, request: Request) -> Response:
        form = await request.form()
        current = signin.session(request)
        if not signin.csrf_ok(current, form.get("csrf")):  # signed in, and the form came from our own page
            return _page("Sign in again", "<p>Your sign-in has expired. Start connecting the assistant again.</p>",
                         status=403)
        pending = self.store.use_code(f"consent:{form.get('request', '')}")  # one answer per request
        if pending is None:
            return _page("Link expired", "<p>This sign-in link has expired or was already used.</p>", status=400)
        if form.get("decision") != "allow":
            return RedirectResponse(construct_redirect_uri(pending["redirect_uri"], error="access_denied",
                                                           state=pending["state"]), status_code=302)
        customer_id, auth_level = current["customer_id"], current["auth_level"]  # from the session, never the form
        code = secrets.token_urlsafe(32)
        scopes = [s for s in pending["scopes"] if s in GRANTABLE_SCOPES] or GRANTABLE_SCOPES
        ttl_s = float(self.settings["auth_code_ttl_s"])
        self.store.save_code(f"code:{code}", {**pending, "scopes": scopes, "customer_id": customer_id,
                                              "auth_level": auth_level, "expires_at": time.time() + ttl_s},
                             ttl_s=ttl_s)
        return RedirectResponse(construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"]),
                                status_code=302)

    async def grants_page(self, request: Request) -> Response:
        current = signin.session(request)
        if current is None:
            return RedirectResponse(signin.signin_url("/grants"), status_code=303)
        csrf = _e(signin.csrf_token(current))
        rows = "".join(
            f"<tr><td>{_e(g['client_name'] or g['client_id'])}</td><td>{_e(', '.join(g['scopes']))}</td>"
            f"<td>{time.strftime('%Y-%m-%d', time.localtime(g['created_at']))}</td>"
            f"<td>{time.strftime('%Y-%m-%d', time.localtime(g['expires_at']))}</td>"
            f"<td><form method='post' action='/grants/revoke'><input type='hidden' name='csrf' value='{csrf}'>"
            f"<input type='hidden' name='grant_id' value='{_e(g['grant_id'])}'>"
            f"<button class='quiet'>Disconnect</button></form></td></tr>"
            for g in self.store.customer_grants(current["customer_id"]))
        body = ("<table><tr><th>Assistant</th><th>Access</th><th>Connected</th><th>Expires</th><th></th></tr>"
                f"{rows}</table>" if rows else "<p>No assistants are connected.</p>")
        return _page("Connected assistants", body, signed_in=True)

    async def grants_revoke(self, request: Request) -> Response:
        form = await request.form()
        current = signin.session(request)
        if not signin.csrf_ok(current, form.get("csrf")):
            return _page("Sign in again", "<p>Your sign-in has expired.</p>", status=403)
        grant_id = str(form.get("grant_id", ""))
        if self.store.revoke(grant_id, current["customer_id"]):  # only one of the signed-in customer's own grants
            self.store.audit(self.store.get(grant_id), "revoke", {}, "revoked by the customer")
        return RedirectResponse("/grants", status_code=303)


async def _customer_options(selected: str = "") -> str:
    customers = (await Catalog.aload()).search(kind="all", limit=100)["customers"]
    return "".join(
        f"<option value='{_e(c['customer_id'])}'{' selected' if c['customer_id'] == selected else ''}>"
        f"{_e(c['name'])}{' · ' + _e(', '.join(s['id'] for s in c['scenarios'])) if c['scenarios'] else ''}</option>"
        for c in customers)


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _page(title: str, body: str, status: int = 200, signed_in: bool = False) -> HTMLResponse:
    if signed_in:
        body += ("<form method='post' action='/signout' class='small'><button class='quiet'>Sign out</button></form>")
    return _html(title, body, status)


def _html(title: str, body: str, status: int) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ACME Retail · {_e(title)}</title>
<style>
:root {{ --bg:#f5f6f8; --panel:#fff; --ink:#1a1f2b; --muted:#5d6577; --brand:#3a49c9; --line:#dde1e8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --panel:#161a22; --ink:#e6e9ef; --muted:#9aa2b1;
  --brand:#8090f5; --line:#2b313d; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 system-ui, sans-serif; }}
main {{ max-width:560px; margin:40px auto; padding:24px; background:var(--panel); border:1px solid var(--line);
  border-radius:12px; }}
h1 {{ font-size:22px; margin:0 0 16px; }} .brand {{ color:var(--ink); font-weight:500; }}
select, input, button {{ font:inherit; padding:8px 12px; border-radius:8px; margin-top:6px; }}
select, input {{ width:100%; box-sizing:border-box; border:1px solid var(--line); background:var(--panel); color:var(--ink); }}
button {{ border:0; background:var(--brand); color:#fff; cursor:pointer; margin-right:8px; }}
button.quiet {{ background:transparent; color:var(--ink); border:1px solid var(--line); }}
.row {{ margin-top:16px; }} .small {{ color:var(--muted); font-size:14px; }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; font-size:14px; }}
td, th {{ text-align:left; padding:6px 4px; border-bottom:1px solid var(--line); }}
@media (max-width:600px) {{ main {{ margin:0; border-radius:0; padding:16px; }} }}
</style></head><body><main><div class="brand"><b style="letter-spacing:.12em">ACME</b> Retail</div><h1>{_e(title)}</h1>{body}</main></body></html>""",
                        status_code=status)
