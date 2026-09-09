"""Create an account in the API server's user database.

    uv run python scripts/create_api_user.py hliang16 --role admin
    uv run python scripts/create_api_user.py agent --role operator --password <pw>

The account CLI is `python -m lumi.api.manage` (create-user / list-users /
set-password / gen-secret). This script is a thin wrapper over its `create-user`,
kept in scripts/ next to `apply_broker_permissions.py` so the two account types are
found together; it adds `--database` for targeting a users.db other than the
configured one. Reach for the module CLI for anything past creating a user.

This is the login the browser console and the `/auth/login` endpoint check; it is
stored in the SQLite file at `auth.database_path` (default `cfg/users.db`) and is
**separate** from a RabbitMQ account -- see `scripts/apply_broker_permissions.py`
for those. A notebook that connects straight to the broker needs a broker account,
not one made here.

The password is prompted for (not echoed) unless `--password` is given. Roles are
viewer / operator / admin; `is_admin` is derived from `role == "admin"`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from lumi.api.db import USER_ROLES, UserStore
from lumi.path import PROJECT_ROOT


async def create(args: argparse.Namespace) -> int:
    path: Path | None = None
    if args.database:
        path = Path(args.database)
        if not path.is_absolute():
            path = PROJECT_ROOT / path

    store = UserStore(path)
    await store.connect()
    try:
        if await store.get_user_by_username(args.username) is not None:
            print(f"error: user '{args.username}' already exists", file=sys.stderr)
            return 1

        password = args.password or getpass.getpass(f"Password for {args.username}: ")
        if not password:
            print("error: password may not be empty", file=sys.stderr)
            return 1

        role = "admin" if args.admin else args.role
        user = await store.create_user(
            username=args.username,
            password=password,
            full_name=args.full_name,
            role=role,
        )
        print(f"created {user['role']} '{user['username']}' (id={user['id']}) in {store.path}")
        return 0
    finally:
        await store.close()


def main() -> int:
    p = argparse.ArgumentParser(
        prog="create_api_user",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("username")
    p.add_argument("--password", default=None, help="prompted for (hidden) if omitted")
    p.add_argument("--full-name", default="")
    p.add_argument(
        "--role", choices=sorted(USER_ROLES), default="viewer", help="default: viewer"
    )
    p.add_argument("--admin", action="store_true", help="shorthand for --role admin")
    p.add_argument(
        "--database",
        default=None,
        help="user database path (default: auth.database_path from settings, "
        "usually cfg/users.db); relative paths are under the repo root",
    )
    args = p.parse_args()
    return asyncio.run(create(args))


if __name__ == "__main__":
    raise SystemExit(main())
