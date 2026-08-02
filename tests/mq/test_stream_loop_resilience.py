"""The stream loop must outlive a transient publish failure. Tier T0: no broker.

The bug this pins: `_stream_loop` guarded handler.next() and encode(), but published
unguarded. The node connects with `connect_robust`, so a broker blip closes the channel
underneath the loop and raises ChannelInvalidStateError there -- once. That one
exception ended the task, and because the task object is held on `self._stream_task` it
was never garbage collected, which is the only moment asyncio would have printed "Task
exception was never retrieved".

The result, observed live on two capabilities in two different processes: producer
threads healthy and counting, queues filling and being dropped, nothing on the wire,
nothing in the log, and `is_streaming` still True hours later.
"""

from __future__ import annotations

import asyncio

import pytest

from lumi.base.mq.server import CapabilityServer
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.payloads.camera import JpegMeta


class FlakyExchange:
    """Publishes, but fails on demand -- the way a closed RobustChannel does."""

    def __init__(self, fail_on: set[int] | None = None, exc: Exception | None = None):
        self.published: list[bytes] = []
        self.fail_on = fail_on or set()
        self.exc = exc or RuntimeError("channel closed")
        self.n = 0

    async def publish(self, message, routing_key, **kw):
        # Yield. A coroutine that never awaits does not hand control back, and
        # _stream_loop would spin without ever letting the test's sleep resume.
        await asyncio.sleep(0)
        self.n += 1
        if self.n in self.fail_on:
            raise self.exc
        self.published.append(message.body)


class Handler:
    """Serves chamber.camera's ops nominally; only next() matters here."""

    def __init__(self) -> None:
        self.blob = b"\xff\xd8\xff\xe0" + b"jpeg" * 8 + b"\xff\xd9"

    async def image(self, req):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def get_camera_config(self, req):  # pragma: no cover
        raise NotImplementedError

    async def update_camera_config(self, req):  # pragma: no cover
        raise NotImplementedError

    async def next(self):
        await asyncio.sleep(0)
        return (
            JpegMeta(time=1.0, uuid="u", time_stamp="ts", width=64, height=48,
                     channels=3, quality=80),
            self.blob,
        )


def build(exchange) -> CapabilityServer:
    server = CapabilityServer(
        CHAMBER.capability("camera"), "test", Handler(),
        channel=object(), exchange=exchange, instance_id="t",
    )
    server._streaming = True
    return server


async def run_loop(server, seconds=0.3):
    task = asyncio.create_task(server._stream_loop())
    task.add_done_callback(server._on_loop_finished)
    await asyncio.sleep(seconds)
    return task


async def test_a_transient_publish_failure_does_not_kill_the_stream():
    exchange = FlakyExchange(fail_on={3})
    server = build(exchange)

    task = await run_loop(server)
    try:
        assert not task.done(), "one bad publish ended the stream loop"
        assert len(exchange.published) > 5, "the loop stopped producing after the failure"
    finally:
        task.cancel()


async def test_the_failure_is_reported_on_state_not_swallowed():
    exchange = FlakyExchange(fail_on={2})
    server = build(exchange)

    task = asyncio.create_task(server._stream_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The error surfaced somewhere a client can actually see it.
    assert server.state.state.error is not None


async def test_the_error_clears_once_publishing_recovers():
    """A stale error pinned to a working capability is its own kind of lie."""
    exchange = FlakyExchange(fail_on={1})
    server = build(exchange)

    task = await run_loop(server, seconds=0.2)
    try:
        assert server.state.state.error is None
        assert server._stream_errors == 0
    finally:
        task.cancel()


async def test_a_dead_loop_is_logged_and_flips_is_streaming_off(caplog):
    """If a loop does die, it must never be silent again.

    is_streaming is what the frontend trusts; leaving it True over a dead loop is the
    part that made this invisible from outside the node.
    """
    server = build(FlakyExchange())
    server.state.update(is_streaming=True)

    async def boom():
        raise RuntimeError("loop died")

    task = asyncio.create_task(boom(), name="stream:test.camera")
    task.add_done_callback(server._on_loop_finished)
    await asyncio.sleep(0.05)

    assert server.state.state.is_streaming is False
    assert "RuntimeError" in (server.state.state.error or "")
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_draining_does_not_report_a_fault():
    """drain() cancels these tasks on purpose."""
    server = build(FlakyExchange())
    server._draining = True

    task = asyncio.create_task(server._stream_loop())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert server.state.state.error is None
