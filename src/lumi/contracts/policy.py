"""Who may do what, expressed as RabbitMQ topic permissions.

This is the reason the op is in the routing key. RabbitMQ authorizes publishes and
bindings by matching a regex against the routing key, so with the op in the key we can
say "a browser may call `image` but not `update_camera_config`" and have the *broker*
enforce it. If the op lived only in an AMQP header -- as it did -- then any client
permitted to talk to the camera at all could also reconfigure it, and no amount of
per-user credentials would help.

An op is classified by what it does, not by where it lives:

  READ      answers a question, changes nothing        image, get_config, cache, bboxes
  MUTATE    changes the instrument or the experiment   update_camera_config, register,
                                                       start_recording, spawn, kill

Roles are derived from that classification, so a new op is governed the moment it is
declared -- there is no second list to forget to update.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .registry import REGISTRY, iter_capabilities
from .spec import Capability, EquipmentContract, Op


class Effect(StrEnum):
    READ = "read"
    MUTATE = "mutate"


#: Ops that only ever answer questions. Everything not listed is treated as a mutation:
#: the default has to be deny, or a new op quietly inherits browser access.
READ_ONLY_OPS = frozenset({
    "image",
    "get_camera_config",
    "get_all_config",
    "get_config",
    "get_configs_by_section",
    "get_sections",
    "log",
    "bboxes",
    "cache",
    "detection",
    "initial_fragments",
    "initial_fragments_size",
    "video_fragment",
    "list_execution",
    "list_nodes",
    "get_node",
    "wait_for",
    "list_hosts",
})
# (The host agent's ops -- spawn / kill / list_processes -- are deliberately absent:
# the agent is not a contract capability. It is addressed per-host on agent.<host>.cmd
# and no browser role is ever granted write access to that key.)

#: Control verbs a viewer may send. `start`/`stop` merely toggle a stream that is
#: already public; `shutdown` and `drain` take a node off the air, and `config` changes
#: it, so those are never granted to a browser.
VIEWER_CONTROL_VERBS = frozenset({"start", "stop", "state"})


def effect(op: Op) -> Effect:
    return Effect.READ if op.name in READ_ONLY_OPS else Effect.MUTATE


@dataclass(frozen=True)
class Permission:
    """One RabbitMQ topic permission entry."""

    exchange: str
    write: str  # regex over routing keys this user may publish with
    read: str  # regex over routing keys this user may bind/consume


def _alt(keys: list[str]) -> str:
    """An anchored regex matching exactly these routing keys, and nothing else."""
    if not keys:
        return "^$"  # matches nothing
    return "^(" + "|".join(re.escape(k) for k in sorted(set(keys))) + ")$"


def viewer_permissions() -> dict[str, Permission]:
    """A browser: may watch everything, may ask read-only questions, may start and stop
    streams. May not reconfigure an instrument, start a recording, or stop a node.

    The masks never even reach it: `detection.detection` (NPZ, pattern + masks) is not
    in its read set, only `detection.overlay` (boxes).
    """
    by_exchange: dict[str, tuple[list[str], list[str]]] = {}

    for contract, cap in iter_capabilities():
        keys = cap.keys(contract.name)
        write, read = by_exchange.setdefault(contract.exchange, ([], []))

        # Read: every stream, update and state channel...
        if cap.stream is not None:
            read.append(keys.publish)
        if cap.update is not None:
            read.append(keys.update)
        read.append(keys.state)

        # ...with one deliberate exception: the heavy detection stream.
        if contract.name == "detection" and cap.name == "detection":
            read.remove(keys.publish)

        # Write: read-only ops, and the harmless control verbs.
        for op in cap.ops:
            if effect(op) is Effect.READ:
                write.append(keys.request(op.name))
        for verb in VIEWER_CONTROL_VERBS:
            write.append(keys.control(verb))

    return {
        exchange: Permission(exchange=exchange, write=_alt(w), read=_alt(r))
        for exchange, (w, r) in by_exchange.items()
    }


def operator_permissions() -> dict[str, Permission]:
    """A human operator: everything a viewer can do, plus mutations -- reconfigure the
    camera, register boxes, start a recording. Still may not shut a node down; that is
    the supervisor's job and it belongs to an admin."""
    by_exchange: dict[str, tuple[list[str], list[str]]] = {}

    for contract, cap in iter_capabilities():
        keys = cap.keys(contract.name)
        write, read = by_exchange.setdefault(contract.exchange, ([], []))

        if cap.stream is not None:
            read.append(keys.publish)
        if cap.update is not None:
            read.append(keys.update)
        read.append(keys.state)

        for op in cap.ops:
            # An operator may do anything the contract offers on the equipment...
            if contract.name == "system" and cap.name == "supervisor":
                # ...except spawn and kill nodes.
                if effect(op) is Effect.MUTATE:
                    continue
            write.append(keys.request(op.name))

        for verb in ("start", "stop", "state", "config"):
            write.append(keys.control(verb))

    return {
        exchange: Permission(exchange=exchange, write=_alt(w), read=_alt(r))
        for exchange, (w, r) in by_exchange.items()
    }


def node_permissions() -> dict[str, Permission]:
    """A node process: unrestricted on its own exchange. Nodes are trusted code running
    on lab machines; they are not the threat model. The threat model is a browser."""
    return {
        contract.exchange: Permission(exchange=contract.exchange, write=".*", read=".*")
        for contract in REGISTRY.values()
    }


ROLES = {
    "viewer": viewer_permissions,
    "operator": operator_permissions,
    "node": node_permissions,
}


def permits(role: str, exchange: str, routing_key: str) -> bool:
    """Whether `role` may PUBLISH to `routing_key` on `exchange`.

    This is the single enforcement predicate. The backend bridge calls it to gate a
    logged-in user, and it matches against the *same* regex the broker would use for a
    connecting user of that role -- so app-layer enforcement and broker enforcement are
    guaranteed identical rather than two hand-kept lists that can drift.
    """
    perm = ROLES[role]().get(exchange)
    if perm is None:
        return False
    return bool(re.match(perm.write, routing_key))


def permits_read(role: str, exchange: str, routing_key: str) -> bool:
    """Whether `role` may SUBSCRIBE to `routing_key` (a stream/update/state key)."""
    perm = ROLES[role]().get(exchange)
    if perm is None:
        return False
    return bool(re.match(perm.read, routing_key))
