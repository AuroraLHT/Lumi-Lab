"""The permission model. Tier T0.

These are the tests that decide whether "a browser can talk to the broker directly" is
safe or reckless. They are regexes, so they are cheap to check exhaustively -- and a
mistake here is a node that a stranger on the network can switch off.

The rule under test: a viewer may WATCH anything and ASK anything, but may not CHANGE
anything.
"""

from __future__ import annotations

import re

import pytest

from lumi.contracts import REGISTRY, iter_capabilities
from lumi.contracts.policy import (
    READ_ONLY_OPS,
    Effect,
    effect,
    node_permissions,
    operator_permissions,
    viewer_permissions,
)


def _may_write(perms, exchange: str, key: str) -> bool:
    perm = perms.get(exchange)
    return bool(perm and re.match(perm.write, key))


def _may_read(perms, exchange: str, key: str) -> bool:
    perm = perms.get(exchange)
    return bool(perm and re.match(perm.read, key))


# --- the dangerous things a viewer must not be able to do -------------------


@pytest.mark.parametrize(
    "exchange,key,what",
    [
        ("RHEED", "rheed.camera.req.update_camera_config", "reconfigure the camera"),
        ("RHEED", "rheed.integrator.req.register", "register an integration box"),
        ("RHEED", "rheed.integrator.req.remove", "remove someone's integration box"),
        ("STORAGE", "storage.storage.req.start_recording", "start a recording"),
        ("STORAGE", "storage.storage.req.stop_recording", "stop someone's recording"),
        ("CHAMBER", "chamber.mi_mode.req.register_commands", "run a chamber script"),
        ("RHEED", "detection.detection.req.set_detection_crop", "change the detector crop"),
        ("lumi.system", "system.supervisor.req.spawn", "spawn a node"),
        ("lumi.system", "system.supervisor.req.kill", "kill a node"),
        ("lumi.system", "system.supervisor.req.restart", "restart a node"),
        ("RHEED", "rheed.camera.ctrl.shutdown", "shut the RHEED node down"),
        ("CHAMBER", "chamber.mi_mode.ctrl.shutdown", "shut the chamber down"),
        ("RHEED", "rheed.camera.ctrl.drain", "drain the RHEED node"),
        ("RHEED", "rheed.camera.ctrl.config", "reconfigure the server"),
    ],
)
def test_a_viewer_cannot(exchange, key, what):
    perms = viewer_permissions()
    assert not _may_write(perms, exchange, key), f"a viewer must not be able to {what}"


def test_a_viewer_cannot_publish_anything_that_mutates():
    """Exhaustive, rather than a list someone forgets to extend: every MUTATE op in the
    whole contract must be unreachable for a viewer."""
    perms = viewer_permissions()
    for contract, cap in iter_capabilities():
        keys = cap.keys(contract.name)
        for op in cap.ops:
            if effect(op) is Effect.MUTATE:
                key = keys.request(op.name)
                assert not _may_write(perms, contract.exchange, key), (
                    f"viewer can call {key} -- classify it in READ_ONLY_OPS only if it "
                    f"genuinely changes nothing"
                )


def test_a_viewer_never_receives_masks():
    """The heavy detection stream (pattern + per-instance masks, NPZ) must not be
    readable by a browser -- that is the whole point of the separate overlay stream."""
    perms = viewer_permissions()
    detection = REGISTRY["detection"].capability("detection")
    overlay = REGISTRY["detection"].capability("overlay")

    heavy = detection.keys("detection").publish
    light = overlay.keys("detection").publish

    assert not _may_read(perms, "RHEED", heavy), "a viewer can subscribe to the mask stream"
    assert _may_read(perms, "RHEED", light), "a viewer cannot subscribe to the overlay"


# --- the things a viewer must still be able to do ---------------------------


@pytest.mark.parametrize(
    "exchange,key",
    [
        ("RHEED", "rheed.camera.req.image"),
        ("RHEED", "rheed.camera.req.get_camera_config"),
        ("RHEED", "rheed.video.req.video_fragment"),
        ("RHEED", "rheed.integrator.req.cache"),
        ("CHAMBER", "chamber.config.req.get_all_config"),
        ("CHAMBER", "chamber.log.req.log"),
        ("lumi.system", "system.registry.req.list_nodes"),
    ],
)
def test_a_viewer_can_ask_read_only_questions(exchange, key):
    assert _may_write(viewer_permissions(), exchange, key)


@pytest.mark.parametrize(
    "exchange,key",
    [
        ("RHEED", "rheed.camera.pub"),
        ("RHEED", "rheed.video.pub"),
        ("RHEED", "rheed.integrator.pub"),
        ("RHEED", "detection.overlay.pub"),
        ("CHAMBER", "chamber.log.pub"),
        ("CHAMBER", "chamber.mi_mode.update"),
        ("STORAGE", "storage.storage.state"),
    ],
)
def test_a_viewer_can_watch(exchange, key):
    assert _may_read(viewer_permissions(), exchange, key)


def test_a_viewer_can_toggle_a_stream_but_not_the_node():
    perms = viewer_permissions()
    assert _may_write(perms, "RHEED", "rheed.camera.ctrl.start")
    assert _may_write(perms, "RHEED", "rheed.camera.ctrl.stop")
    assert not _may_write(perms, "RHEED", "rheed.camera.ctrl.shutdown")


# --- operator and node ------------------------------------------------------


def test_an_operator_may_mutate_the_instrument_but_not_kill_nodes():
    perms = operator_permissions()
    assert _may_write(perms, "RHEED", "rheed.camera.req.update_camera_config")
    assert _may_write(perms, "STORAGE", "storage.storage.req.start_recording")
    assert _may_write(perms, "CHAMBER", "chamber.mi_mode.req.register_commands")

    assert not _may_write(perms, "lumi.system", "system.supervisor.req.spawn")
    assert not _may_write(perms, "lumi.system", "system.supervisor.req.kill")


def test_nodes_are_unrestricted():
    """Nodes are trusted code on lab machines. The threat model is a browser."""
    perms = node_permissions()
    assert _may_write(perms, "RHEED", "rheed.camera.req.update_camera_config")
    assert _may_write(perms, "RHEED", "rheed.camera.ctrl.shutdown")


# --- the classification itself ----------------------------------------------


def test_unclassified_ops_default_to_mutate():
    """Deny by default. If a new op is not explicitly listed as read-only it is treated
    as a mutation -- so forgetting to classify it locks browsers out, rather than
    silently handing them the ability to call it."""
    from lumi.contracts.payloads.common import Ack, Empty
    from lumi.contracts.spec import Op

    assert effect(Op("some_brand_new_op", Empty, Ack)) is Effect.MUTATE


def test_read_only_list_only_names_ops_that_exist():
    """Stops the list rotting into a set of names that no longer mean anything."""
    real = {op.name for _, cap in iter_capabilities() for op in cap.ops}
    stale = READ_ONLY_OPS - real
    assert not stale, f"READ_ONLY_OPS names ops that no longer exist: {sorted(stale)}"
