"""Presence over a real broker. Tier T1.

The lifecycle that matters operationally: a node appears on its own, a crashed node
is noticed without anyone asking, and a deliberate stop looks different from a crash.
"""

from __future__ import annotations

import asyncio

import pytest
from aio_pika import connect_robust

from lumi.contracts import Capability, EquipmentContract, Kind
from lumi.contracts.payloads.common import Ack, Empty, ServerStateBase
from lumi.contracts.payloads.system import NodeStatus
from lumi.contracts.spec import Op
from lumi.node import EquipmentNode
from lumi.system.monitor import RegistryHandler

pytestmark = pytest.mark.broker


class ToyState(ServerStateBase):
    pings: int = 0


TOY_CAP = Capability(name="toy", kind=Kind.RPC, state=ToyState, ops=(Op("ping", Empty, Ack),))
TOY = EquipmentContract(name="toynode", exchange="LUMI_TEST_SYS", version="test",
                        capabilities=(TOY_CAP,))


class ToyHandler:
    def __init__(self) -> None:
        self.pings = 0

    async def ping(self, req: Empty) -> Ack:
        self.pings += 1
        return Ack()

    def state(self) -> ToyState:
        return ToyState(is_running=True, pings=self.pings)


@pytest.fixture
async def monitor(rabbitmq_url):
    conn = await connect_robust(rabbitmq_url)
    channel = await conn.channel()
    registry = RegistryHandler()
    await registry.listen(channel)
    yield registry
    await registry.stop()
    await conn.close()


async def _toy_node(rabbitmq_url, interval=0.5) -> EquipmentNode:
    node = EquipmentNode(TOY, amqp_url=rabbitmq_url, heartbeat_interval=interval)
    node.mount("toy", ToyHandler())
    return node


async def _wait_until(predicate, timeout=10.0, interval=0.1):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_a_node_announces_itself_without_being_asked(monitor, rabbitmq_url):
    """No hardcoded list, no RPC probe. The node says it is here."""
    node = await _toy_node(rabbitmq_url)
    await node.start()
    try:
        assert await _wait_until(lambda: monitor.registry.is_up("toynode")), "node never appeared"
        record = monitor.registry.get("toynode")
        assert record.status is NodeStatus.UP
        assert record.instance_id == node.instance_id
        assert [c.name for c in record.capabilities] == ["toy"]
        assert record.contract_matches is True
    finally:
        await node.drain()


async def test_a_clean_stop_is_reported_as_leaving_not_as_a_crash(monitor, rabbitmq_url):
    node = await _toy_node(rabbitmq_url)
    await node.start()
    assert await _wait_until(lambda: monitor.registry.is_up("toynode"))

    await node.drain()

    # drain() announces departure *before* the process would exit, so this is
    # immediate -- we do not have to sit through the heartbeat TTL.
    assert await _wait_until(
        lambda: monitor.registry.get("toynode", node.instance_id).status is NodeStatus.LEAVING,
        timeout=5,
    ), "clean shutdown was not announced"


async def test_a_silent_node_is_reaped(monitor, rabbitmq_url):
    """Simulates a crash: stop heartbeating without announcing anything."""
    node = await _toy_node(rabbitmq_url, interval=0.5)
    await node.start()
    assert await _wait_until(lambda: monitor.registry.is_up("toynode"))

    # Kill the heartbeat only -- the node is now a zombie as far as the bus knows.
    await node._heartbeat.stop()

    assert await _wait_until(
        lambda: monitor.registry.get("toynode", node.instance_id).status is NodeStatus.DOWN,
        timeout=15,
    ), "a silent node was never marked down"

    await node.drain()


async def test_capability_state_reaches_the_monitor(monitor, rabbitmq_url):
    node = await _toy_node(rabbitmq_url)
    await node.start()
    try:
        assert await _wait_until(lambda: monitor.registry.is_up("toynode"))
        handler = node._handlers["toy"]
        handler.pings = 7

        def state_arrived() -> bool:
            record = monitor.registry.get("toynode", node.instance_id)
            return bool(record and record.capabilities
                        and record.capabilities[0].state.get("pings") == 7)

        assert await _wait_until(state_arrived, timeout=10), "capability state never propagated"
    finally:
        await node.drain()


async def test_wait_for_unblocks_when_the_node_appears(monitor, rabbitmq_url):
    """What lets storage wait for its sources instead of assuming they are up."""
    from lumi.contracts.payloads.system import WaitForNode

    waiter = asyncio.create_task(monitor.wait_for(WaitForNode(equipment="toynode", timeout_s=15)))
    await asyncio.sleep(0.5)
    assert not waiter.done()

    node = await _toy_node(rabbitmq_url)
    await node.start()
    try:
        record = await asyncio.wait_for(waiter, 15)
        assert record.equipment == "toynode"
    finally:
        await node.drain()


async def test_wait_for_times_out_if_nothing_appears(monitor):
    from lumi.contracts.payloads.system import WaitForNode

    with pytest.raises(TimeoutError):
        await monitor.wait_for(WaitForNode(equipment="never_going_to_exist", timeout_s=1.0))


async def test_two_instances_of_one_equipment_are_both_seen(monitor, rabbitmq_url):
    a = await _toy_node(rabbitmq_url)
    b = await _toy_node(rabbitmq_url)
    await a.start()
    await b.start()
    try:
        assert await _wait_until(lambda: len(monitor.registry.list_nodes()) >= 2)
        ids = {n.instance_id for n in monitor.registry.list_nodes()}
        assert {a.instance_id, b.instance_id} <= ids
    finally:
        await a.drain()
        await b.drain()
