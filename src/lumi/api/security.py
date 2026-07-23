"""
Password hashing and JWT token handling for the API.

The signing key is read from `settings.auth.secret_key`, which should be
supplied through `cfg/.secrets.toml` (git-ignored) or the environment variable
`DYNACONF_AUTH__SECRET_KEY` in production. The default below is only usable for
local development and the API refuses to start with it when `auth.dev_mode` is
false.
"""

import datetime
import logging
import secrets
from typing import Any, Optional

import bcrypt
import jwt

from lumi.config import settings

ALGORITHM = "HS256"

# Sentinel value shipped in cfg/settings.toml. Never allow this in production.
DEV_SECRET_KEY = "dev-only-insecure-key-change-me"


def get_secret_key() -> str:
    key = settings.get("auth.secret_key", DEV_SECRET_KEY)

    if key == DEV_SECRET_KEY and not settings.get("auth.dev_mode", False):
        raise RuntimeError(
            "auth.secret_key is still the development default. Set a real key in "
            "cfg/.secrets.toml or via DYNACONF_AUTH__SECRET_KEY before running "
            "with auth.dev_mode = false."
        )

    return key


def generate_secret_key() -> str:
    """Helper for operators bootstrapping cfg/.secrets.toml."""
    return secrets.token_urlsafe(48)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the database: treat as a failed login rather than a 500.
        logging.warning("Malformed password hash encountered during verification")
        return False


def create_access_token(
    subject: str,
    extra_claims: Optional[dict[str, Any]] = None,
    expires_minutes: Optional[int] = None,
) -> str:
    if expires_minutes is None:
        expires_minutes = int(settings.get("auth.access_token_expire_minutes", 720))

    now = datetime.datetime.now(datetime.timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + datetime.timedelta(minutes=expires_minutes),
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, get_secret_key(), algorithm=ALGORITHM)


class TokenError(Exception):
    """Raised when a token is missing, malformed, or expired."""


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc
