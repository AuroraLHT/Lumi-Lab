"""A signed token whose `sub` is not a user id must be rejected, not crash the request.

Tier T0: no broker, a throwaway SQLite file.

The bug this pins: `sub` carries the user id as a string (routes/auth.py mints it as
`str(user["id"])`), and all three verifiers turned it back with a bare `int(subject)`.
A token signed with the right key but carrying anything else -- a username, an older
token format, a hand-minted one -- therefore raised ValueError *past* the code that
exists to say "no": the MCP HTTP transport answered 500 with a stack trace instead of
401, and the API's dependency did the same instead of raising CREDENTIALS_EXCEPTION.

Found by pointing scripts/demo_mcp.py at the HTTP transport with a token minted as
`create_access_token("demoagent")`. Rejecting that token is correct; the 500 was not.
"""

from __future__ import annotations

import pytest

from lumi.api.db import UserStore
from lumi.api.security import create_access_token
from lumi.mcp.auth import LumiTokenVerifier


@pytest.fixture
async def verifier(tmp_path):
    store = UserStore(path=tmp_path / "users.db")
    await store.connect()
    await store.create_user(username="agent", password="pw", role="operator")
    yield LumiTokenVerifier(store)
    await store.close()


async def test_a_real_token_is_accepted(verifier):
    user = await verifier.user_store.get_user_by_username("agent")
    token = create_access_token(str(user["id"]), extra_claims={"role": "operator"})
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.claims["role"] == "operator"
    assert "operator" in access.scopes


@pytest.mark.parametrize("subject", ["agent", "", "12abc", None, [1]])
async def test_a_subject_that_is_not_a_user_id_is_refused(verifier, subject):
    """Refused by returning None -- the middleware turns that into 401. Before the fix
    every case here except None raised ValueError out of verify_token."""
    token = create_access_token(subject)
    assert await verifier.verify_token(token) is None


async def test_an_unknown_user_id_is_refused(verifier):
    assert await verifier.verify_token(create_access_token("99999")) is None
