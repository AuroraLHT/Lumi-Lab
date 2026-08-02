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
import secrets
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

#: Identity of the synthetic account used when `auth.enabled` is false. `users.id` is
#: AUTOINCREMENT, which starts at 1 and never reuses a value, so 0 cannot collide with
#: a real account -- which is why it was chosen as the sentinel. See
#: `ensure_anonymous_user` for why a row for it nevertheless has to exist on disk.
ANONYMOUS_USER_ID = 0
ANONYMOUS_USERNAME = "anonymous"
ANONYMOUS_FULL_NAME = "Anonymous (auth disabled)"

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


async def ensure_anonymous_user(store: UserStore) -> None:
    """Give the auth-disabled identity a real row, so its settings can be persisted.

    With `auth.enabled = false` every request resolves to `deps.ANONYMOUS_USER`, whose
    id is 0. That identity is synthetic and was never written to the database, but
    `user_settings.user_id` is `REFERENCES users(id)` and this store turns foreign keys
    on (they are off by default in SQLite), so saving settings raised

        sqlite3.IntegrityError: FOREIGN KEY constraint failed

    on every PUT /users/me/settings. Reads were unaffected -- a SELECT for a missing id
    just returns nothing, and the route maps that to "{}" -- so the UI loaded happily
    and silently failed to ever save anything.

    The row is inserted **inactive**, with an unguessable random password. That
    preserves the property the sentinel was chosen for in the first place: a token
    minted while auth was off must stop working the moment auth is turned back on.
    Both token paths (`deps._user_from_token` and the /ws `auth._identity_from_token`)
    reject on `not user["is_active"]`, and `authenticate` checks it too, so this row
    can anchor a foreign key but can never log in or authorise anything.

    `role` mirrors ANONYMOUS_USER's "admin" so an operator listing users sees what the
    anonymous identity actually gets at runtime. The role is never read for an
    authorisation decision -- with auth off the hardcoded dict is used and the database
    is not consulted; with auth on the row is refused as inactive before its role is
    looked at -- so `is_active`, not the role, is the guard.

    Only called when auth is disabled (see `main.lifespan`), so an auth-enabled
    deployment never grows a passwordless admin-shaped row.
    """
    if await store.get_user_by_id(ANONYMOUS_USER_ID) is not None:
        return

    await store.db.execute(
        "INSERT INTO users (id, username, full_name, password_hash, role, is_active,"
        " created_at) VALUES (?, ?, ?, ?, 'admin', 0, ?) ON CONFLICT DO NOTHING",
        (
            ANONYMOUS_USER_ID,
            ANONYMOUS_USERNAME,
            ANONYMOUS_FULL_NAME,
            # A real bcrypt hash of a value nobody holds, rather than a placeholder
            # string: verify_password logs a warning on a malformed hash, and this
            # fails the comparison cleanly instead.
            hash_password(secrets.token_urlsafe(32)),
            _now(),
        ),
    )
    await store.db.commit()

    if await store.get_user_by_id(ANONYMOUS_USER_ID) is None:
        # ON CONFLICT DO NOTHING also swallows a clash on the UNIQUE username, which
        # happens if someone created a genuine account called "anonymous". Saving
        # settings will keep failing, so say why rather than leaving another silent 500.
        logging.error(
            f"Could not seed the anonymous user (id={ANONYMOUS_USER_ID}): a different "
            f"account already uses the name {ANONYMOUS_USERNAME!r}. Per-user settings "
            "will fail to save while auth is disabled; rename that account to fix it."
        )
        return

    logging.info(
        f"Seeded the inactive '{ANONYMOUS_USERNAME}' user (id={ANONYMOUS_USER_ID}) "
        "because auth is disabled; it exists only so UI settings can be saved."
    )


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
