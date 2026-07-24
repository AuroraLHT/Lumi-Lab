"""End-to-end transport. Tier T1: needs a real broker.

Deliberately not faked. Everything asserted here -- named vs. exclusive queues,
reply_to, competing consumers, message TTL, per-subscriber fanout -- is exactly what
an in-process fake would have stubbed out, and exactly where the old code was wrong.

Runs against a testcontainers RabbitMQ, or whatever LUMI_TEST_AMQP_URL points at.
The test contract below has its own exchange (LUMI_TEST) and its own routing keys,
so it cannot collide with live equipment even on a shared broker.
"""

from __future__ import annotations

import asyncio

import pytest
from aio_pika import ExchangeType, connect_robust
from pydantic import BaseModel

from lumi.base.mq import CapabilityClient, CapabilityServer, RemoteError
from lumi.contracts import Capability, EquipmentContract, Kind
from lumi.contracts.payloads.common import Ack, Empty, ServerStateBase
from lumi.contracts.spec import Op, StreamSpec

pytestmark = pytest.mark.broker


class EchoRequest(BaseModel):
    text: str


class EchoResponse(BaseModel):
    text: str
    served_by: str = ""


class Tick(BaseModel):
    n: int


class ProbeReadout(BaseModel):
    calls: int = 0


class ProbeState(ServerStateBase, ProbeReadout):
    pass


PROBE = Capability(
    name="probe",
    kind=Kind.DUPLEX,
    state=ProbeState,
    ops=(
        Op("echo", EchoRequest, EchoResponse),
        Op("boom", Empty, Ack),
        Op("slow", Empty, Ack),
    ),
    stream=StreamSpec("tick", Tick),
)

TEST_CONTRACT = EquipmentContract(
    name="lumitest", exchange="LUMI_TEST", version="test", capabilities=(PROBE,)
)


class ProbeHandler:
    def __init__(self, name: str = "a") -> None:
        self.name = name
        self.calls = 0
        self._n = 0
        self.streaming_allowed = True

    async def echo(self, req: EchoRequest) -> EchoResponse:
        self.calls += 1
        return EchoResponse(text=req.text, served_by=self.name)

    async def boom(self, req: Empty) -> Ack:
        raise RuntimeError("handler exploded")

    async def slow(self, req: Empty) -> Ack:
        await asyncio.sleep(5)
        return Ack()

    async def next(self):
        self._n += 1
        return Tick(n=self._n)

    def readout(self) -> ProbeReadout:
        return ProbeReadout(calls=self.calls)


@pytest.fixture
async def bus(rabbitmq_url):
    conn = await connect_robust(rabbitmq_url)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=8)
    # Topic, like the real exchanges: the server binds `lumitest.probe.req.*`, which a
    # direct exchange cannot match.
    exchange = await channel.declare_exchange("LUMI_TEST", ExchangeType.TOPIC, auto_delete=True)
    yield channel, exchange
    # Leave nothing behind: the work queue is durable by design.
    try:
        q = await channel.get_queue(PROBE.keys("lumitest").work_queue)
        await q.delete(if_unused=False, if_empty=False)
    except Exception:
        pass
    await conn.close()


async def _serve(bus, handler=None):
    channel, exchange = bus
    server = CapabilityServer(
        PROBE, "lumitest", handler or ProbeHandler(),
        channel=channel, exchange=exchange,
    )
    await server.start()
    return server


async def _client(bus, timeout=5.0):
    channel, exchange = bus
    client = CapabilityClient(PROBE, "lumitest", channel=channel, exchange=exchange, timeout=timeout)
    await client.start()
    return client


async def test_request_response(bus):
    server = await _serve(bus)
    client = await _client(bus)
    try:
        res = await client.call("echo", EchoRequest(text="hello"))
        assert res.text == "hello"
    finally:
        await client.stop()
        await server.drain()


async def test_unknown_op_answers_and_leaves_the_consumer_alive(bus):
    """The old behaviour was one of three things depending on which file you were in:
    STFT raised ValueError (killing its own consumer), detection returned None
    (AttributeError in the reply path), integrator answered properly."""
    server = await _serve(bus)
    client = await _client(bus)
    try:
        with pytest.raises(RemoteError) as exc:
            await _call_unknown(client)
        assert exc.value.error_type == "UnknownOp"

        # The server must still be serving.
        res = await client.call("echo", EchoRequest(text="still here"))
        assert res.text == "still here"
    finally:
        await client.stop()
        await server.drain()


async def _call_unknown(client):
    """Send an op the contract does not declare, bypassing the typed client."""
    import uuid

    from aio_pika import Message

    from lumi.base.mq import envelope

    cid = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    client._futures[cid] = fut
    await client.exchange.publish(
        Message(
            body=b"{}",
            headers=envelope.request(client.name, "nonexistent", "json"),
            correlation_id=cid,
            reply_to=client._reply.queue.name,
        ),
        routing_key=client.keys.request("nonexistent"),
    )
    body, headers = await asyncio.wait_for(fut, 5)
    if not headers.get("succ", False):
        raise RemoteError("lumitest.probe", "nonexistent",
                          headers.get("error_type", ""), headers.get("error_message", ""))


async def test_handler_exception_becomes_an_error_response(bus):
    server = await _serve(bus)
    client = await _client(bus)
    try:
        with pytest.raises(RemoteError) as exc:
            await client.call("boom")
        assert exc.value.error_type == "RuntimeError"
        assert "exploded" in exc.value.error_message

        res = await client.call("echo", EchoRequest(text="alive"))
        assert res.text == "alive"
    finally:
        await client.stop()
        await server.drain()


async def test_bad_request_body_is_rejected_not_crashed(bus):
    server = await _serve(bus)
    client = await _client(bus)
    try:
        # `echo` requires `text`; send a body that does not satisfy the model.
        import uuid

        from aio_pika import Message

        from lumi.base.mq import envelope

        cid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        client._futures[cid] = fut
        await client.exchange.publish(
            Message(
                body=b'{"wrong_field": 1}',
                headers=envelope.request(client.name, "echo", "json"),
                correlation_id=cid,
                reply_to=client._reply.queue.name,
            ),
            routing_key=client.keys.request("echo"),
        )
        _, headers = await asyncio.wait_for(fut, 5)
        assert headers["succ"] is False
        assert headers["error_type"] == "BadRequest"
    finally:
        await client.stop()
        await server.drain()


async def test_two_servers_share_the_work_and_do_not_double_answer(bus):
    """The old servers each bound an *exclusive* queue to the well-known request key,
    so a second instance meant every request was answered twice. A named, shared work
    queue makes them compete instead -- which is what lets a node scale out at all."""
    a = await _serve(bus, ProbeHandler("a"))
    b = await _serve(bus, ProbeHandler("b"))
    client = await _client(bus)
    try:
        results = await asyncio.gather(
            *(client.call("echo", EchoRequest(text=str(i))) for i in range(50))
        )
        assert sorted(r.text for r in results) == sorted(str(i) for i in range(50))
        servers_used = {r.served_by for r in results}
        assert servers_used <= {"a", "b"}
        # Both did some of the work: this is a work queue, not a broadcast.
        assert len(servers_used) == 2, f"only {servers_used} served -- not load balancing"
    finally:
        await client.stop()
        await a.drain()
        await b.drain()


async def test_timeout_raises_instead_of_hanging(bus):
    """PubSubClient.request() had no timeout at all -- a lost response hung the
    caller forever."""
    server = await _serve(bus)
    client = await _client(bus, timeout=0.5)
    try:
        with pytest.raises(TimeoutError, match="did not answer"):
            await client.call("slow")
    finally:
        await client.stop()
        await server.drain()


async def test_stream_is_off_until_started_and_fans_out(bus):
    server = await _serve(bus)
    c1 = await _client(bus)
    c2 = await _client(bus)
    got1: list[int] = []
    got2: list[int] = []
    try:
        await c1.subscribe_stream(lambda m, p: _collect(got1, m))
        await c2.subscribe_stream(lambda m, p: _collect(got2, m))

        await asyncio.sleep(0.3)
        assert not got1, "stream delivered before anyone asked it to start"

        await c1.start_streaming()
        await asyncio.sleep(0.5)
        await c1.stop_streaming()
        n1, n2 = len(got1), len(got2)
        assert n1 > 0

        # Every subscriber gets every message -- this is how N browsers each see the
        # full camera feed.
        assert n2 > 0

        await asyncio.sleep(0.3)
        assert len(got1) == n1, "stream kept publishing after stop"
    finally:
        await c1.stop()
        await c2.stop()
        await server.drain()


async def _collect(sink: list, model) -> None:
    sink.append(model.n)


async def test_state_is_pollable_and_broadcast_on_change(bus):
    handler = ProbeHandler()
    server = await _serve(bus, handler)
    client = await _client(bus)
    seen: list[int] = []
    try:
        await client.subscribe_state(lambda s: _collect_state(seen, s))

        state = await client.get_state()
        assert state.is_running is True
        assert state.calls == 0

        await client.call("echo", EchoRequest(text="x"))
        await asyncio.sleep(0.3)

        state = await client.get_state()
        assert state.calls == 1
        assert seen, "state change was not broadcast"
    finally:
        await client.stop()
        await server.drain()


async def _collect_state(sink: list, state) -> None:
    sink.append(state.calls)


async def test_shutdown_control_verb_is_answered(bus):
    """It has never been. Clients sent `shutdown`; servers registered `terminate`;
    the control handler replied to nothing on an unknown verb, so the client just
    timed out and returned an empty message."""
    server = await _serve(bus)
    client = await _client(bus)
    try:
        await client.shutdown_server()  # must not raise, must not time out
    finally:
        await client.stop()
        await server.drain()


async def test_streaming_with_no_subscribers_is_not_an_error(bus):
    """aio_pika publishes with mandatory=True by default, so an unroutable message is
    returned by the broker and raises DeliveryError. A broadcast with nobody listening
    is normal -- otherwise the camera raises once per frame, at 30fps, the moment the
    last browser closes its tab."""
    server = await _serve(bus)
    client = await _client(bus)
    try:
        await client.start_streaming()   # streaming on, but nobody subscribed
        await asyncio.sleep(1.0)

        # The server must still be alive and answering.
        res = await client.call("echo", EchoRequest(text="alive"))
        assert res.text == "alive"

        # And its stream task must not have died.
        assert server._stream_task is not None and not server._stream_task.done()
    finally:
        await client.stop()
        await server.drain()


async def test_drain_finishes_inflight_work(bus):
    server = await _serve(bus)
    client = await _client(bus, timeout=10)
    try:
        task = asyncio.create_task(client.call("echo", EchoRequest(text="inflight")))
        await asyncio.sleep(0.05)
        await server.drain(timeout=5)
        assert (await task).text == "inflight"
    finally:
        await client.stop()
