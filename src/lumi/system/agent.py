"""The host agent: process control for one machine.

This is the only component that can start a process, so it is the only place where a
security boundary exists, and the rules are deliberately blunt:

  * `SpawnRequest.node` is a KEY into a local allowlist, never a command line. The
    agent resolves it to `[sys.executable, "-m", <module>, *args]`.
  * The allowlist lives in this host's own config file. The bus cannot add to it.
  * Extra args are matched against a per-node allowlist of flags, so `--src` can be
    passed but `--; rm -rf /` cannot.

If any of that were relaxed -- if the agent ran a string from the bus -- then anyone
who can publish to the broker could run arbitrary code on the lab machines, and the
broker currently authenticates with guest:guest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime

from aio_pika import ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.system import (
    AgentReadout,
    KillRequest,
    ProcessInfo,
    ProcessList,
    SpawnRequest,
)
from lumi.contracts.system import SYSTEM

log = logging.getLogger(__name__)


class NodeSpec:
    """An entry in the allowlist: what this host is permitted to run."""

    def __init__(self, module: str, allowed_flags: tuple[str, ...] = ()) -> None:
        self.module = module
        self.allowed_flags = set(allowed_flags)


class Process:
    def __init__(self, instance_id: str, node: str, popen: subprocess.Popen) -> None:
        self.instance_id = instance_id
        self.node = node
        self.popen = popen
        self.started_at = datetime.now(UTC).isoformat()

    @property
    def running(self) -> bool:
        return self.popen.poll() is None

    def info(self, host: str) -> ProcessInfo:
        return ProcessInfo(
            instance_id=self.instance_id, node=self.node, host=host,
            pid=self.popen.pid, started_at=self.started_at, running=self.running,
        )


class HostAgent:
    """Serves the `agent` capability on `agent.<hostname>.cmd`."""

    def __init__(self, allowlist: dict[str, NodeSpec], *, host: str | None = None) -> None:
        self.host = host or socket.gethostname()
        self.allowlist = allowlist
        self.processes: dict[str, Process] = {}
        self.channel: AbstractChannel | None = None
        self._exchange = None

    async def listen(self, channel: AbstractChannel) -> None:
        self.channel = channel
        self._exchange = await channel.declare_exchange(SYSTEM.exchange, ExchangeType.TOPIC, durable=True)
        q = await channel.declare_queue(exclusive=True)
        await q.bind(self._exchange, routing_key=f"agent.{self.host}.cmd")
        await q.consume(self._on_command, no_ack=True)
        log.info("agent on %s listening; may run: %s", self.host, sorted(self.allowlist))

    async def _on_command(self, message: AbstractIncomingMessage) -> None:
        verb = (message.headers or {}).get("agent_op", "")
        succ, error = True, ""
        try:
            if verb == "spawn":
                await self.spawn(SpawnRequest.model_validate_json(message.body))
            elif verb == "kill":
                await self.kill(KillRequest.model_validate_json(message.body))
            else:
                succ, error = False, f"unknown agent op {verb!r}"
        except Exception as exc:
            log.exception("agent op %r failed", verb)
            succ, error = False, f"{type(exc).__name__}: {exc}"

        if message.reply_to and self.channel is not None:
            await self.channel.default_exchange.publish(
                Message(
                    body=b"",
                    headers={"succ": succ, "error_message": error},
                    correlation_id=message.correlation_id,
                ),
                routing_key=message.reply_to,
            )

    # --- contract ops -----------------------------------------------------

    async def spawn(self, req: SpawnRequest) -> Ack:
        spec = self.allowlist.get(req.node)
        if spec is None:
            # The refusal is the feature.
            raise PermissionError(
                f"{req.node!r} is not in this host's allowlist ({sorted(self.allowlist)})"
            )

        for arg in req.args:
            flag = arg.split("=", 1)[0]
            if flag.startswith("-") and flag not in spec.allowed_flags:
                raise PermissionError(f"flag {flag!r} is not permitted for {req.node!r}")

        instance_id = req.instance_id or f"{self.host}-{req.node}-{uuid.uuid4().hex[:6]}"
        if instance_id in self.processes and self.processes[instance_id].running:
            raise RuntimeError(f"{instance_id} is already running")

        cmd = [sys.executable, "-m", spec.module, *req.args]
        log.info("spawning %s: %s", instance_id, shlex.join(cmd))
        popen = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # New process group, so killing the node does not kill the agent.
            start_new_session=os.name != "nt",
        )
        self.processes[instance_id] = Process(instance_id, req.node, popen)
        return Ack()

    async def kill(self, req: KillRequest) -> Ack:
        proc = self.processes.get(req.instance_id)
        if proc is None:
            raise KeyError(f"{req.instance_id!r} is not running on {self.host}")
        if not proc.running:
            return Ack()

        # SIGTERM first: the node's own drain() handles it, announces its departure,
        # and finishes in-flight work. SIGKILL only if it will not go.
        proc.popen.send_signal(signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + req.grace_s
        while proc.running and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

        if proc.running:
            log.warning("%s ignored SIGTERM for %.0fs; killing", req.instance_id, req.grace_s)
            proc.popen.kill()
        return Ack()

    async def list_processes(self, req: Empty) -> ProcessList:
        return ProcessList(processes=[p.info(self.host) for p in self.processes.values()])

    def readout(self) -> AgentReadout:
        return AgentReadout(
            host=self.host,
            available_nodes=sorted(self.allowlist),
            n_processes=sum(1 for p in self.processes.values() if p.running),
        )
