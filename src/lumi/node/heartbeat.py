"""Presence: nodes announce themselves.

None of this existed. "Is the node up?" was answered by firing an RPC at a hardcoded
list of seven clients and seeing which ones failed to answer within one second --
which conflates "the node is dead", "the node is busy", and "the network hiccuped".

Now every node publishes a heartbeat to the `lumi.system` topic exchange on
`presence.<equipment>.<instance_id>`, carrying its capabilities, their state, and the
contract hash it was built against. Liveness is heartbeat expiry. Departure is
announced. And a node running a stale contract is *visible* rather than merely
strange.

The heartbeat is fire-and-forget: nothing here blocks on the monitor existing, so a
monitor that is down or restarting cannot take the lab with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import TYPE_CHECKING

from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange

from lumi.contracts import contract_hash
from lumi.contracts.payloads.system import CapabilityPresence, Heartbeat
from lumi.contracts.system import SYSTEM

if TYPE_CHECKING:
    from .node import EquipmentNode

log = logging.getLogger(__name__)


def presence_key(equipment: str, instance_id: str) -> str:
    return f"presence.{equipment}.{instance_id}"


def leaving_key(equipment: str, instance_id: str) -> str:
    return f"presence.leave.{equipment}.{instance_id}"


class HeartbeatPublisher:
    def __init__(self, node: "EquipmentNode", channel: AbstractChannel, interval: float = 2.0) -> None:
        self.node = node
        self.channel = channel
        self.interval = interval
        self.seq = 0
        self._exchange: AbstractExchange | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._exchange = await self.channel.declare_exchange(
            SYSTEM.exchange, ExchangeType.TOPIC, durable=True
        )
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat:{self.node.instance_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _build(self) -> Heartbeat:
        caps = []
        for name, server in self.node.servers.items():
            server.refresh_state()
            state = server.state.state
            caps.append(
                CapabilityPresence(
                    name=name,
                    kind=str(server.cap.kind),
                    is_running=getattr(state, "is_running", False),
                    is_streaming=getattr(state, "is_streaming", False),
                    state=state.model_dump(mode="json"),
                )
            )
        return Heartbeat(
            equipment=self.node.contract.name,
            instance_id=self.node.instance_id,
            host=socket.gethostname(),
            pid=os.getpid(),
            contract_version=self.node.contract.version,
            contract_hash=contract_hash(),
            started_at=self.node.started_at,
            seq=self.seq,
            interval_s=self.interval,
            capabilities=caps,
        )

    async def _publish(self, hb: Heartbeat, routing_key: str) -> None:
        assert self._exchange is not None
        await self._exchange.publish(
            Message(
                body=hb.model_dump_json().encode(),
                # Expire after three missed beats. Without this, a heartbeat sitting
                # in a backed-up queue could resurrect a node that is long dead.
                expiration=self.interval * 3,
                delivery_mode=DeliveryMode.NOT_PERSISTENT,
            ),
            routing_key=routing_key,
            # No monitor running is a perfectly normal state -- a node must not raise
            # (or, with aio_pika's mandatory default, be handed back its own heartbeat
            # as undeliverable) just because nobody happens to be watching.
            mandatory=False,
        )

    async def _loop(self) -> None:
        while True:
            try:
                hb = self._build()
                await self._publish(hb, presence_key(hb.equipment, hb.instance_id))
                self.seq += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broker blip must never take the node down with it.
                log.debug("heartbeat publish failed", exc_info=True)
            await asyncio.sleep(self.interval)

    async def announce_leaving(self) -> None:
        """Say we are going, so the monitor records a clean stop rather than a crash."""
        try:
            hb = self._build()
            await self._publish(hb, leaving_key(hb.equipment, hb.instance_id))
        except Exception:
            log.debug("could not announce departure", exc_info=True)
