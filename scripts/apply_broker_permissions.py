"""Apply the contract's permission roles to RabbitMQ.

    uv run python scripts/apply_broker_permissions.py --host localhost \
        --admin-user guest --admin-pass guest \
        --user viewer --role viewer --password <pw>

    # ...and what it would do, without doing it:
    uv run python scripts/apply_broker_permissions.py --role viewer --dry-run

Requires the management plugin (rabbitmq-plugins enable rabbitmq_management), and for
browsers, the STOMP-over-WebSocket plugin:

    rabbitmq-plugins enable rabbitmq_management rabbitmq_web_stomp

The permissions are DERIVED from src/lumi/contracts, so a new op is governed the moment
it is declared. Re-run this after any contract change (CI will tell you the hash moved).

Note: `guest` can only connect from localhost by default, and it has full access to
everything. Once real users exist, delete it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from base64 import b64encode

from lumi.contracts import REGISTRY, contract_hash
from lumi.contracts.policy import ROLES


def api(host: str, port: int, user: str, password: str, method: str, path: str, body=None):
    url = f"http://{host}:{port}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} failed: {exc.code} {exc.read().decode()[:200]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"cannot reach the management API at {url}: {exc.reason}\n"
            f"Is rabbitmq_management enabled?"
        ) from exc


def main() -> int:
    p = argparse.ArgumentParser(prog="apply-broker-permissions")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=15672, help="management API port")
    p.add_argument("--vhost", default="/")
    p.add_argument("--admin-user", default="guest")
    p.add_argument("--admin-pass", default="guest")
    p.add_argument("--user", help="the user to create/update")
    p.add_argument("--password", help="that user's password")
    p.add_argument("--role", required=True, choices=sorted(ROLES))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    permissions = ROLES[args.role]()

    print(f"role {args.role!r} from contract {contract_hash()}:\n")
    for exchange, perm in sorted(permissions.items()):
        print(f"  exchange {exchange}")
        print(f"    write: {perm.write[:160]}{'...' if len(perm.write) > 160 else ''}")
        print(f"    read:  {perm.read[:160]}{'...' if len(perm.read) > 160 else ''}\n")

    if args.dry_run:
        return 0

    if not args.user or not args.password:
        raise SystemExit("--user and --password are required unless --dry-run")

    vhost = args.vhost.replace("/", "%2F")
    call = lambda m, path, body=None: api(  # noqa: E731
        args.host, args.port, args.admin_user, args.admin_pass, m, path, body
    )

    # Topic permissions can only be set on an exchange that exists, and on a fresh
    # broker none do until a node starts. Declare them from the contract -- idempotent,
    # and it means permissions can be provisioned before any node is running.
    for contract in REGISTRY.values():
        call("PUT", f"/exchanges/{vhost}/{contract.exchange}", {
            "type": contract.exchange_type,
            "durable": True,
        })
    print(f"exchanges ensured: {', '.join(sorted({c.exchange for c in REGISTRY.values()}))}")

    call("PUT", f"/users/{args.user}", {"password": args.password, "tags": ""})
    print(f"user {args.user!r} created/updated")

    # Resource permissions, which are about *names* -- topic permissions (below) are
    # about routing keys, and both have to be right.
    #
    #   configure  which queues/exchanges it may declare -> depends on the role
    #   write      publish to exchange / bind queue      -> our exchanges + its queues
    #   read       bind to exchange / consume queue      -> our exchanges + its queues
    #
    # A node is not the threat model (see policy.node_permissions): it declares its
    # own durable work queue `q.<cap>.req`, redeclares its exchange on every start,
    # then binds and consumes that queue -- all four need the name in scope. The same
    # goes for admin, which policy also grants ".*". So those two get ".*" here, in
    # step with their topic permissions below.
    #
    # viewer/operator (browsers) do NOT: `read` must NOT be ".*" for them, or a viewer
    # could consume from `q.rheed.camera.req` -- the servers' shared work queue -- and
    # quietly steal requests off the bus, an information leak and a denial of service.
    # Their anonymous queues are `amq_<hex>` (aio_pika) or `amq.gen-<...>` (broker),
    # hence `amq[._]`.
    if args.role in ("node", "admin"):
        resource_perms = {"configure": ".*", "write": ".*", "read": ".*"}
        summary = "unrestricted (trusted role)"
    else:
        own_queues = r"amq[._].*"
        exchanges = "|".join(
            re.escape(x) for x in sorted({c.exchange for c in REGISTRY.values()})
        )
        resource_perms = {
            "configure": f"^({own_queues})$",
            "write": f"^({own_queues}|{exchanges})$",
            "read": f"^({own_queues}|{exchanges})$",
        }
        summary = "own queues + lumi exchanges only"
    call("PUT", f"/permissions/{vhost}/{args.user}", resource_perms)
    print(f"resource permissions set ({summary})")

    for exchange, perm in sorted(permissions.items()):
        call("PUT", f"/topic-permissions/{vhost}/{args.user}", {
            "exchange": exchange,
            "write": perm.write,
            "read": perm.read,
        })
        print(f"topic permissions set on {exchange}")

    print(f"\napplied role {args.role!r} to {args.user!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
