"""The monitor's view of the world.

Pure logic, no AMQP -- which means the expiry rules (the part that decides a node is
dead) are unit-testable without a broker.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from lumi.contracts import contract_hash
from lumi.contracts.payloads.system import (
    Heartbeat,
    NodeRecord,
    NodeStatus,
    RegistryEvent,
)

log = logging.getLogger(__name__)

# How long a node may go unheard before we call it dead. Generous relative to the
# heartbeat interval: one dropped packet is not a crash.
MISSED_BEATS = 3
MIN_TTL = timedelta(seconds=5)
# Keep dead nodes visible for a while, so the UI can show "died 20s ago" rather than
# having the row silently vanish.
GRAVE_PERIOD = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(UTC)


class NodeRegistry:
    """Who is on the bus. Liveness is heartbeat expiry, not an RPC timeout."""

    def __init__(self, *, own_contract_hash: str | None = None) -> None:
        self._nodes: dict[tuple[str, str], NodeRecord] = {}
        self._last_seen: dict[tuple[str, str], datetime] = {}
        self._ttl: dict[tuple[str, str], timedelta] = {}
        self.own_hash = own_contract_hash or contract_hash()

    # --- ingest -----------------------------------------------------------

    def on_heartbeat(self, hb: Heartbeat) -> RegistryEvent | None:
        key = (hb.equipment, hb.instance_id)
        now = _now()
        previous = self._nodes.get(key)

        record = NodeRecord(
            equipment=hb.equipment,
            instance_id=hb.instance_id,
            host=hb.host,
            pid=hb.pid,
            status=NodeStatus.UP,
            contract_hash=hb.contract_hash,
            # The Windows chamber PC, the CUDA box and the camera host are updated at
            # different times. Without this they just misbehave quietly.
            contract_matches=hb.contract_hash == self.own_hash,
            started_at=hb.started_at,
            last_seen=now.isoformat(),
            capabilities=hb.capabilities,
        )
        self._nodes[key] = record
        self._last_seen[key] = now
        self._ttl[key] = max(timedelta(seconds=hb.interval_s * MISSED_BEATS), MIN_TTL)

        if previous is None or previous.status is not NodeStatus.UP:
            if not record.contract_matches:
                log.warning(
                    "%s/%s is running contract %s but we expect %s",
                    hb.equipment, hb.instance_id, hb.contract_hash, self.own_hash,
                )
            return RegistryEvent(event="node_up", node=record)

        if previous.capabilities != record.capabilities:
            return RegistryEvent(event="state_changed", node=record)
        return None

    def on_leaving(self, hb: Heartbeat) -> RegistryEvent | None:
        """A clean shutdown. Distinguishing this from a crash is the whole reason
        drain() announces itself before exiting."""
        key = (hb.equipment, hb.instance_id)
        record = self._nodes.get(key)
        if record is None:
            return None
        record = record.model_copy(update={"status": NodeStatus.LEAVING, "last_seen": _now().isoformat()})
        self._nodes[key] = record
        return RegistryEvent(event="node_leaving", node=record)

    # --- expiry -----------------------------------------------------------

    def reap(self) -> list[RegistryEvent]:
        """Mark nodes we have not heard from as down; forget them once cold."""
        now = _now()
        events: list[RegistryEvent] = []

        for key, record in list(self._nodes.items()):
            last = self._last_seen.get(key, now)
            ttl = self._ttl.get(key, MIN_TTL)

            if record.status is NodeStatus.UP and now - last > ttl:
                dead = record.model_copy(update={"status": NodeStatus.DOWN})
                self._nodes[key] = dead
                log.warning("%s/%s went silent (no heartbeat for %s)", key[0], key[1], now - last)
                events.append(RegistryEvent(event="node_down", node=dead))

            elif record.status in (NodeStatus.DOWN, NodeStatus.LEAVING) and now - last > GRAVE_PERIOD:
                del self._nodes[key]
                self._last_seen.pop(key, None)
                self._ttl.pop(key, None)

        return events

    # --- query ------------------------------------------------------------

    def list_nodes(self) -> list[NodeRecord]:
        return sorted(self._nodes.values(), key=lambda n: (n.equipment, n.instance_id))

    def get(self, equipment: str, instance_id: str | None = None) -> NodeRecord | None:
        if instance_id is not None:
            return self._nodes.get((equipment, instance_id))
        live = [
            n for (eq, _), n in self._nodes.items()
            if eq == equipment and n.status is NodeStatus.UP
        ]
        return live[0] if live else None

    def is_up(self, equipment: str) -> bool:
        return self.get(equipment) is not None

    @property
    def n_up(self) -> int:
        return sum(1 for n in self._nodes.values() if n.status is NodeStatus.UP)
