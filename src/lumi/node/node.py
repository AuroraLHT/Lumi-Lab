"""One node per equipment.

A node owns its connection, its channel, its exchange, its worker threads, and every
capability its contract declares -- broadcast, request/response, and pub/sub together
in one place, which is the thing this refactor was asked for.

It also has a lifecycle, which the old nodes did not. Every one of them ended in
`await asyncio.Future()` with the shutdown code written out underneath, permanently
unreachable:

    await asyncio.Future()

    await chamber_mq.cancel()      # nodes/pascal.py:257 -- never runs
    log_reader.stop()              # never runs
    camera.join()                  # never runs

Ctrl-C threw KeyboardInterrupt out of asyncio.run, the daemon threads died with the
process, and the broker reaped the exclusive queues. It worked, but nothing was ever
closed on purpose. Now SIGTERM/SIGINT run drain(): announce departure, stop taking
work, finish what is in flight, join the threads, close the connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import threading
import uuid
from datetime import UTC, datetime

from aio_pika import ExchangeType, connect_robust
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange

from lumi.base.mq import CapabilityServer
from lumi.contracts import EquipmentContract

from .heartbeat import HeartbeatPublisher

log = logging.getLogger(__name__)


class EquipmentNode:
    def __init__(
        self,
        contract: EquipmentContract,
        *,
        amqp_url: str,
        instance_id: str | None = None,
        heartbeat_interval: float = 2.0,
        prefetch: int = 8,
        drain_timeout: float = 10.0,
    ) -> None:
        self.contract = contract
        self.amqp_url = amqp_url
        # Distinct per process. The API runs 10 uvicorn workers, and under the old
        # scheme all ten opened clients with identical names.
        self.instance_id = instance_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.heartbeat_interval = heartbeat_interval
        self.prefetch = prefetch
        self.drain_timeout = drain_timeout
        self.started_at = datetime.now(UTC).isoformat()

        self._handlers: dict[str, object] = {}
        self._servers: dict[str, CapabilityServer] = {}
        self._threads: list[threading.Thread] = []
        self._on_drain: list = []

        self.connection: AbstractConnection | None = None
        self.channel: AbstractChannel | None = None
        self.exchange: AbstractExchange | None = None
        self._heartbeat: HeartbeatPublisher | None = None
        self._shutdown = asyncio.Event()
        self._draining = False

    # --- composition ------------------------------------------------------

    def mount(self, capability: str, handler: object) -> None:
        """Attach a handler to a capability.

        The handler is plain domain code: `async def <op>(self, req)` for each op,
        a `state()`, and `async def next()` if it streams. It never sees AMQP.
        """
        cap = self.contract.capability(capability)  # raises if not in the contract
        self._handlers[cap.name] = handler

    def thread(self, t: threading.Thread) -> None:
        """Register a worker thread (camera, compressor, log reader) so drain() joins it."""
        self._threads.append(t)

    def on_drain(self, fn) -> None:
        """Register a cleanup callback to run during drain, before threads are joined."""
        self._on_drain.append(fn)

    def request_shutdown(self) -> None:
        """Ask the node to exit. Called by the `shutdown` control verb, and by signals."""
        self._shutdown.set()

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        missing = {c.name for c in self.contract.capabilities} - set(self._handlers)
        if missing:
            raise RuntimeError(
                f"{self.contract.name}: no handler mounted for {sorted(missing)}. "
                f"The contract says this node serves them."
            )

        self.connection = await connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=self.prefetch)
        self.exchange = await self.channel.declare_exchange(
            self.contract.exchange, ExchangeType(self.contract.exchange_type), durable=True
        )

        for cap in self.contract.capabilities:
            server = CapabilityServer(
                cap, self.contract.name, self._handlers[cap.name],
                channel=self.channel, exchange=self.exchange,
                instance_id=self.instance_id,
            )
            server.node = self  # so the `shutdown` verb can reach us
            await server.start()
            self._servers[cap.name] = server

        for t in self._threads:
            if not t.is_alive():
                t.start()

        self._heartbeat = HeartbeatPublisher(
            node=self, channel=self.channel, interval=self.heartbeat_interval
        )
        await self._heartbeat.start()

        log.info(
            "node %s/%s up: %s",
            self.contract.name, self.instance_id,
            ", ".join(self._servers),
        )

    async def drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        log.info("node %s/%s draining", self.contract.name, self.instance_id)

        # Say goodbye first, so the monitor marks us as a clean stop rather than
        # waiting for the heartbeat to expire and calling it a crash.
        if self._heartbeat is not None:
            await self._heartbeat.announce_leaving()
            await self._heartbeat.stop()

        for server in self._servers.values():
            await server.drain(timeout=self.drain_timeout)

        for fn in self._on_drain:
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("drain callback failed")

        for t in self._threads:
            stop = getattr(t, "stop", None)
            if callable(stop):
                stop()
        for t in self._threads:
            t.join(timeout=5.0)
            if t.is_alive():
                log.warning("thread %s did not stop within 5s", t.name)

        if self.channel is not None and not self.channel.is_closed:
            await self.channel.close()
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()

        log.info("node %s/%s down", self.contract.name, self.instance_id)

    async def serve_forever(self) -> None:
        """Run until a signal or a `shutdown` control message arrives."""
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.drain()

    def run(self) -> None:
        """Entry point for a node script. Installs signal handlers and blocks."""

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.request_shutdown)
                except NotImplementedError:
                    # Windows: the chamber PC. add_signal_handler is unsupported there.
                    signal.signal(sig, lambda *_: self.request_shutdown())
            await self.serve_forever()

        asyncio.run(_main())

    # --- introspection ----------------------------------------------------

    @property
    def servers(self) -> dict[str, CapabilityServer]:
        return dict(self._servers)
