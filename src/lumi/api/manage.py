"""
Small CLI for bootstrapping API accounts without going through HTTP.

    python -m lumi.api.manage create-user alice --role admin
    python -m lumi.api.manage list-users
    python -m lumi.api.manage set-password alice
    python -m lumi.api.manage gen-secret
"""

import argparse
import asyncio
import getpass
import sys

from .db import USER_ROLES, UserStore
from .security import generate_secret_key


async def cmd_create_user(args) -> int:
    store = UserStore()
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
        print(f"created {user['role']} '{user['username']}' (id={user['id']})")
        return 0
    finally:
        await store.close()


async def cmd_list_users(args) -> int:
    store = UserStore()
    await store.connect()
    try:
        users = await store.list_users()
        if not users:
            print("no users")
            return 0

        for user in users:
            flags = [user["role"]]
            if not user["is_active"]:
                flags.append("inactive")
            suffix = f" [{', '.join(flags)}]"
            print(f"{user['id']:>3}  {user['username']}{suffix}")
        return 0
    finally:
        await store.close()


async def cmd_set_password(args) -> int:
    store = UserStore()
    await store.connect()
    try:
        user = await store.get_user_by_username(args.username)
        if user is None:
            print(f"error: no such user '{args.username}'", file=sys.stderr)
            return 1

        password = args.password or getpass.getpass(f"New password for {args.username}: ")
        if not password:
            print("error: password may not be empty", file=sys.stderr)
            return 1

        await store.set_password(user["id"], password)
        print(f"password updated for '{args.username}'")
        return 0
    finally:
        await store.close()


async def cmd_gen_secret(args) -> int:
    print(generate_secret_key())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="lumi.api.manage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-user", help="create a new API user")
    p_create.add_argument("username")
    p_create.add_argument("--password", default=None, help="prompted for if omitted")
    p_create.add_argument("--full-name", default="")
    p_create.add_argument(
        "--role", choices=USER_ROLES, default="viewer", help="default: viewer"
    )
    p_create.add_argument(
        "--admin", action="store_true", help="shorthand for --role admin"
    )
    p_create.set_defaults(func=cmd_create_user)

    p_list = sub.add_parser("list-users", help="list API users")
    p_list.set_defaults(func=cmd_list_users)

    p_pass = sub.add_parser("set-password", help="change a user's password")
    p_pass.add_argument("username")
    p_pass.add_argument("--password", default=None, help="prompted for if omitted")
    p_pass.set_defaults(func=cmd_set_password)

    p_secret = sub.add_parser("gen-secret", help="print a fresh JWT signing key")
    p_secret.set_defaults(func=cmd_gen_secret)

    args = parser.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
