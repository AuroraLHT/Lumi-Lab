"""The MCP server's OAuth authorization server: logging in gets you a token.

Tier T0: no broker, no MCP transport, throwaway SQLite files. The SDK owns the
protocol half (metadata, PKCE, error shapes) and is not retested here; what these
cover is the half that knows about lumi accounts -- who may log in, what a code is
worth, and what happens to a grant when the account behind it changes.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.routing import Route

from lumi.api.db import UserStore
from lumi.mcp.auth import LumiTokenVerifier
from lumi.mcp.oauth import LOGIN_PATH, LumiAuthorizationServer
from lumi.mcp.store import OAuthStore

REDIRECT_URI = "http://127.0.0.1:41234/callback"


@pytest.fixture
async def env(tmp_path):
    users = UserStore(path=tmp_path / "users.db")
    await users.connect()
    await users.create_user(username="agent", password="pw", role="operator")
    await users.create_user(username="watcher", password="pw", role="viewer")

    store = OAuthStore(path=tmp_path / "mcp_oauth.db")
    await store.connect()

    provider = LumiAuthorizationServer(user_store=users, store=store)
    app = Starlette(routes=[Route(LOGIN_PATH, provider.handle_login, methods=["GET", "POST"])])
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    yield provider, client

    await client.aclose()
    await store.close()
    await users.close()


def _client_info(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
    )


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="opaque-state",
        scopes=["operator"],
        code_challenge="not-checked-here-the-sdk-verifies-it",
        redirect_uri=REDIRECT_URI,
        redirect_uri_provided_explicitly=True,
    )


async def _begin(provider, client_info=None):
    """Register a client and start an authorization, returning the login handle."""
    info = client_info or _client_info()
    await provider.register_client(info)
    url = await provider.authorize(info, _params())
    assert url.startswith(f"{LOGIN_PATH}?txn=")
    return info, url.split("txn=", 1)[1]


async def _login(http, handle, username="agent", password="pw"):
    return await http.post(
        LOGIN_PATH,
        data={"txn": handle, "username": username, "password": password},
        follow_redirects=False,
    )


def _code_from(response) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI)
    assert "state=opaque-state" in location
    return location.split("code=", 1)[1].split("&", 1)[0]


# --- the happy path ---------------------------------------------------------


async def test_a_password_login_yields_a_token_the_transport_accepts(env):
    """The whole point: no token is handed in, and one comes out the far end that
    `LumiTokenVerifier` -- the same check the HTTP transport's middleware runs --
    already accepts. Nothing new to trust on the verification side."""
    provider, http = env
    info, handle = await _begin(provider)

    assert (await http.get(LOGIN_PATH, params={"txn": handle})).status_code == 200

    code = _code_from(await _login(http, handle))
    authorization_code = await provider.load_authorization_code(info, code)
    assert authorization_code is not None

    token = await provider.exchange_authorization_code(info, authorization_code)
    assert token.refresh_token
    assert token.scope == "viewer operator"

    access = await LumiTokenVerifier(provider.user_store).verify_token(token.access_token)
    assert access is not None
    assert access.claims["role"] == "operator"
    assert access.client_id == "agent"


async def test_refresh_rotates_and_burns_the_old_token(env):
    """Silent renewal is the reason this exists at all -- a 12-hour access token
    that cannot be renewed is the pasted-token problem with extra steps."""
    provider, http = env
    info, handle = await _begin(provider)
    first = await provider.exchange_authorization_code(
        info, await provider.load_authorization_code(info, _code_from(await _login(http, handle)))
    )

    loaded = await provider.load_refresh_token(info, first.refresh_token)
    assert loaded is not None
    second = await provider.exchange_refresh_token(info, loaded, ["operator"])
    assert second.refresh_token != first.refresh_token

    assert await provider.load_refresh_token(info, first.refresh_token) is None
    assert await provider.load_refresh_token(info, second.refresh_token) is not None


async def test_a_registered_client_survives_a_restart(tmp_path):
    """A host stores its client_id forever; forgetting it turns the next quiet
    refresh into `invalid_client`, which hosts surface as a hard failure."""
    users = UserStore(path=tmp_path / "users.db")
    await users.connect()
    try:
        store = OAuthStore(path=tmp_path / "mcp_oauth.db")
        await store.connect()
        await LumiAuthorizationServer(user_store=users, store=store).register_client(_client_info())
        await store.close()

        reopened = OAuthStore(path=tmp_path / "mcp_oauth.db")
        await reopened.connect()
        provider = LumiAuthorizationServer(user_store=users, store=reopened)
        assert (await provider.get_client("client-1")) is not None
        await reopened.close()
    finally:
        await users.close()


# --- refusals ---------------------------------------------------------------


async def test_a_viewer_is_refused_at_the_login_page(env):
    """Refused with a sentence, not with a token that 401s on every tool call."""
    provider, http = env
    _, handle = await _begin(provider)

    response = await _login(http, handle, username="watcher")
    assert response.status_code == 403
    assert "viewer" in response.text
    assert not provider._codes


async def test_a_wrong_password_re_renders_the_form(env):
    provider, http = env
    _, handle = await _begin(provider)

    response = await _login(http, handle, password="nope")
    assert response.status_code == 400
    assert "Incorrect username or password" in response.text
    assert not provider._codes
    # The transaction survives, so a typo does not mean restarting the whole flow.
    assert (await _login(http, handle)).status_code == 302


async def test_an_authorization_code_is_single_use(env):
    provider, http = env
    info, handle = await _begin(provider)
    code = _code_from(await _login(http, handle))

    authorization_code = await provider.load_authorization_code(info, code)
    assert authorization_code is not None
    await provider.exchange_authorization_code(info, authorization_code)

    assert await provider.load_authorization_code(info, code) is None


async def test_another_client_cannot_redeem_the_code(env):
    """The redirect carrying the code could leak; the code is still bound to the
    client that asked for it."""
    provider, http = env
    _, handle = await _begin(provider)
    code = _code_from(await _login(http, handle))

    other = _client_info("client-2")
    await provider.register_client(other)
    assert await provider.load_authorization_code(other, code) is None


async def test_an_expired_code_is_not_loadable(env):
    provider, http = env
    info, handle = await _begin(provider)
    code = _code_from(await _login(http, handle))

    provider._codes[code].expires_at = time.time() - 1
    assert await provider.load_authorization_code(info, code) is None


async def test_an_expired_login_link_says_so(env):
    provider, http = env
    _, handle = await _begin(provider)
    provider._pending[handle].expires_at = time.time() - 1

    response = await http.get(LOGIN_PATH, params={"txn": handle})
    assert response.status_code == 400
    assert "expired" in response.text
    assert (await _login(http, handle)).status_code == 400


async def test_deactivating_an_account_stops_its_refresh_chain(env):
    """Where a stateless access token's 12-hour life is bounded: the renewal that
    would extend it re-reads the account instead of trusting the grant."""
    provider, http = env
    info, handle = await _begin(provider)
    token = await provider.exchange_authorization_code(
        info, await provider.load_authorization_code(info, _code_from(await _login(http, handle)))
    )
    loaded = await provider.load_refresh_token(info, token.refresh_token)
    assert loaded is not None

    await provider.user_store.db.execute("UPDATE users SET is_active = 0 WHERE username = 'agent'")
    await provider.user_store.db.commit()

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(info, loaded, ["operator"])


async def test_demoting_to_viewer_stops_the_refresh_chain_too(env):
    provider, http = env
    info, handle = await _begin(provider)
    token = await provider.exchange_authorization_code(
        info, await provider.load_authorization_code(info, _code_from(await _login(http, handle)))
    )
    loaded = await provider.load_refresh_token(info, token.refresh_token)
    assert loaded is not None

    await provider.user_store.db.execute("UPDATE users SET role = 'viewer' WHERE username = 'agent'")
    await provider.user_store.db.commit()

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(info, loaded, ["operator"])


async def test_revoking_drops_the_refresh_token(env):
    provider, http = env
    info, handle = await _begin(provider)
    token = await provider.exchange_authorization_code(
        info, await provider.load_authorization_code(info, _code_from(await _login(http, handle)))
    )

    loaded = await provider.load_refresh_token(info, token.refresh_token)
    assert loaded is not None
    await provider.revoke_token(loaded)
    assert await provider.load_refresh_token(info, token.refresh_token) is None
