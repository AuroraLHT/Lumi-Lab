"""The auth-disabled identity must be able to save settings -- without becoming a
usable account. Tier T0: no broker, a throwaway SQLite file.

The bug this pins: with `auth.enabled = false` every request runs as
`deps.ANONYMOUS_USER`, whose id is 0. `user_settings.user_id` is
`REFERENCES users(id)` and UserStore turns foreign keys on, so every
PUT /users/me/settings raised `sqlite3.IntegrityError: FOREIGN KEY constraint failed`
and 500'd. GET was unaffected -- a SELECT for a missing id returns no rows and the
route maps that to `{}` -- so the UI loaded fine and silently never saved anything.

The fix seeds a real row for id 0. The tests below exist mostly to stop that row
turning into a back door: it must stay inactive and unloginable, because that is what
keeps a token minted while auth was off from working once auth is switched back on.
"""

from __future__ import annotations

import sqlite3

import pytest

from lumi.api.db import (
    ANONYMOUS_USER_ID,
    ANONYMOUS_USERNAME,
    UserStore,
    ensure_anonymous_user,
)
from lumi.api.deps import ANONYMOUS_USER


@pytest.fixture
async def store(tmp_path):
    s = UserStore(path=tmp_path / "users.db")
    await s.connect()
    yield s
    await s.close()


async def test_saving_settings_fails_without_the_anonymous_row(store):
    """The original bug, pinned: this is what the frontend hit on every save."""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await store.put_settings(ANONYMOUS_USER_ID, {"theme": "dark"})


async def test_reads_succeeded_all_along_which_is_why_it_looked_healthy(store):
    """No row, no error -- the asymmetry that hid the failure from the UI."""
    assert await store.get_settings(ANONYMOUS_USER_ID) is None


async def test_settings_round_trip_once_the_row_exists(store):
    await ensure_anonymous_user(store)

    await store.put_settings(ANONYMOUS_USER_ID, {"theme": "dark", "panels": [1, 2]})
    assert await store.get_settings(ANONYMOUS_USER_ID) == {"theme": "dark", "panels": [1, 2]}

    # Last write wins, the same as for a real user.
    await store.put_settings(ANONYMOUS_USER_ID, {"theme": "light"})
    assert await store.get_settings(ANONYMOUS_USER_ID) == {"theme": "light"}


async def test_seeding_is_idempotent_across_restarts(store):
    """lifespan runs this on every boot."""
    await ensure_anonymous_user(store)
    await store.put_settings(ANONYMOUS_USER_ID, {"kept": True})

    await ensure_anonymous_user(store)
    await ensure_anonymous_user(store)

    assert await store.count_users() == 1
    # Re-seeding must not wipe what the user saved.
    assert await store.get_settings(ANONYMOUS_USER_ID) == {"kept": True}


async def test_the_seeded_row_is_inactive(store):
    """`is_active` is the guard that keeps a stale anonymous token from working once
    auth is re-enabled -- both token paths reject on it."""
    await ensure_anonymous_user(store)

    user = await store.get_user_by_id(ANONYMOUS_USER_ID)
    assert user is not None
    assert user["is_active"] is False


async def test_the_seeded_row_cannot_be_logged_into(store):
    await ensure_anonymous_user(store)

    for attempt in ("", "anonymous", "admin", "password", "0"):
        assert await store.authenticate(ANONYMOUS_USERNAME, attempt) is None


async def test_seeding_does_not_disturb_real_user_ids(store):
    """AUTOINCREMENT starts at 1, so a real account never collides with the sentinel."""
    await ensure_anonymous_user(store)

    alice = await store.create_user("alice", "pw", role="operator")
    bob = await store.create_user("bob", "pw", role="viewer")

    assert alice["id"] == 1
    assert bob["id"] == 2
    assert alice["is_active"] is True


async def test_real_users_keep_their_own_settings(store):
    await ensure_anonymous_user(store)
    alice = await store.create_user("alice", "pw")

    await store.put_settings(ANONYMOUS_USER_ID, {"who": "anon"})
    await store.put_settings(alice["id"], {"who": "alice"})

    assert await store.get_settings(ANONYMOUS_USER_ID) == {"who": "anon"}
    assert await store.get_settings(alice["id"]) == {"who": "alice"}


async def test_a_conflicting_real_account_is_reported_not_swallowed(store, caplog):
    """ON CONFLICT DO NOTHING also hides a clash on the UNIQUE username. If someone
    holds the name "anonymous", seeding cannot succeed and saves keep failing -- so it
    must say so rather than produce another silent 500."""
    await store.create_user(ANONYMOUS_USERNAME, "pw")

    await ensure_anonymous_user(store)

    assert await store.get_user_by_id(ANONYMOUS_USER_ID) is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_the_seeded_row_matches_the_runtime_identity(store):
    """deps.ANONYMOUS_USER is what requests actually run as; the row is only a foreign
    key anchor, but the two must not drift on identity."""
    await ensure_anonymous_user(store)

    row = await store.get_user_by_id(ANONYMOUS_USER_ID)
    assert row["id"] == ANONYMOUS_USER["id"]
    assert row["username"] == ANONYMOUS_USER["username"]
    assert row["role"] == ANONYMOUS_USER["role"]
    # Deliberately NOT is_active: the runtime identity is active, the stored row must
    # not be. That difference is the security property, not an inconsistency.
    assert row["is_active"] is not ANONYMOUS_USER["is_active"]
