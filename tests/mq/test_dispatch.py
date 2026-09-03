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


# --- the step journal hook -----------------------------------------------------


class FakeJournal:
    """Records what the dispatch asked it to write, in order."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def begin(self, kind, *, params=None, actor=None, source=None, **_):
        self.rows.append({"kind": kind, "params": params, "actor": actor,
                          "source": source, "ok": None, "error": None})
        return len(self.rows) - 1

    async def end(self, step_id, *, ok, result=None, error=None):
        if step_id is None:
            return
        self.rows[step_id].update(ok=ok, error=error)


def _message(op: str, *, actor: str | None = None):
    from unittest.mock import AsyncMock

    msg = MagicMock()
    msg.routing_key = f"test.thing.req.{op}"
    msg.body = b"{}"
    msg.reply_to = "reply-q"
    msg.correlation_id = "cid"
    headers = {"codec": "json", "message_source": "notebook"}
    if actor is not None:
        headers["actor"] = actor
    msg.headers = headers
    return msg


JOURNAL_CAP = Capability(
    name="thing",
    kind=Kind.RPC,
    state=Empty,
    ops=(
        Op("alpha", Empty, Ack, journal=True),      # journaled by the dispatch
        Op("beta", Empty, Ack),                     # a read: never journaled
        Op("slow", Empty, Ack, journal=True),       # journaled by the handler itself
    ),
)


def _journal_server(handler, journal):
    from unittest.mock import AsyncMock

    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock()
    return CapabilityServer(
        JOURNAL_CAP, "test", handler, channel=channel, exchange=MagicMock(), journal=journal
    )


class Journalled:
    journals_own = frozenset({"slow"})

    async def alpha(self, req): return Ack()
    async def beta(self, req): return Ack()
    async def slow(self, req): raise RuntimeError("heating laser is off")


async def test_dispatch_journals_a_world_changing_op():
    journal = FakeJournal()
    server = _journal_server(Journalled(), journal)
    await server._handle_request(_message("alpha", actor="hliang16"))

    assert len(journal.rows) == 1
    assert journal.rows[0]["kind"] == "alpha"
    assert journal.rows[0]["ok"] is True
    assert journal.rows[0]["actor"] == "hliang16"
    assert journal.rows[0]["source"] == "notebook"


async def test_dispatch_does_not_journal_a_read():
    journal = FakeJournal()
    server = _journal_server(Journalled(), journal)
    await server._handle_request(_message("beta"))
    assert journal.rows == []


async def test_dispatch_leaves_handler_owned_ops_alone_when_they_succeed():
    """A long-running op opens its own row when its task starts, so the dispatch must
    not open a second one -- that would record a 27-minute ramp as the milliseconds it
    took to hand back a TaskAck."""
    journal = FakeJournal()

    class Ok(Journalled):
        async def slow(self, req): return Ack()

    server = _journal_server(Ok(), journal)
    await server._handle_request(_message("slow"))
    assert journal.rows == []


async def test_dispatch_journals_a_handler_owned_op_that_refuses():
    """...but if it raises before its task starts, the handler journaled nothing, and
    a refused ramp would otherwise leave no trace at all."""
    journal = FakeJournal()
    server = _journal_server(Journalled(), journal)
    await server._handle_request(_message("slow", actor="hliang16"))

    assert len(journal.rows) == 1
    assert journal.rows[0]["kind"] == "slow"
    assert journal.rows[0]["ok"] is False
    assert "heating laser is off" in journal.rows[0]["error"]
    assert journal.rows[0]["actor"] == "hliang16"


async def test_a_server_with_no_journal_still_dispatches():
    server = _journal_server(Journalled(), None)
    await server._handle_request(_message("alpha"))  # must not raise
