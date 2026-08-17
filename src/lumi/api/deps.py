"""
Authentication dependencies for the HTTP routes (login, current user, settings).

The browser's live data path is the `/ws` bridge, whose auth lives in
`lumi.api.auth`. These dependencies cover the small HTTP surface that sits beside
it: they accept a bearer token either as an `Authorization: Bearer <token>`
header or as a `?token=` query parameter (so tooling that cannot set headers can
still call in), and resolve it to a `User` row.
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lumi.config import settings

from .db import (
    ANONYMOUS_FULL_NAME,
    ANONYMOUS_USER_ID,
    ANONYMOUS_USERNAME,
    User,
    UserStore,
)
from .security import TokenError, decode_access_token

# auto_error=False so we can fall back to the query parameter before rejecting.
bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

#: The synthetic identity returned when auth is disabled entirely. Mirrors the
#: /ws seam, which hands the bridge the "admin" role in the same situation.
#:
#: This dict is what requests actually run as; it is returned directly, without
#: consulting the database. While auth is disabled `db.ensure_anonymous_user` also
#: keeps a matching row on disk, purely so `user_settings.user_id` has a foreign key
#: to point at -- that row is inactive and has no usable password.
#:
#: A token minted for this identity therefore still stops working the moment auth is
#: turned back on: `_user_from_token` looks the subject up and rejects it, now because
#: the row it finds is inactive rather than because no row exists. `is_active` is what
#: enforces that, so it must stay false on the seeded row.
ANONYMOUS_USER: User = {
    "id": ANONYMOUS_USER_ID,
    "username": ANONYMOUS_USERNAME,
    "full_name": ANONYMOUS_FULL_NAME,
    "role": "admin",
    "is_admin": True,
    "is_active": True,
}


def auth_enabled() -> bool:
    return bool(settings.get("auth.enabled", True))


def get_user_store(request: Request) -> UserStore:
    store: Optional[UserStore] = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User store is not initialised",
        )
    return store


async def _user_from_token(token: str, store: UserStore) -> User:
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        logging.debug(f"Rejected token: {exc}")
        raise CREDENTIALS_EXCEPTION from exc

    # `sub` is the user id as a string. A signed token carrying anything else is just
    # an invalid credential -- without the guard, int() raises past CREDENTIALS_EXCEPTION
    # and the caller gets a 500 instead of a 401.
    subject = payload.get("sub")
    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        logging.debug(f"Rejected token: sub {subject!r} is not a user id")
        raise CREDENTIALS_EXCEPTION from None

    user = await store.get_user_by_id(user_id)
    if user is None or not user["is_active"]:
        raise CREDENTIALS_EXCEPTION

    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    token: Optional[str] = Query(
        default=None,
        description="Bearer token, for callers that cannot send an Authorization header.",
    ),
    store: UserStore = Depends(get_user_store),
) -> User:
    if not auth_enabled():
        return ANONYMOUS_USER

    raw_token = credentials.credentials if credentials else token
    if not raw_token:
        raise CREDENTIALS_EXCEPTION

    return await _user_from_token(raw_token, store)


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user["is_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator account",
        )
    return user
