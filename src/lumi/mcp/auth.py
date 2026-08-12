"""Bearer-token verification for the MCP server's HTTP transport, backed by the
same JWT/user store the browser bridge already uses (lumi.api.auth/security/db) --
one login system (`POST /auth/login`) issues tokens usable by both, rather than a
second auth mechanism to manage.

The stdio transport (a local subprocess only you can spawn) never touches this.
HTTP is the one meant for a remote agent, so it always requires a valid operator-
or-admin token -- independent of `settings.auth.enabled`, which only controls the
browser's wide-open dev mode. Exposing real hardware control to the network with
zero auth just because the browser side happens to be running unauthenticated would
be a much bigger blast radius than a stray websocket.
"""

from __future__ import annotations

import logging

from mcp.server.auth.provider import AccessToken, TokenVerifier

from lumi.api.db import UserStore
from lumi.api.security import TokenError, decode_access_token
from lumi.contracts.policy import ROLES

log = logging.getLogger(__name__)

#: Scopes granted per role, cumulative so a `required_scopes=["operator"]` check
#: passes for both "operator" and "admin" tokens without special-casing admin.
ROLE_SCOPES: dict[str, list[str]] = {
    "viewer": ["viewer"],
    "operator": ["viewer", "operator"],
    "admin": ["viewer", "operator", "admin"],
}

#: The scope the HTTP transport's RequireAuthMiddleware demands before a request
#: even reaches a tool call. A viewer token is rejected here, at the door, rather
#: than by falling through every per-op policy.permits() check with nothing granted.
REQUIRED_SCOPE = "operator"


class LumiTokenVerifier(TokenVerifier):
    def __init__(self, user_store: UserStore) -> None:
        self.user_store = user_store

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            payload = decode_access_token(token)
        except TokenError as exc:
            log.debug("rejected MCP bearer token: %s", exc)
            return None

        subject = payload.get("sub")
        if subject is None:
            return None

        user = await self.user_store.get_user_by_id(int(subject))
        if user is None or not user["is_active"]:
            return None

        role = user["role"]
        if role not in ROLES:
            log.warning("user %r has unknown role %r; rejecting MCP token", user["username"], role)
            return None

        return AccessToken(
            token=token,
            client_id=user["username"],
            scopes=ROLE_SCOPES.get(role, [role]),
            claims={"role": role, "user": user["username"]},
        )
