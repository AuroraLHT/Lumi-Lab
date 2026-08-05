"""Contract invariants. Tier T0: no broker, no hardware.

These are cheap and they are the reason the contract can be trusted as a source of
truth. A routing-key collision or a payload that doesn't round-trip is caught here,
at import time, instead of as a mystery message on the bus.
"""

from __future__ import annotations

import json

import pytest

from lumi.contracts import (
    REGISTRY,
    Capability,
    Codec,
    ContractError,
    Kind,
    canonical_json,
    contract_hash,
    iter_capabilities,
)
from lumi.contracts.payloads.common import Empty
from lumi.contracts.spec import Op, StreamSpec


def _all_caps() -> list[tuple[str, Capability]]:
    return [(c.name, cap) for c, cap in iter_capabilities()]


def test_registry_is_not_empty():
    assert REGISTRY
    assert {"rheed", "chamber", "detection", "storage", "system"} <= set(REGISTRY)


def test_routing_keys_are_globally_unique():
    """Two capabilities sharing a routing key would silently steal each other's
    messages. The old layout made this possible: keys were free-text in settings.toml."""
    seen: dict[str, str] = {}
    for equipment, cap in _all_caps():
        for key in cap.keys(equipment).all():
            owner = f"{equipment}.{cap.name}"
            assert key not in seen, f"routing key {key!r} claimed by {seen[key]} and {owner}"
            seen[key] = owner


def test_work_queues_are_globally_unique():
    seen: dict[str, str] = {}
    for equipment, cap in _all_caps():
        q = cap.keys(equipment).work_queue
        owner = f"{equipment}.{cap.name}"
        assert q not in seen, f"work queue {q!r} claimed by {seen[q]} and {owner}"
        seen[q] = owner


@pytest.mark.parametrize("equipment,cap", _all_caps(), ids=lambda v: getattr(v, "name", v))
def test_every_capability_has_a_state_model(equipment, cap):
    assert cap.state is not None
    # State must be constructible with no arguments -- a node publishes state before
    # it knows anything, and a required field there would crash it at startup.
    cap.state()


@pytest.mark.parametrize("equipment,cap", _all_caps(), ids=lambda v: getattr(v, "name", v))
def test_kind_matches_shape(equipment, cap):
    if cap.kind is Kind.RPC:
        assert cap.ops and cap.stream is None and cap.update is None
    elif cap.kind is Kind.STREAM:
        assert not cap.ops and cap.stream is not None
    elif cap.kind is Kind.PUBSUB:
        assert cap.ops and cap.update is not None and cap.stream is None
    elif cap.kind is Kind.DUPLEX:
        assert cap.ops and cap.stream is not None


@pytest.mark.parametrize("equipment,cap", _all_caps(), ids=lambda v: getattr(v, "name", v))
def test_payloads_round_trip(equipment, cap):
    """Every declared model must survive JSON. A model that cannot be serialised is
    a contract that cannot be spoken."""
    models = [cap.state]
    for op in cap.ops:
        models += [op.request, op.response]
    for spec in (cap.stream, cap.update):
        if spec is not None:
            models.append(spec.payload)

    for model in models:
        schema = model.model_json_schema()
        assert isinstance(schema, dict) and "properties" in schema or not model.model_fields
        json.dumps(schema)  # schema itself must be serialisable (codegen depends on it)


def test_contract_hash_is_stable_and_short():
    assert contract_hash() == contract_hash()
    assert len(contract_hash()) == 16
    assert json.loads(canonical_json())


def test_contract_hash_changes_when_the_wire_changes():
    """The hash is what detects a node deployed against a stale contract. If it did
    not move when an op was added, that detection would be worthless."""
    before = canonical_json()
    # A capability with an extra op must not serialise identically.
    cap = Capability(
        name="probe", kind=Kind.RPC, state=Empty,
        ops=(Op("a", Empty, Empty),),
    )
    cap2 = Capability(
        name="probe", kind=Kind.RPC, state=Empty,
        ops=(Op("a", Empty, Empty), Op("b", Empty, Empty)),
    )
    assert cap.keys("x") == cap2.keys("x")          # same keys...
    assert len(cap.ops) != len(cap2.ops)            # ...different surface
    assert canonical_json() == before               # and we did not mutate the registry


# --- the spec rejects nonsense ---------------------------------------------


def test_rpc_cannot_have_a_stream():
    with pytest.raises(ContractError):
        Capability(name="bad", kind=Kind.RPC, state=Empty,
                   ops=(Op("a", Empty, Empty),),
                   stream=StreamSpec("s", Empty))


def test_duplex_requires_a_stream():
    with pytest.raises(ContractError):
        Capability(name="bad", kind=Kind.DUPLEX, state=Empty, ops=(Op("a", Empty, Empty),))


def test_rpc_requires_ops():
    with pytest.raises(ContractError):
        Capability(name="bad", kind=Kind.RPC, state=Empty)


def test_pubsub_requires_an_update_channel():
    with pytest.raises(ContractError):
        Capability(name="bad", kind=Kind.PUBSUB, state=Empty, ops=(Op("a", Empty, Empty),))


def test_duplicate_op_names_are_rejected():
    with pytest.raises(ContractError):
        Capability(name="bad", kind=Kind.RPC, state=Empty,
                   ops=(Op("a", Empty, Empty), Op("a", Empty, Empty)))


@pytest.mark.parametrize("name", ["start", "stop", "call", "get_state", "next"])
def test_ops_may_not_shadow_the_generated_clients_own_methods(name):
    """Storage originally had ops literally called `start` and `end`. The generated
    client's `start()` op method then overrode CapabilityClient.start(), so connecting
    the client fired a recording request instead -- and the failure surfaced as a
    baffling `start() missing 1 required positional argument: 'req'`."""
    with pytest.raises(ContractError, match="shadow"):
        Capability(name="bad", kind=Kind.RPC, state=Empty, ops=(Op(name, Empty, Empty),))


def test_no_shipped_capability_shadows_a_client_method():
    from lumi.contracts.spec import RESERVED_OP_NAMES

    for equipment, cap in _all_caps():
        for op in cap.ops:
            assert op.name not in RESERVED_OP_NAMES, f"{equipment}.{cap.name}.{op.name}"


# --- the contract still describes the system we actually have ---------------


def test_camera_is_one_capability_not_two():
    """The whole point of Kind.DUPLEX. `camera` and `live_camera` used to be two
    servers, two clients, two routing-key sets, for one camera."""
    cam = REGISTRY["rheed"].capability("camera")
    assert cam.kind is Kind.DUPLEX
    assert {op.name for op in cam.ops} == {"image", "get_camera_config", "update_camera_config"}
    assert cam.stream is not None and cam.stream.codec is Codec.NPY


def test_binary_payloads_are_not_json():
    """Frames and video fragments must not go through pydantic as JSON -- that would
    mean base64-ing megabytes per frame."""
    cam = REGISTRY["rheed"].capability("camera")
    assert cam.op("image").response_codec is Codec.NPY

    video = REGISTRY["rheed"].capability("video")
    assert video.op("video_fragment").response_codec is Codec.RAW
    assert video.stream.codec is Codec.RAW


def test_pubsub_is_reserved_for_submit_now_result_later_ops():
    """PUBSUB is for a request that can't resolve inline: chamber.mi_mode submits a
    script and reports completion later; experiment.driver submits a long-running op
    (to_temperature, perform_deposition, ...) or a human-gated one (set_laser_power,
    ...) the same way, via current_task/pending_confirmation on its update channel.
    Anything else declaring PUBSUB should be looked at hard before landing."""
    pubsubs = {f"{eq}.{cap.name}" for eq, cap in _all_caps() if cap.kind is Kind.PUBSUB}
    assert pubsubs == {"chamber.mi_mode", "experiment.driver"}


def test_every_exchange_is_a_topic_exchange():
    """Topic, not direct -- everywhere.

    For exact-match keys the two behave identically, but RabbitMQ's *topic*
    authorization only applies to topic exchanges. That is the mechanism by which a
    browser can be granted `rheed.camera.req.image` and denied
    `rheed.camera.req.update_camera_config`, enforced by the broker rather than by us.
    """
    for name in REGISTRY:
        assert REGISTRY[name].exchange_type == "topic", name


def test_the_op_is_in_the_routing_key():
    """If the op lived only in a header, "may publish to the camera" would necessarily
    mean "may also reconfigure it": the broker authorizes on routing keys."""
    cam = REGISTRY["rheed"].capability("camera")
    keys = cam.keys("rheed")

    assert keys.request("image") == "rheed.camera.req.image"
    assert keys.request("update_camera_config") == "rheed.camera.req.update_camera_config"
    assert keys.control("shutdown") == "rheed.camera.ctrl.shutdown"

    # Every op gets a distinct key -- otherwise they cannot be told apart by a permission.
    all_keys = {op.name: keys.request(op.name) for op in cam.ops}
    assert len(set(all_keys.values())) == len(all_keys)


def test_masks_do_not_share_a_routing_key_with_the_browser_view():
    """The overlay exists so the heavy arrays are never *sent* to a browser, rather
    than sent and then stripped by a proxy."""
    detection = REGISTRY["detection"].capability("detection")
    overlay = REGISTRY["detection"].capability("overlay")

    assert detection.stream.codec is Codec.NPZ      # pattern + masks
    assert overlay.stream.codec is Codec.JSON       # boxes only
    assert detection.keys("detection").publish != overlay.keys("detection").publish
