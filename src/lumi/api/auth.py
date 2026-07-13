"""Authentication: who is connecting, and what may they do.

This is the seam where a real login system plugs in -- session cookies, OAuth, an LDAP
lookup, whatever the lab standardises on. It is deliberately small and explicit rather
than pretending to be a full auth stack, because the *shape* is what matters: a
connection resolves to an `Identity(user, role)`, and the role is what every
permission check keys off (`lumi.contracts.policy`).

The default below is intentionally conservative: an unauthenticated connection gets the
`viewer` role (read-only) if `api.allow_anonymous_viewer` is set, and is otherwise
rejected. It never defaults to `operator`. Wire in a real backend before exposing this
beyond a trusted network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import WebSocket

from lumi.config import settings
from lumi.contracts.policy import ROLES

log = logging.getLogger(__name__)

VALID_ROLES = frozenset(ROLES) | {"admin"}


@dataclass(frozen=True)
class Identity:
    user: str
    role: str


async def authenticate(websocket: WebSocket) -> Identity | None:
    """Resolve a connection to an Identity, or None to reject it.

    Replace the body with a real check. The current implementation supports two things
    so the bridge can be exercised end to end:

      * a `token` query param mapped to a role via `api.tokens` in settings, and
      * optional anonymous read-only access via `api.allow_anonymous_viewer`.
    """
    api = settings.get("api", {})
    tokens: dict = dict(api.get("tokens", {}))  # { "<token>": {"user": ..., "role": ...} }

    token = websocket.query_params.get("token")
    if token and token in tokens:
        entry = tokens[token]
        role = entry.get("role", "viewer")
        if role not in VALID_ROLES:
            log.warning("token maps to unknown role %r; rejecting", role)
            return None
        return Identity(user=entry.get("user", "user"), role=role)

    if api.get("allow_anonymous_viewer", False):
        return Identity(user="anonymous", role="viewer")

    log.info("rejecting unauthenticated websocket (no valid token)")
    return None
