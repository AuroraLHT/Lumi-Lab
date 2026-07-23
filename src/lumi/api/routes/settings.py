"""
Per-user UI settings.

The payload is an opaque JSON object owned by the frontend (dashboard layout,
theme, selected host, per-panel configuration). The backend validates only the
size and that it is a JSON object, so the UI can evolve its own schema without
backend migrations.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..db import User, UserStore
from ..deps import get_current_user, get_user_store
from ..models import UserSettings

router = APIRouter(prefix="/users/me", tags=["settings"])

# Guards against a runaway client persisting an unbounded blob.
MAX_SETTINGS_BYTES = 256 * 1024


@router.get("/settings", response_model=UserSettings)
async def get_user_settings(
    user: User = Depends(get_current_user),
    store: UserStore = Depends(get_user_store),
):
    """
    Return the caller's saved settings.

    A user who has never saved anything gets an empty object rather than a 404,
    so the frontend can treat "no settings yet" and "settings are empty" the
    same way and just fall back to its defaults.
    """
    payload = await store.get_settings(user["id"])
    return UserSettings(settings=payload or {})


@router.put("/settings", response_model=UserSettings)
async def put_user_settings(
    body: UserSettings,
    user: User = Depends(get_current_user),
    store: UserStore = Depends(get_user_store),
):
    """Replace the caller's settings wholesale (last write wins)."""
    encoded_size = len(json.dumps(body.settings).encode("utf-8"))
    if encoded_size > MAX_SETTINGS_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Settings payload is {encoded_size} bytes, limit is {MAX_SETTINGS_BYTES}",
        )

    saved = await store.put_settings(user["id"], body.settings)
    logging.debug(f"Saved settings for user '{user['username']}' ({encoded_size} bytes)")

    return UserSettings(settings=saved)


@router.delete("/settings", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_settings(
    user: User = Depends(get_current_user),
    store: UserStore = Depends(get_user_store),
):
    """Reset the caller's settings back to the frontend defaults."""
    await store.delete_settings(user["id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
