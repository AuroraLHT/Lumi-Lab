"""Presence, discovery and supervision payloads.

None of this existed before: liveness was inferred by firing an RPC at a hardcoded
list of seven clients and seeing which ones timed out within one second.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .common import ServerStateBase


class NodeStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    LEAVING = "leaving"  # announced a clean shutdown


class CapabilityPresence(BaseModel):
    name: str
    kind: str
    is_running: bool = False
    is_streaming: bool = False
    # The capability's own state model, dumped. Already validated node-side.
    state: dict[str, object] = {}


class Heartbeat(BaseModel):
    """What a node says about itself, every interval_s seconds.

    contract_hash is the load-bearing field: Pascal runs on a Windows box,
    detection on a CUDA box, RHEED on the camera host, and they are updated at
    different times. Without it, a node deployed against a stale contract just
    misbehaves quietly.
    """

    equipment: str
    instance_id: str
    host: str
    pid: int
    contract_version: str
    contract_hash: str
    started_at: str
    seq: int
    interval_s: float
    capabilities: list[CapabilityPresence] = []


class NodeRecord(BaseModel):
    """A node as the monitor sees it."""

    equipment: str
    instance_id: str
    host: str
    pid: int
    status: NodeStatus
    contract_hash: str
    contract_matches: bool = Field(
        default=True,
        description="False when the node's contract_hash differs from the monitor's own",
    )
    started_at: str
    last_seen: str
    capabilities: list[CapabilityPresence] = []


class NodeList(BaseModel):
    nodes: list[NodeRecord] = []


class NodeQuery(BaseModel):
    equipment: str
    instance_id: str | None = None


class WaitForNode(BaseModel):
    equipment: str
    timeout_s: float = 30.0


class RegistryEvent(BaseModel):
    """Streamed whenever the registry changes."""

    event: str  # node_up | node_down | node_leaving | state_changed
    node: NodeRecord


class RegistryState(ServerStateBase):
    n_nodes: int = 0
    n_up: int = 0
    contract_hash: str = ""


# --- Supervision -----------------------------------------------------------


class SpawnRequest(BaseModel):
    """Start a node on a host.

    `node` is a key into the agent's *local* allowlist, never a command line. The
    agent resolves it to `[sys.executable, "-m", <module>, *args]`. Anything else
    would make the bus a remote-code-execution channel.
    """

    host: str
    node: str
    args: list[str] = []
    instance_id: str | None = None


class KillRequest(BaseModel):
    instance_id: str
    grace_s: float = 10.0


class ProcessInfo(BaseModel):
    instance_id: str
    node: str
    host: str
    pid: int
    started_at: str
    running: bool


class ProcessList(BaseModel):
    processes: list[ProcessInfo] = []


class HostInfo(BaseModel):
    host: str
    available_nodes: list[str] = []
    processes: list[ProcessInfo] = []


class HostList(BaseModel):
    hosts: list[HostInfo] = []


class SupervisorState(ServerStateBase):
    n_hosts: int = 0
    n_processes: int = 0


class AgentState(ServerStateBase):
    host: str = ""
    available_nodes: list[str] = []
    n_processes: int = 0
