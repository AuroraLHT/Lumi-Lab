"""Authentication: who is connecting, and what may they do.

This is the seam where the login system plugs in. A connection resolves to an
`Identity(user, role)`, and the role is what every permission check keys off
(`lumi.contracts.policy`). The bridge never sees anything but the resolved role.

The implementation backs onto the SQLite user store (`lumi.api.db`): the browser
logs in over HTTP (`POST /auth/login`), gets a JWT carrying its user id, and
presents it as `?token=` on the `/ws` upgrade. Here we verify that token and load
the account's current role, so a role change or a deactivated account takes effect
on the next connection rather than living forever inside a stale token.

Two escape hatches remain for running without a login flow:

  * `auth.enabled = false` -- the backend runs wide open, every connection is an
    administrator. Never do this outside a trusted network.
  * `api.allow_anonymous_viewer = true` -- a connection with no token gets the
    read-only `viewer` role instead of being rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import WebSocket

from lumi.config import settings
from lumi.contracts.policy import ROLES

from .db import UserStore
from .security import TokenError, decode_access_token

log = logging.getLogger(__name__)

VALID_ROLES = frozenset(ROLES)


@dataclass(frozen=True)
class Identity:
    user: str
    role: str


async def authenticate(websocket: WebSocket) -> Identity | None:
    """Resolve a connection to an Identity, or None to reject it."""
    # Wide-open mode: no token check at all, everyone is an administrator.
    if not settings.get("auth.enabled", True):
        return Identity(user="anonymous", role="admin")

    api = settings.get("api", {})
    token = websocket.query_params.get("token")

    if token:
        identity = await _identity_from_token(websocket, token)
        if identity is None:
            log.info("rejecting websocket: invalid or unknown token")
        return identity

    if api.get("allow_anonymous_viewer", False):
        return Identity(user="anonymous", role="viewer")

    log.info("rejecting unauthenticated websocket (no token)")
    return None


async def _identity_from_token(websocket: WebSocket, token: str) -> Identity | None:
    store: UserStore | None = getattr(websocket.app.state, "user_store", None)
    if store is None:
        log.error("cannot authenticate websocket: user store is not initialised")
        return None

    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        log.debug("rejected websocket token: %s", exc)
        return None

    subject = payload.get("sub")
    if subject is None:
        return None

    user = await store.get_user_by_id(int(subject))
    if user is None or not user["is_active"]:
        return None

    role = user["role"]
    if role not in VALID_ROLES:
        log.warning("user %r has unknown role %r; rejecting", user["username"], role)
        return None

    return Identity(user=user["username"], role=role)
