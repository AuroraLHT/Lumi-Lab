"""The experiment node: drives a PLD growth end to end.

    python -m nodes.experiment

A consumer, not an instrument, like storage -- it needs clients on the chamber,
RHEED, and storage exchanges, and asks the monitor whether they exist before
promising to drive anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aio_pika import ExchangeType

from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.rheed import RHEED
from lumi.contracts.storage import STORAGE_NODE
from lumi.contracts.system import SYSTEM
from lumi.experiment.db import GrowthDB
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.journal import StepJournal
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration
from lumi.experiment.mi import MiCommandRunner
from lumi.generated.clients.chamber import ChamberConfigClient, ChamberLogClient, ChamberMiModeClient
from lumi.generated.clients.rheed import RheedCameraClient
from lumi.generated.clients.storage import StorageStorageClient
from lumi.generated.clients.system import SystemRegistryClient
from lumi.node import EquipmentNode

log = logging.getLogger(__name__)


def _pld_config() -> PLDChamberConfiguration:
    cfg = settings.experiment.pld_config
    return PLDChamberConfiguration(
        center_mask_pos=cfg.center_mask_pos,
        center_rheed_pos=cfg.center_rheed_pos,
        plumb_center=cfg.plumb_center,
        target_to_mask_distance=cfg.target_to_mask_distance,
        mask_to_sample_distance=cfg.mask_to_sample_distance,
        rheed_limit=tuple(cfg.rheed_limit),
        mask_block_position=cfg.mask_block_position,
    )


def _bounds() -> ExperimentBounds:
    b = settings.experiment.bounds
    return ExperimentBounds(
        mask_travel_max=b.mask_travel_max,
        temperature_min=b.temperature_min,
        temperature_max=b.temperature_max,
        temperature_pid_engage_threshold=b.temperature_pid_engage_threshold,
        warm_up_step=b.warm_up_step,
        warm_up_current_ramp_rate=b.warm_up_current_ramp_rate,
        warm_up_wait_interval=b.warm_up_wait_interval,
        warm_up_max_waittime=b.warm_up_max_waittime,
    )


async def main(args: argparse.Namespace) -> None:
    node = EquipmentNode(
        EXPERIMENT,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )

    growth_db = GrowthDB(args.db_path or settings.experiment.growth_db_path)
    await growth_db.connect()
    await growth_db.create_database()

    # The handler needs clients, and the clients need the node's channel, so it is
    # mounted with empty sources and filled in once the node is connected -- same
    # two-step wiring nodes/storage.py uses.
    handler = ExperimentHandler(
        sources={}, growth_db=growth_db, pld_config=_pld_config(), bounds=_bounds(),
        target_mapper=dict(settings.experiment.target_mapper), registry_client=None,
    )

    # The step journal has to exist before node.start(), which is where the capability
    # servers are built and where each one picks up its handler's journal. Opening the
    # session here is what makes "this chamber run" a thing steps can chain within.
    handler.journal = StepJournal(
        growth_db,
        node_instance=node.instance_id,
        sample_resolver=handler.current_sample_id,
    )
    await handler.journal.open_session()

    node.mount("driver", handler)
    await node.start()

    channel = node.channel
    assert channel is not None

    chamber_x = await channel.declare_exchange(
        CHAMBER.exchange, ExchangeType(CHAMBER.exchange_type), durable=True
    )
    rheed_x = await channel.declare_exchange(
        RHEED.exchange, ExchangeType(RHEED.exchange_type), durable=True
    )
    storage_x = await channel.declare_exchange(
        STORAGE_NODE.exchange, ExchangeType(STORAGE_NODE.exchange_type), durable=True
    )
    system_x = await channel.declare_exchange(
        SYSTEM.exchange, ExchangeType(SYSTEM.exchange_type), durable=True
    )

    chamber_mi_client = ChamberMiModeClient(channel, chamber_x)
    # 0 (or negative) means "no deadline": the script finishes when it finishes. See the
    # setting's comment in cfg/settings.example.toml for what that gives up.
    mi_timeout = args.mi_timeout if args.mi_timeout is not None else float(settings.experiment.mi_command_timeout)
    if mi_timeout <= 0:
        mi_timeout = None
        log.warning("MI completion waits are unbounded -- a lost update will hang the op that is waiting")
    sources = {
        "chamber_mi": MiCommandRunner(chamber_mi_client, timeout=mi_timeout),
        "chamber_log": ChamberLogClient(channel, chamber_x),
        "chamber_config": ChamberConfigClient(channel, chamber_x),
        "rheed_camera": RheedCameraClient(channel, rheed_x),
        "storage": StorageStorageClient(channel, storage_x),
    }
    registry = SystemRegistryClient(channel, system_x)

    # MiCommandRunner is not itself a CapabilityClient -- start the client it wraps.
    startable = [c for c in sources.values() if not isinstance(c, MiCommandRunner)]
    startable.append(chamber_mi_client)
    for client in (*startable, registry):
        await client.start()

    handler.sources = sources
    handler.registry = registry
    handler.build_manager()

    async def watch_deps() -> None:
        while True:
            await handler.refresh_deps()
            await asyncio.sleep(5.0)

    deps_task = asyncio.create_task(watch_deps(), name="experiment-deps")

    async def stop_clients() -> None:
        deps_task.cancel()
        for client in (*startable, registry):
            await client.stop()
        await handler.journal.close_session()
        await growth_db.close()

    node.on_drain(stop_clients)

    log.info("experiment node up; sources: %s", ", ".join(sources))
    try:
        await node._shutdown.wait()
    finally:
        await node.drain()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-experiment", description="PLD experiment driver node")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None)
    parser.add_argument("--db-path", default=None,
                        help="growth database path (defaults to experiment.growth_db_path)")
    parser.add_argument("--mi-timeout", type=float, default=None,
                        help="seconds to wait for an MI script to finish; 0 = wait forever "
                             "(defaults to experiment.mi_command_timeout)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
