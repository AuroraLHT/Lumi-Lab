"""Registry and expiry logic. Tier T0: no broker.

The reason this is pure logic with no AMQP in it: the rule that decides a node is
dead is the most important rule in the monitor, and it should be testable without
spinning up a broker and waiting in real time for something to die.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lumi.contracts import contract_hash
from lumi.contracts.payloads.system import CapabilityPresence, Heartbeat, NodeStatus
from lumi.system.registry import NodeRegistry


def beat(equipment="rheed", instance="rheed-1", seq=0, interval=2.0, chash=None, caps=None) -> Heartbeat:
    return Heartbeat(
        equipment=equipment,
        instance_id=instance,
        host="camera-host",
        pid=123,
        contract_version="2.0",
        contract_hash=chash or contract_hash(),
        started_at=datetime.now(UTC).isoformat(),
        seq=seq,
        interval_s=interval,
        capabilities=caps or [],
    )


def _age(reg: NodeRegistry, key, seconds: float) -> None:
    """Pretend the last heartbeat arrived `seconds` ago."""
    reg._last_seen[key] = datetime.now(UTC) - timedelta(seconds=seconds)


def test_first_heartbeat_brings_a_node_up():
    reg = NodeRegistry()
    event = reg.on_heartbeat(beat())
    assert event is not None and event.event == "node_up"
    assert reg.is_up("rheed")
    assert reg.n_up == 1


def test_repeated_heartbeats_are_not_events():
    """Otherwise every node emits an event twice a second, forever."""
    reg = NodeRegistry()
    reg.on_heartbeat(beat(seq=0))
    assert reg.on_heartbeat(beat(seq=1)) is None
    assert reg.on_heartbeat(beat(seq=2)) is None


def test_capability_state_change_is_an_event():
    reg = NodeRegistry()
    reg.on_heartbeat(beat())
    caps = [CapabilityPresence(name="camera", kind="duplex", is_running=True, is_streaming=True)]
    event = reg.on_heartbeat(beat(caps=caps))
    assert event is not None and event.event == "state_changed"


def test_silence_marks_a_node_down_after_three_missed_beats():
    reg = NodeRegistry()
    reg.on_heartbeat(beat(interval=2.0))
    key = ("rheed", "rheed-1")

    _age(reg, key, 3.0)         # under the 6s TTL
    assert reg.reap() == []
    assert reg.is_up("rheed")

    _age(reg, key, 7.0)         # past it
    events = reg.reap()
    assert [e.event for e in events] == ["node_down"]
    assert not reg.is_up("rheed")


def test_a_node_is_not_declared_dead_twice():
    reg = NodeRegistry()
    reg.on_heartbeat(beat())
    _age(reg, ("rheed", "rheed-1"), 60)
    assert len(reg.reap()) == 1
    assert reg.reap() == []          # already down; no second funeral


def test_a_fast_heartbeat_still_gets_a_minimum_ttl():
    """A 0.5s interval would otherwise mean a 1.5s TTL -- one scheduling hiccup and
    a perfectly healthy node is declared dead."""
    reg = NodeRegistry()
    reg.on_heartbeat(beat(interval=0.5))
    _age(reg, ("rheed", "rheed-1"), 3.0)
    assert reg.reap() == []          # MIN_TTL is 5s
    assert reg.is_up("rheed")


def test_clean_shutdown_is_distinguishable_from_a_crash():
    reg = NodeRegistry()
    reg.on_heartbeat(beat())
    event = reg.on_leaving(beat())
    assert event is not None and event.event == "node_leaving"
    assert reg.get("rheed", "rheed-1").status is NodeStatus.LEAVING
    assert not reg.is_up("rheed")


def test_dead_nodes_are_forgotten_only_after_the_grave_period():
    """They stay visible for a minute so the UI can say 'died 20s ago' instead of the
    row silently vanishing."""
    reg = NodeRegistry()
    reg.on_heartbeat(beat())
    key = ("rheed", "rheed-1")

    _age(reg, key, 30)
    reg.reap()
    assert len(reg.list_nodes()) == 1        # down, but still listed

    _age(reg, key, 120)
    reg.reap()
    assert reg.list_nodes() == []            # now forgotten


def test_a_node_can_come_back():
    reg = NodeRegistry()
    reg.on_heartbeat(beat())
    _age(reg, ("rheed", "rheed-1"), 60)
    reg.reap()
    assert not reg.is_up("rheed")

    event = reg.on_heartbeat(beat(seq=99))
    assert event is not None and event.event == "node_up"
    assert reg.is_up("rheed")


def test_contract_skew_is_flagged():
    """Pascal is a Windows box, detection is a CUDA box, RHEED is the camera host, and
    they get updated at different times. This is what makes that visible instead of
    merely baffling."""
    reg = NodeRegistry()
    reg.on_heartbeat(beat(chash="deadbeefdeadbeef"))
    record = reg.get("rheed", "rheed-1")
    assert record.contract_matches is False

    reg2 = NodeRegistry()
    reg2.on_heartbeat(beat())
    assert reg2.get("rheed", "rheed-1").contract_matches is True


def test_multiple_instances_of_one_equipment():
    reg = NodeRegistry()
    reg.on_heartbeat(beat(instance="rheed-1"))
    reg.on_heartbeat(beat(instance="rheed-2"))
    assert reg.n_up == 2
    assert len(reg.list_nodes()) == 2
    assert reg.get("rheed") is not None          # any live one
    assert reg.get("rheed", "rheed-2") is not None
