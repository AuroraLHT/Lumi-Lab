"""The system contract: presence, discovery, supervision.

Two processes speak it. The **monitor** watches presence heartbeats and serves the
registry; it also acts as supervisor, forwarding spawn/kill to the right host. The
**host agent** is a small process (the only systemd unit) that owns process control
on one machine -- the monitor cannot spawn a process on a remote host by itself,
something has to already be listening there.

Uses a topic exchange, unlike the equipment contracts: presence keys are
`presence.<equipment>.<instance_id>` and agents bind `agent.<host>.cmd`, both of
which need pattern matching.
"""

from __future__ import annotations

from .payloads.common import Ack, Empty
from .payloads.system import (
    HostList,
    KillRequest,
    NodeList,
    NodeQuery,
    NodeRecord,
    RegistryEvent,
    RegistryState,
    SpawnRequest,
    SupervisorState,
    WaitForNode,
)
from .spec import Capability, EquipmentContract, Kind, Op, StreamSpec

REGISTRY = Capability(
    name="registry",
    kind=Kind.DUPLEX,
    doc="Who is on the bus, right now. Liveness comes from heartbeat expiry, not "
        "from an RPC timing out.",
    state=RegistryState,
    ops=(
        Op("list_nodes", Empty, NodeList),
        Op("get_node", NodeQuery, NodeRecord),
        Op("wait_for", WaitForNode, NodeRecord,
           doc="Block until an instance of `equipment` is up. Lets a dependent node "
               "(storage) wait for its sources instead of assuming they are there."),
    ),
    stream=StreamSpec("registry_event", RegistryEvent),
)

SUPERVISOR = Capability(
    name="supervisor",
    kind=Kind.RPC,
    doc="Start and stop nodes, by forwarding to the agent on the target host.",
    state=SupervisorState,
    ops=(
        Op("list_hosts", Empty, HostList),
        Op("spawn", SpawnRequest, Ack),
        Op("kill", KillRequest, Ack),
        Op("restart", KillRequest, Ack),
    ),
)

SYSTEM = EquipmentContract(
    name="system",
    exchange="lumi.system",
    exchange_type="topic",
    version="2.0",
    capabilities=(REGISTRY, SUPERVISOR),
)

# The host agent is deliberately NOT a capability of SYSTEM.
#
# Every other capability is addressed by a routing key derived from the contract
# (`system.registry.req`), which assumes exactly one logical server. The agent is
# per-host: there is one on every machine, each addressed at `agent.<host>.cmd`, and
# a request for the camera host must never be answered by the CUDA box. That does not
# fit a derived key, and pretending it does would mean mounting a capability on the
# monitor that the monitor cannot serve.
#
# So the agent speaks the same payload models (SpawnRequest, KillRequest, ...) over
# its own host-scoped routing key. See lumi.system.agent.HostAgent.
AGENT_KEY = "agent.{host}.cmd"
AGENT_EVENT_KEY = "agent.{host}.evt"
