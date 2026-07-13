"""Server dispatch construction. Tier T0: no broker.

The seven hand-written `if headers["request_type"] == ...` ladders are replaced by a
table built from the contract. Building it eagerly means a handler that does not
satisfy its contract fails when the node boots, rather than the first time someone
happens to call the op it forgot -- which, for a capability like `stft`, could be
days into a run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lumi.base.mq.server import CapabilityServer
from lumi.contracts import Capability, ContractError, Kind
from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.spec import Op, StreamSpec

CAP = Capability(
    name="thing",
    kind=Kind.RPC,
    state=Empty,
    ops=(Op("alpha", Empty, Ack), Op("beta", Empty, Ack)),
)

STREAM_CAP = Capability(
    name="feed",
    kind=Kind.DUPLEX,
    state=Empty,
    ops=(Op("alpha", Empty, Ack),),
    stream=StreamSpec("tick", Ack),
)


def _server(cap, handler):
    return CapabilityServer(
        cap, "test", handler,
        channel=MagicMock(), exchange=MagicMock(),
    )


class Complete:
    async def alpha(self, req): return Ack()
    async def beta(self, req): return Ack()


class MissingBeta:
    async def alpha(self, req): return Ack()


class BetaIsNotAsync:
    async def alpha(self, req): return Ack()
    def beta(self, req): return Ack()  # noqa: ASYNC  -- deliberately wrong


def test_a_complete_handler_binds_every_op():
    server = _server(CAP, Complete())
    assert set(server._ops) == {"alpha", "beta"}


def test_a_missing_op_fails_at_construction():
    with pytest.raises(ContractError, match="does not implement.*beta"):
        _server(CAP, MissingBeta())


def test_a_sync_op_fails_at_construction():
    """A def where the contract needs an async def would otherwise return a
    coroutine-less value and fail deep inside the reply path."""
    with pytest.raises(ContractError, match="beta"):
        _server(CAP, BetaIsNotAsync())


def test_a_stream_capability_needs_a_next():
    class NoNext:
        async def alpha(self, req): return Ack()

    with pytest.raises(ContractError, match="next"):
        _server(STREAM_CAP, NoNext())


def test_a_stream_capability_with_next_is_fine():
    class WithNext:
        async def alpha(self, req): return Ack()
        async def next(self): return None

    server = _server(STREAM_CAP, WithNext())
    assert server.cap.stream is not None


def test_control_verbs_are_present_on_the_server():
    server = _server(CAP, Complete())
    # `shutdown` in particular: the old servers registered `terminate` while every
    # client sent `shutdown`, so it never once worked.
    assert set(server.control_verbs) >= {"state", "start", "stop", "shutdown"}


def test_routing_keys_come_from_the_contract():
    server = _server(CAP, Complete())
    assert server.keys.work_queue == "q.test.thing.req"
    # The op is IN the key, so RabbitMQ can authorize `alpha` separately from `beta`.
    assert server.keys.request("alpha") == "test.thing.req.alpha"
    assert server.keys.control("shutdown") == "test.thing.ctrl.shutdown"
    # ...and the server binds the wildcard, so it still serves them all from one queue.
    assert server.keys.request_pattern == "test.thing.req.*"
    assert server.keys.control_pattern == "test.thing.ctrl.*"
