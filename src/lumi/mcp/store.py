"""SQLite persistence for the MCP server's OAuth authorization server.

Two things have to outlive the process, and only these two:

  * **Registered clients.** An MCP host registers itself once (RFC 7591 dynamic
    client registration) and then stores the `client_id` it was given -- Claude
    Code keeps it in ~/.claude.json, indefinitely. If this server forgot its
    clients on restart, that stored id would come back as `invalid_client` on
    the next token refresh, which most hosts surface as a hard failure rather
    than by re-registering. Losing them is a worse experience than not having
    persistence at all.
  * **Refresh tokens.** The whole point of the OAuth flow is that you log in
    through the browser once and the host renews quietly after that. A refresh
    token that dies with the process turns "once" into "every restart".

Authorization codes and half-finished logins deliberately stay in memory (see
`oauth.py`): both live for seconds inside a single browser round-trip, so a
restart mid-flow just means clicking the link again.

This is its own database file, *not* a pair of extra tables in users.db. The api
node holds that one open for the whole of its life, and SQLite's default
rollback journal takes a database-wide write lock; adding a second writing
process to it would trade a clean separation for intermittent "database is
locked" errors on both sides. Nothing here needs to join against `users` -- a
user id is all it stores -- so there is nothing to gain by sharing the file.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from lumi.api.db import get_database_path
from lumi.config import settings
from lumi.path import PROJECT_ROOT

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token      TEXT PRIMARY KEY,
    client_id  TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    scopes     TEXT    NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS oauth_refresh_by_user ON oauth_refresh_tokens(user_id);
"""


def get_oauth_database_path() -> Path:
    """Where the client/refresh-token database lives.

    Defaults to a sibling of the user database rather than a fixed path, so the
    simulation launcher pointing `auth.database_path` at run/simulation/ takes
    this with it -- one throwaway stack should not leave OAuth grants in cfg/.
    """
    configured = settings.get("mcp.oauth_database_path", None)
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return get_database_path().parent / "mcp_oauth.db"


class OAuthStore:
    """Async CRUD over the two OAuth tables. One connection, owned by the MCP server."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or get_oauth_database_path()
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        # Anything expired is dead weight the moment we open the file; clearing it
        # here means the table cannot grow without bound across restarts.
        await self.purge_expired()
        log.info("MCP OAuth database ready at %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("OAuthStore.connect() has not been called")
        return self._db

    # --- clients -------------------------------------------------------------

    async def put_client(self, client_id: str, info: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, info, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(info), _now()),
        )
        await self.db.commit()

    async def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        async with self.db.execute(
            "SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["info"]) if row else None

    # --- refresh tokens ------------------------------------------------------

    async def put_refresh_token(
        self, token: str, *, client_id: str, user_id: int, scopes: list[str], expires_at: int
    ) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO oauth_refresh_tokens "
            "(token, client_id, user_id, scopes, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token, client_id, user_id, " ".join(scopes), int(expires_at), _now()),
        )
        await self.db.commit()

    async def get_refresh_token(self, token: str) -> Optional[dict[str, Any]]:
        """The stored grant, or None if unknown or expired. An expired row is
        deleted on the way out rather than merely hidden."""
        async with self.db.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token = ?", (token,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if row["expires_at"] <= time.time():
            await self.delete_refresh_token(token)
            return None
        return {
            "token": row["token"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "scopes": row["scopes"].split() if row["scopes"] else [],
            "expires_at": row["expires_at"],
        }

    async def delete_refresh_token(self, token: str) -> None:
        await self.db.execute("DELETE FROM oauth_refresh_tokens WHERE token = ?", (token,))
        await self.db.commit()

    async def delete_refresh_tokens_for_user(self, user_id: int) -> int:
        """Cut every host loose from one account -- the lever to pull when a
        laptop with a live grant on it goes missing."""
        cursor = await self.db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE user_id = ?", (user_id,)
        )
        await self.db.commit()
        return cursor.rowcount

    async def purge_expired(self) -> int:
        cursor = await self.db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE expires_at <= ?", (int(time.time()),)
        )
        await self.db.commit()
        return cursor.rowcount


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
