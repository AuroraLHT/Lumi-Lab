"""The vocabulary a contract is written in.

A `Capability` is one addressable thing on the bus -- a camera, a chamber log, a
detection model. It declares its operations (request/response), its stream
(server push), its updates (broadcast), and its state, once. Servers, clients,
the websocket bridge, and the TypeScript frontend are all derived from that one
declaration; none of them re-spell an operation name or a payload shape.

Routing keys are *derived* from the contract, never configured. Before this,
every routing key was written by hand in cfg/settings.toml and then re-spelled at
each construction site, which is how the client and server could drift apart
without anything failing. Deployment config (where the broker lives, how the
camera is tuned) stays in settings.toml; what the wire looks like lives here.
Never both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel


class Kind(StrEnum):
    """How a capability is talked to.

    DUPLEX is the common case for lab equipment and the reason this refactor
    exists: a camera is *one* thing that answers questions ("give me a frame")
    and also pushes frames continuously. The old code modelled that as two
    separate servers on two separate routing-key sets (`camera` and
    `live_camera`), which is why there were twice as many classes as concepts.
    """

    RPC = "rpc"  # ops only
    STREAM = "stream"  # server push only
    PUBSUB = "pubsub"  # ops + broadcast updates to all subscribers
    DUPLEX = "duplex"  # ops + server-push stream


class Codec(StrEnum):
    """How a body is put on the wire.

    Not everything is JSON. Frames and video fragments are raw buffers, and
    forcing them through a pydantic model would mean base64-ing megabytes per
    frame. So for NPY and RAW the model describes the *metadata* (carried in AMQP
    headers) and the body stays an opaque buffer; the generated client hands back
    `(meta, payload)`.
    """

    JSON = "json"  # body is the model as JSON
    NPY = "npy"  # body is np.save bytes (ONE array); model describes the headers
    NPZ = "npz"  # body is np.savez bytes (NAMED arrays); model describes the headers
    RAW = "raw"  # body is opaque bytes; model describes the headers


@dataclass(frozen=True, slots=True)
class RoutingKeys:
    """Every key a capability uses. Derived, never hand-written.

    The op name is *in the routing key* (`rheed.camera.req.image`), not only in a
    header. This is what makes broker-enforced authorization possible at all: RabbitMQ
    authorizes on routing keys, so if every request went to `rheed.camera.req` with the
    op hidden in a header, then "may publish to the camera" would necessarily mean "may
    also reconfigure it". With the op in the key, a browser can be granted
    `*.*.req.image` and denied `*.*.req.update_camera_config` -- enforced by the broker,
    not by our code.

    Servers bind to the wildcard patterns, so this costs them nothing.
    """

    request_prefix: str  # rheed.camera.req
    control_prefix: str  # rheed.camera.ctrl
    state: str
    publish: str
    update: str
    work_queue: str  # the named, durable server-side request queue

    @property
    def request_pattern(self) -> str:
        """What the server's work queue binds to: every op of this capability."""
        return f"{self.request_prefix}.*"

    @property
    def control_pattern(self) -> str:
        return f"{self.control_prefix}.*"

    def request(self, op: str) -> str:
        return f"{self.request_prefix}.{op}"

    def control(self, verb: str) -> str:
        return f"{self.control_prefix}.{verb}"

    def all(self) -> tuple[str, ...]:
        return (
            self.request_prefix,
            self.control_prefix,
            self.state,
            self.publish,
            self.update,
        )


@dataclass(frozen=True, slots=True)
class Op:
    """One request/response operation."""

    name: str
    request: type[BaseModel]
    response: type[BaseModel]
    request_codec: Codec = Codec.JSON
    response_codec: Codec = Codec.JSON
    doc: str = ""


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """A server-push channel: a stream (continuous) or an update (broadcast)."""

    name: str
    payload: type[BaseModel]
    codec: Codec = Codec.JSON
    doc: str = ""


class ContractError(Exception):
    """A contract is malformed, or a handler does not satisfy one."""


#: Op names that would shadow a generated client's own API. `start` is the trap: a
#: capability with a `start` op would generate a method that overrides
#: CapabilityClient.start(), so connecting the client would instead send a request --
#: which is exactly what happened to storage's original `start`/`end` ops.
RESERVED_OP_NAMES = frozenset({
    "call",
    "get_state",
    "next",
    "next_update",
    "shutdown_server",
    "start",
    "start_streaming",
    "state",
    "stop",
    "stop_streaming",
    "subscribe_state",
    "subscribe_stream",
    "subscribe_updates",
    "unsubscribe_stream",
})


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    kind: Kind
    state: type[BaseModel]
    ops: tuple[Op, ...] = ()
    stream: StreamSpec | None = None
    update: StreamSpec | None = None
    doc: str = ""

    def __post_init__(self) -> None:
        # Reject a contract that cannot mean anything, at import time, rather
        # than letting it fail on the fifth message in the lab.
        if self.kind in (Kind.RPC, Kind.PUBSUB, Kind.DUPLEX) and not self.ops:
            raise ContractError(f"{self.name}: {self.kind} requires at least one op")
        if self.kind is Kind.STREAM and self.ops:
            raise ContractError(f"{self.name}: a stream capability cannot have ops")
        if self.kind in (Kind.STREAM, Kind.DUPLEX) and self.stream is None:
            raise ContractError(f"{self.name}: {self.kind} requires a stream")
        if self.kind in (Kind.RPC, Kind.PUBSUB) and self.stream is not None:
            raise ContractError(f"{self.name}: {self.kind} cannot have a stream")
        if self.kind is Kind.PUBSUB and self.update is None:
            raise ContractError(f"{self.name}: pubsub requires an update channel")
        if self.kind is not Kind.PUBSUB and self.update is not None:
            raise ContractError(f"{self.name}: only pubsub may have an update channel")

        seen: set[str] = set()
        for op in self.ops:
            if op.name in seen:
                raise ContractError(f"{self.name}: duplicate op {op.name!r}")
            if op.name in RESERVED_OP_NAMES:
                raise ContractError(
                    f"{self.name}: op {op.name!r} would shadow the generated client's own "
                    f"{op.name}() method. Pick a more specific name "
                    f"(e.g. start_recording rather than start)."
                )
            seen.add(op.name)

    def op(self, name: str) -> Op:
        for op in self.ops:
            if op.name == name:
                return op
        raise KeyError(f"{self.name} has no op {name!r}")

    def keys(self, equipment: str) -> RoutingKeys:
        base = f"{equipment}.{self.name}"
        return RoutingKeys(
            request_prefix=f"{base}.req",
            control_prefix=f"{base}.ctrl",
            state=f"{base}.state",
            publish=f"{base}.pub",
            update=f"{base}.update",
            work_queue=f"q.{base}.req",
        )


@dataclass(frozen=True, slots=True)
class EquipmentContract:
    """One node's worth of capabilities."""

    name: str
    exchange: str
    version: str
    capabilities: tuple[Capability, ...]
    # Topic, not direct. For exact-match keys a topic exchange behaves identically to a
    # direct one -- but only topic exchanges support RabbitMQ's topic authorization, and
    # that is the mechanism that lets a browser watch the camera without being able to
    # switch the chamber off.
    exchange_type: str = "topic"
    doc: str = ""

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for cap in self.capabilities:
            if cap.name in seen:
                raise ContractError(f"{self.name}: duplicate capability {cap.name!r}")
            seen.add(cap.name)

    def capability(self, name: str) -> Capability:
        for cap in self.capabilities:
            if cap.name == name:
                return cap
        raise KeyError(f"{self.name} has no capability {name!r}")
