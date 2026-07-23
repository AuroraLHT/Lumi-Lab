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

from .db import User, UserStore
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
_ANONYMOUS_USER: User = {
    "id": 0,
    "username": "anonymous",
    "full_name": "Anonymous (auth disabled)",
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

    subject = payload.get("sub")
    if subject is None:
        raise CREDENTIALS_EXCEPTION

    user = await store.get_user_by_id(int(subject))
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
        return _ANONYMOUS_USER

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
