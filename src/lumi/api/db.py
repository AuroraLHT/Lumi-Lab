"""
SQLite-backed store for API users and their per-user UI settings.

This is deliberately small: the lab has a handful of operators, so a single
SQLite file next to the config is plenty and avoids standing up a database
server. Everything goes through aiosqlite so it never blocks the event loop.

Each user carries a `role` -- one of viewer / operator / admin -- and that role
is the single source of truth for what the user may do. It is what the /ws seam
(`lumi.api.auth`) hands to the bridge, which gates every bus call against the
same `lumi.contracts.policy` predicate the broker uses. `is_admin` is derived
from it (role == "admin") purely so the HTTP admin endpoints have a boolean to
check; there is no second, drift-prone flag.

Settings are stored as an opaque JSON blob. The frontend owns the schema
(dashboard layout, theme, selected host, panel configuration); the backend only
persists and returns it. That keeps UI changes from requiring a migration here.
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Optional, TypedDict

import aiosqlite

from lumi.config import settings
from lumi.path import PROJECT_ROOT

from .security import hash_password, verify_password

#: The roles a human account may hold. "node" (from policy.ROLES) is for
#: equipment processes, never people, so it is deliberately not offered here.
USER_ROLES = ("viewer", "operator", "admin")
DEFAULT_ROLE = "viewer"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    full_name     TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'viewer',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class User(TypedDict):
    id: int
    username: str
    full_name: str
    role: str
    is_admin: bool
    is_active: bool


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalise_role(role: str) -> str:
    if role not in USER_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {USER_ROLES}")
    return role


def _row_to_user(row: aiosqlite.Row) -> User:
    role = row["role"]
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": role,
        "is_admin": role == "admin",
        "is_active": bool(row["is_active"]),
    }


def get_database_path() -> Path:
    configured = settings.get("auth.database_path", "cfg/users.db")
    path = Path(configured)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


class UserStore:
    """Async CRUD over the users / user_settings tables."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or get_database_path()
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logging.info(f"User database ready at {self.path}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("UserStore.connect() has not been called")
        return self._db

    # ---------------------------------------------------------------- users

    async def create_user(
        self,
        username: str,
        password: str,
        full_name: str = "",
        role: str = DEFAULT_ROLE,
    ) -> User:
        role = _normalise_role(role)
        cursor = await self.db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (username, full_name, hash_password(password), role, _now()),
        )
        await self.db.commit()

        user = await self.get_user_by_id(cursor.lastrowid)
        assert user is not None
        return user

    async def get_user_by_id(self, user_id: int) -> Optional[User]:
        async with self.db.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        return _row_to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> Optional[User]:
        async with self.db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cursor:
            row = await cursor.fetchone()

        return _row_to_user(row) if row else None

    async def authenticate(self, username: str, password: str) -> Optional[User]:
        async with self.db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            # Hash anyway so a missing user and a wrong password take a similar
            # amount of time and the endpoint does not leak which usernames exist.
            verify_password(password, "$2b$12$" + "." * 53)
            return None

        if not verify_password(password, row["password_hash"]):
            return None

        if not row["is_active"]:
            return None

        return _row_to_user(row)

    async def list_users(self) -> list[User]:
        async with self.db.execute("SELECT * FROM users ORDER BY id") as cursor:
            rows = await cursor.fetchall()

        return [_row_to_user(row) for row in rows]

    async def set_password(self, user_id: int, password: str) -> None:
        await self.db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(password), user_id),
        )
        await self.db.commit()

    async def count_users(self) -> int:
        async with self.db.execute("SELECT COUNT(*) AS n FROM users") as cursor:
            row = await cursor.fetchone()

        return int(row["n"])

    # ------------------------------------------------------------- settings

    async def get_settings(self, user_id: int) -> Optional[dict[str, Any]]:
        async with self.db.execute(
            "SELECT payload FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            logging.error(f"Corrupt settings payload for user {user_id}; ignoring")
            return None

    async def put_settings(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        await self.db.execute(
            "INSERT INTO user_settings (user_id, payload, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload,"
            " updated_at = excluded.updated_at",
            (user_id, json.dumps(payload), _now()),
        )
        await self.db.commit()
        return payload

    async def delete_settings(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        await self.db.commit()


async def bootstrap_default_user(store: UserStore) -> None:
    """
    Create the initial admin account on an empty database so a fresh deployment
    is reachable. Credentials come from settings; the password must be set
    explicitly, otherwise the operator creates the first user by hand.
    """
    if await store.count_users() > 0:
        return

    username = settings.get("auth.default_admin_username", "admin")
    password = settings.get("auth.default_admin_password", "")

    if not password:
        logging.warning(
            "No users exist and auth.default_admin_password is unset. Create the "
            "first user with: python -m lumi.api.manage create-user <name> --role admin"
        )
        return

    await store.create_user(
        username=username,
        password=password,
        full_name="Administrator",
        role="admin",
    )
    logging.warning(
        f"Bootstrapped default admin user '{username}' from settings. "
        "Change this password immediately."
    )
