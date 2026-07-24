"""The monitor: registry + supervisor.

It is an ordinary EquipmentNode over the SYSTEM contract -- no special casing. It
consumes presence heartbeats, keeps a registry, streams changes, and forwards
spawn/kill to the agent on the target host.

It deliberately holds no authority over what may run: the allowlist lives on each
agent, locally. The monitor can only ask.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from aio_pika import ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage

from lumi.contracts import contract_hash
from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.system import (
    Heartbeat,
    HostInfo,
    HostList,
    KillRequest,
    NodeList,
    NodeQuery,
    NodeRecord,
    NodeStatus,
    RegistryEvent,
    RegistryReadout,
    SpawnRequest,
    SupervisorReadout,
    WaitForNode,
)
from lumi.contracts.system import SYSTEM

from .registry import NodeRegistry

log = logging.getLogger(__name__)

REAP_INTERVAL = 1.0


class RegistryHandler:
    """Serves the `registry` capability: who is on the bus, and a stream of changes."""

    def __init__(self) -> None:
        self.registry = NodeRegistry()
        self._events: asyncio.Queue[RegistryEvent] = asyncio.Queue(maxsize=1000)
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._tasks: list[asyncio.Task] = []

    # --- presence intake --------------------------------------------------

    async def listen(self, channel: AbstractChannel) -> None:
        exchange = await channel.declare_exchange(SYSTEM.exchange, ExchangeType.TOPIC, durable=True)

        presence = await channel.declare_queue(exclusive=True)
        await presence.bind(exchange, routing_key="presence.*.*")
        await presence.consume(self._on_presence, no_ack=True)

        leaving = await channel.declare_queue(exclusive=True)
        await leaving.bind(exchange, routing_key="presence.leave.*.*")
        await leaving.consume(self._on_leaving, no_ack=True)

        self._tasks.append(asyncio.create_task(self._reap_loop(), name="registry-reaper"))

    async def _on_presence(self, message: AbstractIncomingMessage) -> None:
        try:
            hb = Heartbeat.model_validate_json(message.body)
        except Exception:
            log.warning("undecodable heartbeat", exc_info=True)
            return
        event = self.registry.on_heartbeat(hb)
        if event is not None:
            await self._emit(event)
        self._wake_waiters(hb.equipment)

    async def _on_leaving(self, message: AbstractIncomingMessage) -> None:
        try:
            hb = Heartbeat.model_validate_json(message.body)
        except Exception:
            return
        event = self.registry.on_leaving(hb)
        if event is not None:
            await self._emit(event)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            for event in self.registry.reap():
                await self._emit(event)

    async def _emit(self, event: RegistryEvent) -> None:
        log.info("%s: %s/%s", event.event, event.node.equipment, event.node.instance_id)
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            # A monitor with no subscribers must not grow without bound.
            self._events.get_nowait()
            self._events.put_nowait(event)

    def _wake_waiters(self, equipment: str) -> None:
        record = self.registry.get(equipment)
        if record is None:
            return
        for fut in self._waiters.pop(equipment, []):
            if not fut.done():
                fut.set_result(record)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # --- contract ops -----------------------------------------------------

    async def list_nodes(self, req: Empty) -> NodeList:
        return NodeList(nodes=self.registry.list_nodes())

    async def get_node(self, req: NodeQuery) -> NodeRecord:
        record = self.registry.get(req.equipment, req.instance_id)
        if record is None:
            raise KeyError(f"no node for {req.equipment!r}")
        return record

    async def wait_for(self, req: WaitForNode) -> NodeRecord:
        """Block until an instance of `equipment` is up.

        This is what lets storage wait for its sources instead of assuming they are
        there and failing opaquely halfway through a growth.
        """
        record = self.registry.get(req.equipment)
        if record is not None:
            return record

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(req.equipment, []).append(fut)
        try:
            return await asyncio.wait_for(fut, req.timeout_s)
        except TimeoutError:
            raise TimeoutError(f"{req.equipment} did not appear within {req.timeout_s}s") from None

    async def next(self) -> RegistryEvent | None:
        try:
            return self._events.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def readout(self) -> RegistryReadout:
        return RegistryReadout(
            n_nodes=len(self.registry.list_nodes()),
            n_up=self.registry.n_up,
            contract_hash=contract_hash(),
        )


class SupervisorHandler:
    """Serves the `supervisor` capability by forwarding to the agent on each host.

    The monitor cannot spawn a process on a remote machine by itself -- something has
    to already be listening there. That something is the host agent.
    """

    def __init__(self, registry: RegistryHandler, timeout: float = 15.0) -> None:
        self.registry = registry
        self.timeout = timeout
        self.channel: AbstractChannel | None = None
        self.exchange: AbstractExchange | None = None
        self._reply = None
        self._futures: dict[str, asyncio.Future] = {}

    async def connect(self, channel: AbstractChannel) -> None:
        self.channel = channel
        self.exchange = await channel.declare_exchange(SYSTEM.exchange, ExchangeType.TOPIC, durable=True)
        self._reply = await channel.declare_queue(exclusive=True)
        await self._reply.consume(self._on_reply, no_ack=True)

    async def _on_reply(self, message: AbstractIncomingMessage) -> None:
        fut = self._futures.pop(message.correlation_id or "", None)
        if fut and not fut.done():
            fut.set_result((message.body, dict(message.headers or {})))

    async def _ask_agent(self, host: str, verb: str, body: bytes) -> dict[str, Any]:
        assert self.exchange is not None and self._reply is not None
        cid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[cid] = fut
        await self.exchange.publish(
            Message(body=body, headers={"agent_op": verb}, correlation_id=cid,
                    reply_to=self._reply.name),
            routing_key=f"agent.{host}.cmd",
        )
        try:
            _, headers = await asyncio.wait_for(fut, self.timeout)
        except TimeoutError:
            self._futures.pop(cid, None)
            raise TimeoutError(
                f"no agent answered on host {host!r}. Is lumi-agent running there?"
            ) from None
        if not headers.get("succ", False):
            raise RuntimeError(f"agent on {host}: {headers.get('error_message', 'failed')}")
        return headers

    # --- contract ops -----------------------------------------------------

    async def list_hosts(self, req: Empty) -> HostList:
        hosts: dict[str, HostInfo] = {}
        for node in self.registry.registry.list_nodes():
            if node.status is not NodeStatus.UP:
                continue
            hosts.setdefault(node.host, HostInfo(host=node.host))
        return HostList(hosts=list(hosts.values()))

    async def spawn(self, req: SpawnRequest) -> Ack:
        await self._ask_agent(req.host, "spawn", req.model_dump_json().encode())
        return Ack()

    async def kill(self, req: KillRequest) -> Ack:
        host = self._host_of(req.instance_id)
        await self._ask_agent(host, "kill", req.model_dump_json().encode())
        return Ack()

    async def restart(self, req: KillRequest) -> Ack:
        record = self._record_of(req.instance_id)
        await self.kill(req)
        # Wait for it to actually go before starting a replacement, or two processes
        # end up competing for the same camera.
        for _ in range(50):
            if not self.registry.registry.get(record.equipment, record.instance_id):
                break
            await asyncio.sleep(0.2)
        await self.spawn(SpawnRequest(host=record.host, node=record.equipment))
        return Ack()

    def _record_of(self, instance_id: str) -> NodeRecord:
        for node in self.registry.registry.list_nodes():
            if node.instance_id == instance_id:
                return node
        raise KeyError(f"unknown instance {instance_id!r}")

    def _host_of(self, instance_id: str) -> str:
        return self._record_of(instance_id).host

    def readout(self) -> SupervisorReadout:
        hosts = {n.host for n in self.registry.registry.list_nodes()}
        return SupervisorReadout(n_hosts=len(hosts))
