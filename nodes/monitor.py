"""The monitor node: knows what is on and off, and can start and stop it.

    python -m nodes.monitor --host <broker>

It is an ordinary EquipmentNode over the SYSTEM contract. It happens to be the one
whose handlers are about other nodes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from lumi.config import settings
from lumi.contracts.system import SYSTEM
from lumi.node import EquipmentNode
from lumi.system.monitor import RegistryHandler, SupervisorHandler

log = logging.getLogger(__name__)


async def main(args: argparse.Namespace) -> None:
    node = EquipmentNode(
        SYSTEM,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        heartbeat_interval=args.interval,
    )

    registry = RegistryHandler()
    supervisor = SupervisorHandler(registry)

    node.mount("registry", registry)
    node.mount("supervisor", supervisor)

    await node.start()

    # Presence intake and the supervisor's reply queue need the node's channel, so
    # they are wired after start().
    assert node.channel is not None
    await registry.listen(node.channel)
    await supervisor.connect(node.channel)
    node.on_drain(registry.stop)

    log.info("monitor up; watching presence.*")
    try:
        await node._shutdown.wait()
    finally:
        await node.drain()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-monitor", description="Node registry and supervisor")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--interval", type=float, default=2.0, help="own heartbeat interval (s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
