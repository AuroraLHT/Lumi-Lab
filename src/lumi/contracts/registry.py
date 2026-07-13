"""The registry: every contract, plus the hash that detects deployment skew.

`contract_hash()` is a stable digest of the *whole* wire surface -- every op name,
every codec, every payload schema, every routing key. Nodes put it in their
heartbeat and the monitor compares it against its own. That is what turns "the
Windows chamber PC is three commits behind" from a mystifying runtime failure into
a flag in the node list.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from typing import Any

from .chamber import CHAMBER
from .detection import DETECTION_NODE
from .rheed import RHEED
from .spec import Capability, EquipmentContract, Op, StreamSpec
from .storage import STORAGE_NODE
from .system import SYSTEM

REGISTRY: dict[str, EquipmentContract] = {
    c.name: c
    for c in (RHEED, CHAMBER, DETECTION_NODE, STORAGE_NODE, SYSTEM)
}

# Contracts a node process can host. `system` is hosted by the monitor and the
# host agent rather than by an equipment node.
EQUIPMENT: dict[str, EquipmentContract] = {
    name: c for name, c in REGISTRY.items() if name != "system"
}


def contract(name: str) -> EquipmentContract:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown contract {name!r}; known: {sorted(REGISTRY)}") from None


def _schema(model: type) -> dict[str, Any]:
    return model.model_json_schema(ref_template="#/$defs/{model}")


def _op_json(op: Op) -> dict[str, Any]:
    return {
        "name": op.name,
        "request": _schema(op.request),
        "response": _schema(op.response),
        "request_codec": str(op.request_codec),
        "response_codec": str(op.response_codec),
    }


def _stream_json(s: StreamSpec | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {"name": s.name, "payload": _schema(s.payload), "codec": str(s.codec)}


def _capability_json(cap: Capability, equipment: str) -> dict[str, Any]:
    keys = cap.keys(equipment)
    return {
        "name": cap.name,
        "kind": str(cap.kind),
        "state": _schema(cap.state),
        "ops": [_op_json(op) for op in cap.ops],
        "stream": _stream_json(cap.stream),
        "update": _stream_json(cap.update),
        "routing_keys": {
            # Per-op keys, spelled out: these are exactly what a broker permission
            # regex has to match, so they belong in the contract artifact.
            "requests": {op.name: keys.request(op.name) for op in cap.ops},
            "request_pattern": keys.request_pattern,
            "control_pattern": keys.control_pattern,
            "state": keys.state,
            "publish": keys.publish,
            "update": keys.update,
            "work_queue": keys.work_queue,
        },
    }


def canonical_dict() -> dict[str, Any]:
    """The whole wire surface, as plain data. Order is fixed so the hash is stable."""
    return {
        name: {
            "name": c.name,
            "exchange": c.exchange,
            "exchange_type": c.exchange_type,
            "version": c.version,
            "capabilities": [_capability_json(cap, c.name) for cap in c.capabilities],
        }
        for name, c in sorted(REGISTRY.items())
    }


def canonical_json() -> str:
    return json.dumps(canonical_dict(), sort_keys=True, separators=(",", ":"))


@cache
def contract_hash() -> str:
    return hashlib.sha256(canonical_json().encode()).hexdigest()[:16]


def iter_capabilities():
    """Every (contract, capability) pair. The generators and the conformance test
    both walk this, which is why adding a capability needs no change to either."""
    for c in REGISTRY.values():
        for cap in c.capabilities:
            yield c, cap
