"""An OAuth 2.1 authorization server for the MCP HTTP transport, so an agent can
*log in* instead of being handed a token out of band.

Why this exists: the HTTP transport has always demanded an operator bearer token,
and the only way to get one was to run `scripts/start_mcp_http.sh --token-only`
and paste the result into the host's config. That is not how anyone uses an MCP
server, and it has a second problem -- the token expires in 12 hours, so the
pasted copy goes stale by the next morning with no way to renew it.

With this wired in, the flow is the one the MCP spec describes and Claude Code /
Codex already implement:

    1. the host calls /mcp with no credentials and gets 401 plus a
       `WWW-Authenticate` header naming the protected-resource metadata
    2. it reads that metadata, finds this server as the authorization server,
       and registers itself (RFC 7591)
    3. it opens a browser at /authorize, which lands on the login page below
    4. you type a lumi username and password -- the same account the web UI uses
    5. the browser is redirected back with a code; the host exchanges it (PKCE)
       for an access token *and a refresh token*, and renews silently from then on

Everything security-relevant is reused rather than reinvented: passwords are
checked by `UserStore.authenticate`, and the access token minted here is the
same JWT `POST /auth/login` issues, so `LumiTokenVerifier` verifies both without
knowing which one it is holding. A hand-minted token keeps working exactly as
before. What is genuinely new is only the OAuth plumbing -- codes, refresh
tokens, and client registrations -- and the SDK supplies the protocol half of
that (metadata, PKCE verification, error shapes); this module supplies storage
and the one decision the SDK cannot make, which is whether these credentials
belong to somebody allowed to drive the equipment.

Two limits worth stating plainly:

  * **Plain HTTP means a plaintext password.** The login form posts credentials
    to this server. On loopback that is fine; anywhere else, terminate TLS in a
    reverse proxy in front and set --public-url to the https:// address, or you
    have moved the problem from "token in a config file" to "password on the wire".
  * **Access tokens cannot be revoked.** They are stateless JWTs, so an issued
    one is valid until it expires. Revocation drops the *refresh* token, which
    stops renewal; `delete_refresh_tokens_for_user` is the bigger hammer.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from lumi.api.db import User, UserStore
from lumi.api.security import create_access_token
from lumi.config import settings
from lumi.mcp.auth import REQUIRED_SCOPE, ROLE_SCOPES, LumiTokenVerifier
from lumi.mcp.store import OAuthStore

log = logging.getLogger(__name__)

#: Where the browser lands after /authorize. Not an MCP-spec path -- it is this
#: server's own login UI, named in the URL `authorize` redirects to.
LOGIN_PATH = "/login"

#: RFC 6749 §4.1.2 wants an authorization code to be short-lived and single-use.
#: It is redeemed by the host immediately on receiving the redirect, so a minute
#: is generous; it is also deleted on first use in `exchange_authorization_code`.
CODE_TTL_SECONDS = 60

#: How long a half-finished login (browser has been sent to the form, credentials
#: not yet submitted) stays valid. Long enough to go find a password manager.
LOGIN_TTL_SECONDS = 15 * 60

#: How long silent renewal keeps working before the browser login is needed again.
REFRESH_TOKEN_TTL_DAYS = 30


@dataclass
class _PendingLogin:
    """An /authorize request parked while its user proves who they are."""

    client_id: str
    params: AuthorizationParams
    expires_at: float


class LumiAuthorizationServer:
    """`OAuthAuthorizationServerProvider` backed by lumi's own user database.

    The SDK's `create_auth_routes` turns this into /authorize, /token, /register
    and /revoke plus the RFC 8414 metadata document; only the parts that need to
    know about lumi accounts live here.
    """

    def __init__(self, *, user_store: UserStore, store: OAuthStore) -> None:
        self.user_store = user_store
        self.store = store
        self.verifier = LumiTokenVerifier(user_store)
        # In memory on purpose -- see the module docstring in store.py. Both are
        # bounded by their TTLs, swept lazily whenever one is looked up.
        self._pending: dict[str, _PendingLogin] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # --- client registration -------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self.store.get_client(client_id)
        if info is None:
            return None
        return OAuthClientInformationFull.model_validate(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Open registration, which is what the MCP spec expects of a server with no
        # out-of-band client provisioning. It grants nothing on its own: a registered
        # client still cannot obtain a token until somebody logs in below, and the
        # token it then gets carries *that person's* role, not the client's.
        await self.store.put_client(
            client_info.client_id, client_info.model_dump(mode="json", exclude_none=True)
        )
        log.info(
            "registered MCP OAuth client %s (%s)",
            client_info.client_id,
            client_info.client_name or "unnamed",
        )

    # --- authorization -------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to the login form.

        The SDK has already validated the client, the redirect_uri and the PKCE
        challenge by this point; what it cannot do is establish *who* is asking,
        which is the one thing an authorization server exists to do.
        """
        self._sweep()
        handle = secrets.token_urlsafe(32)
        self._pending[handle] = _PendingLogin(
            client_id=client.client_id,
            params=params,
            expires_at=time.time() + LOGIN_TTL_SECONDS,
        )
        return f"{LOGIN_PATH}?{urlencode({'txn': handle})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        # A code issued to one client must not be redeemable by another, even
        # though it was never sent anywhere else -- the redirect could have leaked.
        if code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: pop first, so a replay finds nothing even if what follows fails.
        self._codes.pop(authorization_code.code, None)

        user = await self._load_user(authorization_code.subject)
        if user is None:
            raise TokenError("invalid_grant", "the account this code was issued to can no longer sign in")
        return await self._issue(client, user)

    # --- refresh -------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self.store.get_refresh_token(refresh_token)
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented token dies here whether or not the exchange succeeds.
        await self.store.delete_refresh_token(refresh_token.token)

        # Re-read the account rather than trusting the scopes frozen into the grant.
        # This is the point where deactivating a user, or demoting them to viewer,
        # actually takes effect -- within one access-token lifetime rather than at
        # the end of a 30-day refresh chain.
        user = await self._load_user(refresh_token.subject)
        if user is None:
            raise TokenError("invalid_grant", "the account behind this refresh token can no longer sign in")
        return await self._issue(client, user)

    # --- token verification / revocation -------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Same verifier the transport's middleware uses, so there is exactly one
        # answer to "is this token good" no matter which door asks.
        return await self.verifier.verify_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self.store.delete_refresh_token(token.token)
            return
        # An access token is a stateless JWT with no server-side record to delete;
        # RFC 7009 §2.1 explicitly allows this, and asks that we still answer 200.
        log.info("access-token revocation is a no-op (stateless JWT); it expires on its own")

    # --- the login page ------------------------------------------------------

    async def handle_login(self, request: Request) -> Response:
        """GET renders the form, POST checks the credentials and redirects back.

        Both live here rather than in a template so the whole authorization
        surface is one file: the page is the only thing standing between a
        registered client and equipment control.
        """
        if request.method == "GET":
            handle = request.query_params.get("txn", "")
            if self._get_pending(handle) is None:
                return HTMLResponse(_expired_page(), status_code=400)
            return HTMLResponse(self._login_page(handle))

        form = await request.form()
        handle = str(form.get("txn", ""))
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))

        pending = self._get_pending(handle)
        if pending is None:
            return HTMLResponse(_expired_page(), status_code=400)

        user = await self.user_store.authenticate(username, password)
        if user is None:
            log.info("failed MCP login attempt for username %r", username)
            return HTMLResponse(
                self._login_page(handle, error="Incorrect username or password."), status_code=400
            )

        scopes = ROLE_SCOPES.get(user["role"], [])
        if REQUIRED_SCOPE not in scopes:
            # Refused here, with a sentence explaining it, rather than at the door on
            # the next request: a token that authenticates fine and then 401s every
            # tool call is the least debuggable outcome available.
            log.info("refused MCP login for %r: role %r cannot drive equipment", username, user["role"])
            return HTMLResponse(
                self._login_page(
                    handle,
                    error=(
                        f"'{html.escape(username)}' is a {html.escape(user['role'])} account. "
                        f"Driving equipment needs the {REQUIRED_SCOPE} role or better."
                    ),
                ),
                status_code=403,
            )

        self._pending.pop(handle, None)
        params = pending.params
        code = secrets.token_urlsafe(32)  # 256 bits; RFC 6749 §10.10 asks for >= 128
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(user["id"]),
        )
        log.info("MCP login succeeded for %r (role %s)", user["username"], user["role"])
        return RedirectResponse(
            url=construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # --- internals -----------------------------------------------------------

    async def _issue(self, client: OAuthClientInformationFull, user: User) -> OAuthToken:
        """Mint the pair: lumi's own JWT as the access token, plus a refresh token."""
        scopes = ROLE_SCOPES.get(user["role"], [])
        expires_minutes = int(settings.get("auth.access_token_expire_minutes", 720))
        access_token = create_access_token(
            subject=str(user["id"]),
            extra_claims={"username": user["username"], "role": user["role"]},
            expires_minutes=expires_minutes,
        )

        refresh_token = secrets.token_urlsafe(32)
        await self.store.put_refresh_token(
            refresh_token,
            client_id=client.client_id,
            user_id=user["id"],
            scopes=scopes,
            expires_at=int(time.time() + REFRESH_TOKEN_TTL_DAYS * 24 * 3600),
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_minutes * 60,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def _load_user(self, subject: Optional[str]) -> Optional[User]:
        """The account a grant belongs to, or None if it may no longer sign in."""
        try:
            user_id = int(subject)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        user = await self.user_store.get_user_by_id(user_id)
        if user is None or not user["is_active"]:
            return None
        if REQUIRED_SCOPE not in ROLE_SCOPES.get(user["role"], []):
            return None
        return user

    def _get_pending(self, handle: str) -> Optional[_PendingLogin]:
        self._sweep()
        return self._pending.get(handle)

    def _sweep(self) -> None:
        now = time.time()
        for handle, pending in list(self._pending.items()):
            if pending.expires_at < now:
                del self._pending[handle]
        for code, entry in list(self._codes.items()):
            if entry.expires_at < now:
                del self._codes[code]

    def _login_page(self, handle: str, error: Optional[str] = None) -> str:
        banner = f'<p class="error">{error}</p>' if error else ""
        return _PAGE.format(
            body=f"""
    <h1>Sign in to lumi</h1>
    <p class="sub">An MCP client is asking to control lab equipment on your behalf.
       It will act with your account's role.</p>
    {banner}
    <form method="post" action="{LOGIN_PATH}">
      <input type="hidden" name="txn" value="{html.escape(handle)}">
      <label>Username<input name="username" autocomplete="username" autofocus required></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
      <button type="submit">Sign in</button>
    </form>"""
        )


def _expired_page() -> str:
    return _PAGE.format(
        body="""
    <h1>This login link has expired</h1>
    <p class="sub">Nothing is wrong -- authorization requests are short-lived.
       Start the connection again from your MCP client and a fresh link will open.</p>"""
    )


#: Self-contained so the server has no static-file route to serve or secure, and
#: readable in either colour scheme without a toggle.
_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>lumi MCP - sign in</title>
<style>
  :root {{ color-scheme: light dark; --fg: #1a1a1a; --bg: #fbfbfa; --card: #fff;
           --line: #d8d8d4; --muted: #6b6b66; --accent: #b4573a; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e8e8e6; --bg: #1a1a19; --card: #232322; --line: #3a3a38;
             --muted: #9a9a95; --accent: #d4785a; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
          background: var(--bg); color: var(--fg); padding: 1.5rem;
          font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  main {{ width: 100%; max-width: 25rem; background: var(--card); padding: 2rem;
          border: 1px solid var(--line); border-radius: 10px; }}
  h1 {{ margin: 0 0 .5rem; font-size: 1.3rem; }}
  .sub {{ margin: 0 0 1.5rem; color: var(--muted); font-size: .9rem; }}
  .error {{ margin: 0 0 1rem; padding: .6rem .8rem; border-radius: 6px; font-size: .9rem;
            color: var(--accent); border: 1px solid var(--accent); background: transparent; }}
  label {{ display: block; margin-bottom: 1rem; font-size: .85rem; color: var(--muted); }}
  input {{ display: block; width: 100%; margin-top: .35rem; padding: .6rem .7rem;
           font-size: 1rem; color: var(--fg); background: var(--bg);
           border: 1px solid var(--line); border-radius: 6px; }}
  input:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  button {{ width: 100%; padding: .65rem; font-size: 1rem; font-weight: 600; cursor: pointer;
            color: var(--bg); background: var(--fg); border: 0; border-radius: 6px; }}
</style></head>
<body><main>{body}</main></body></html>
"""
