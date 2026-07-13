"""The storage node: records the other nodes' streams into HDF5.

    python -m nodes.storage

A consumer, not an instrument. It records what RHEED, detection and the chamber are
already broadcasting -- so unlike them it needs clients on two other exchanges, and it
asks the monitor whether its sources exist before it promises to record anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aio_pika import ExchangeType

from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.payloads.common import Empty
from lumi.contracts.rheed import RHEED
from lumi.contracts.storage import STORAGE_NODE
from lumi.contracts.system import SYSTEM
from lumi.generated.clients.chamber import ChamberLogClient
from lumi.generated.clients.detection import DetectionDetectionClient
from lumi.generated.clients.rheed import RheedCameraClient, RheedIntegratorClient
from lumi.generated.clients.system import SystemRegistryClient
from lumi.node import EquipmentNode
from lumi.storage.handlers import StorageHandler

log = logging.getLogger(__name__)


async def main(args: argparse.Namespace) -> None:
    node = EquipmentNode(
        STORAGE_NODE,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )

    # The handler needs clients, and the clients need the node's channel, so it is
    # mounted with empty sources and filled in once the node is connected.
    handler = StorageHandler(sources={}, registry_client=None, root_folder=args.root)
    node.mount("storage", handler)
    await node.start()

    channel = node.channel
    assert channel is not None

    rheed_x = await channel.declare_exchange(
        RHEED.exchange, ExchangeType(RHEED.exchange_type), durable=True
    )
    chamber_x = await channel.declare_exchange(
        CHAMBER.exchange, ExchangeType(CHAMBER.exchange_type), durable=True
    )
    system_x = await channel.declare_exchange(
        SYSTEM.exchange, ExchangeType(SYSTEM.exchange_type), durable=True
    )

    sources = {
        "camera": RheedCameraClient(channel, rheed_x),
        "integrator": RheedIntegratorClient(channel, rheed_x),
        "detection": DetectionDetectionClient(channel, rheed_x),
        "log": ChamberLogClient(channel, chamber_x),
    }
    registry = SystemRegistryClient(channel, system_x)

    for client in (*sources.values(), registry):
        await client.start()

    handler.sources = sources
    handler.registry = registry

    async def watch_deps() -> None:
        """Keep deps_available current, so `is the camera up?` is answerable before
        someone asks us to record rather than only at the moment we fail."""
        while True:
            await handler.refresh_deps()
            await asyncio.sleep(5.0)

    deps_task = asyncio.create_task(watch_deps(), name="storage-deps")

    async def stop_clients() -> None:
        deps_task.cancel()
        for client in (*sources.values(), registry):
            await client.stop()

    node.on_drain(stop_clients)

    log.info("storage node up; sources: %s", ", ".join(sources))
    try:
        await node._shutdown.wait()
    finally:
        if handler.recorder_server is not None:
            # Never leave a half-written HDF5 file behind because someone hit Ctrl-C.
            log.warning("draining while recording %r -- closing the file", handler.project_name)
            await handler.stop_recording(Empty())
        await node.drain()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-storage", description="HDF5 recording node")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None)
    parser.add_argument("--root", default=None,
                        help="where to write HDF5 files (defaults to storage.hdf5_recorder.database_path)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
