"""
Authentication routes: login, current user, password change, user management.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from lumi.config import settings

from ..db import User, UserStore
from ..deps import get_current_admin, get_current_user, get_user_store
from ..models import (
    CreateUserRequest,
    PasswordChangeRequest,
    TokenResponse,
    UserResponse,
)
from ..security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user["id"],
        username=user["username"],
        full_name=user["full_name"],
        role=user["role"],
        is_admin=user["is_admin"],
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    store: UserStore = Depends(get_user_store),
):
    """
    Exchange username + password for a JWT.

    Uses the OAuth2 password-form shape (`username`/`password` as form fields)
    so FastAPI's interactive docs can drive it. The token carries the user's
    `role`, which the /ws bridge reads to gate every bus call.
    """
    user = await store.authenticate(form_data.username, form_data.password)

    if user is None:
        logging.info(f"Failed login attempt for username '{form_data.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expires_minutes = int(settings.get("auth.access_token_expire_minutes", 720))
    token = create_access_token(
        subject=str(user["id"]),
        extra_claims={"username": user["username"], "role": user["role"]},
        expires_minutes=expires_minutes,
    )

    logging.info(f"User '{user['username']}' logged in")

    return TokenResponse(
        access_token=token,
        expires_in=expires_minutes * 60,
        user=_to_response(user),
    )


@router.get("/me", response_model=UserResponse)
async def read_current_user(user: User = Depends(get_current_user)):
    return _to_response(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    store: UserStore = Depends(get_user_store),
):
    verified = await store.authenticate(user["username"], body.current_password)
    if verified is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    await store.set_password(user["id"], body.new_password)
    logging.info(f"User '{user['username']}' changed their password")


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _: User = Depends(get_current_admin),
    store: UserStore = Depends(get_user_store),
):
    return [_to_response(u) for u in await store.list_users()]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    _: User = Depends(get_current_admin),
    store: UserStore = Depends(get_user_store),
):
    if await store.get_user_by_username(body.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{body.username}' already exists",
        )

    user = await store.create_user(
        username=body.username,
        password=body.password,
        full_name=body.full_name,
        role=body.role,
    )
    logging.info(f"Created user '{user['username']}' with role '{user['role']}'")

    return _to_response(user)
