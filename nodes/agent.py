"""The host agent: the one process on each machine that starts and stops the others.

    python -m nodes.agent --host <broker>

This is the only thing that needs to be running at boot (a systemd unit, or a
scheduled task on the Windows chamber PC). Everything else can be started through it.

It is not an EquipmentNode: it is addressed per-host at `agent.<hostname>.cmd`, so a
spawn meant for the camera host is never answered by the CUDA box.

What it is allowed to run comes from *this host's own* settings.toml, never from the
bus:

    [agent.nodes.rheed]
    module = "nodes.rheed"
    flags = ["--src", "--host"]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

from aio_pika import DeliveryMode, ExchangeType, Message, connect_robust

from lumi.config import settings
from lumi.contracts import contract_hash
from lumi.contracts.payloads.system import CapabilityPresence, Heartbeat
from lumi.contracts.system import SYSTEM
from lumi.node.heartbeat import presence_key
from lumi.system.agent import HostAgent, NodeSpec

log = logging.getLogger(__name__)


def load_allowlist() -> dict[str, NodeSpec]:
    """Read what this host may run. A host with no [agent.nodes] section may run nothing."""
    raw = settings.get("agent", {}).get("nodes", {}) if settings.get("agent") else {}
    allowlist: dict[str, NodeSpec] = {}
    for name, spec in dict(raw).items():
        spec = dict(spec)
        allowlist[str(name)] = NodeSpec(
            module=spec["module"],
            allowed_flags=tuple(spec.get("flags", [])),
        )
    return allowlist


async def heartbeat_loop(agent: HostAgent, channel, interval: float) -> None:
    """Agents advertise themselves too, so the monitor can tell 'the node is down'
    from 'the whole host is unreachable'."""
    exchange = await channel.declare_exchange(SYSTEM.exchange, ExchangeType.TOPIC, durable=True)
    started = datetime.now(UTC).isoformat()
    instance_id = f"agent-{agent.host}"
    seq = 0

    while True:
        state = agent.state()
        hb = Heartbeat(
            equipment="agent",
            instance_id=instance_id,
            host=agent.host,
            pid=os.getpid(),
            contract_version=SYSTEM.version,
            contract_hash=contract_hash(),
            started_at=started,
            seq=seq,
            interval_s=interval,
            capabilities=[
                CapabilityPresence(
                    name="agent", kind="rpc", is_running=True,
                    state=state.model_dump(mode="json"),
                )
            ],
        )
        try:
            await exchange.publish(
                Message(
                    body=hb.model_dump_json().encode(),
                    expiration=interval * 3,
                    delivery_mode=DeliveryMode.NOT_PERSISTENT,
                ),
                routing_key=presence_key("agent", instance_id),
            )
        except Exception:
            log.debug("agent heartbeat failed", exc_info=True)
        seq += 1
        await asyncio.sleep(interval)


async def main(args: argparse.Namespace) -> None:
    allowlist = load_allowlist()
    if not allowlist:
        log.warning(
            "no [agent.nodes] in settings for host %s -- this agent may not start anything",
            socket.gethostname(),
        )

    agent = HostAgent(allowlist, host=args.hostname)
    conn = await connect_robust(f"amqp://{args.user}:{args.password}@{args.host}/")
    channel = await conn.channel()

    await agent.listen(channel)
    hb = asyncio.create_task(heartbeat_loop(agent, channel, args.interval))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        import signal as _signal

        try:
            loop.add_signal_handler(getattr(_signal, sig), stop.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows

    log.info("agent up on %s; may run: %s", agent.host, sorted(allowlist) or "(nothing)")
    try:
        await stop.wait()
    finally:
        hb.cancel()
        # Do NOT kill the nodes we spawned: an agent restart must not take the
        # experiment down with it. They keep running and re-attach on the next spawn.
        await conn.close()
        log.info("agent down")


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-agent", description="Per-host process control")
    parser.add_argument("--host", default=settings.rabbitmq.host, help="broker host")
    parser.add_argument("--hostname", default=socket.gethostname(), help="this machine's name on the bus")
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
