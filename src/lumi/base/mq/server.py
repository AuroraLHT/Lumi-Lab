"""One server class for every capability.

Replaces BasicServer, BasicStreamServer and PubSubServer. They differed only in
which queues they opened and where they published, both of which the contract
already knows -- so there is nothing left for three classes to disagree about.

The handler is plain domain code: an async method per op, a `state()`, and (for a
stream) a `next()`. It never sees a routing key, an AMQP header, or a codec.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

from aio_pika import Message
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage
from pydantic import BaseModel, ValidationError

from lumi.contracts import Capability, Codec, ContractError, Kind, Op

from . import envelope
from .codec import Payload, decode, encode
from .control import ControlPlane, ControlResponse, control
from .queues import ControlQueue, WorkQueue
from .state import StatePublisher

log = logging.getLogger(__name__)

# A handler op returns either a model (JSON) or (model, payload) for a binary codec.
OpResult = BaseModel | tuple[BaseModel, Payload]


class CapabilityServer(ControlPlane):
    def __init__(
        self,
        capability: Capability,
        equipment: str,
        handler: Any,
        *,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        instance_id: str = "",
        stream_idle_s: float = 0.005,
    ) -> None:
        self.cap = capability
        self.equipment = equipment
        self.handler = handler
        self.channel = channel
        self.exchange = exchange
        self.instance_id = instance_id
        self.keys = capability.keys(equipment)
        self.source = f"{equipment}.{capability.name}"
        self.stream_idle_s = stream_idle_s

        # Bind ops to handler methods NOW. A handler that is missing an op is a
        # programming error, and it should surface at startup -- not on the fifth
        # message, in the middle of a growth.
        self._ops: dict[str, tuple[Op, Callable[..., Awaitable[OpResult]]]] = self._bind_ops()

        self._work: WorkQueue | None = None
        self._control: ControlQueue | None = None
        self._stream_task: asyncio.Task | None = None
        self._update_task: asyncio.Task | None = None
        self._streaming = False
        self._inflight = 0
        self._draining = False

        self.state = StatePublisher(self._read_state(), self._publish_state)

    # --- wiring -----------------------------------------------------------

    def _bind_ops(self) -> dict[str, tuple[Op, Callable]]:
        table: dict[str, tuple[Op, Callable]] = {}
        for op in self.cap.ops:
            fn = getattr(self.handler, op.name, None)
            if fn is None or not inspect.iscoroutinefunction(fn):
                raise ContractError(
                    f"{type(self.handler).__name__} does not implement "
                    f"`async def {op.name}(self, req) -> ...` required by "
                    f"{self.equipment}.{self.cap.name}"
                )
            table[op.name] = (op, fn)
        if self.cap.stream is not None and not hasattr(self.handler, "next"):
            raise ContractError(
                f"{type(self.handler).__name__} must implement `async def next()` "
                f"to serve the {self.cap.name} stream"
            )
        if self.cap.update is not None and not hasattr(self.handler, "next_update"):
            raise ContractError(
                f"{type(self.handler).__name__} must implement `async def next_update()` "
                f"to serve the {self.cap.name} update channel"
            )
        return table

    def _read_state(self) -> BaseModel:
        reader = getattr(self.handler, "state", None)
        if reader is None:
            return self.cap.state()
        return reader()

    def refresh_state(self) -> None:
        """Re-read the handler's state and publish if it changed."""
        self.state.set(self._read_state())

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.state.bind_loop(asyncio.get_running_loop())

        if self.cap.ops:
            self._work = WorkQueue(
                self.channel,
                self._on_request,
                name=self.keys.work_queue,
                exchange=self.exchange,
                # `rheed.camera.req.*` -- one queue for every op of this capability. The
                # per-op key exists so the *broker* can authorize them separately; the
                # server does not care which one arrived.
                routing_key=self.keys.request_pattern,
            )
            await self._work.start()

        self._control = ControlQueue(
            self.channel,
            self._on_control,
            exchange=self.exchange,
            routing_key=self.keys.control_pattern,
        )
        await self._control.start()

        if self.cap.stream is not None:
            self._stream_task = asyncio.create_task(
                self._stream_loop(), name=f"stream:{self.source}"
            )

        if self.cap.update is not None:
            # Updates are not gated by start/stop: they are results being broadcast
            # (an MI script finished), not a feed someone subscribes to.
            self._update_task = asyncio.create_task(
                self._update_loop(), name=f"update:{self.source}"
            )

        self.state.update(is_running=True)
        log.info("%s serving (ops=%s)", self.source, ",".join(self._ops) or "-")

    async def drain(self, timeout: float = 10.0) -> None:
        """Stop accepting work, finish what is in flight, then let go.

        The old nodes had no such path: every one of them ended in
        `await asyncio.Future()` with the shutdown code sitting unreachable below it.
        """
        self._draining = True
        self._streaming = False

        for attr in ("_stream_task", "_update_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)

        # Cancel intake first, so nothing new arrives while we wait.
        if self._work is not None:
            await self._work.stop()  # not deleted: it is durable and shared
        if self._control is not None:
            await self._control.stop(delete=True)

        deadline = asyncio.get_running_loop().time() + timeout
        while self._inflight and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if self._inflight:
            log.warning("%s drained with %d request(s) still in flight", self.source, self._inflight)

        self.state.update(is_running=False, is_streaming=False)
        log.info("%s drained", self.source)

    # --- requests ---------------------------------------------------------

    async def _on_request(self, message: AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            self._inflight += 1
            try:
                await self._handle_request(message)
            finally:
                self._inflight -= 1

    @staticmethod
    def _op_from_key(routing_key: str | None) -> str | None:
        """The op is the last segment of the routing key: rheed.camera.req.image.

        The KEY is authoritative, never the header. The broker authorizes on the routing
        key, so if we dispatched on `headers["op"]` a caller permitted to publish
        `...req.image` could set a header saying `update_camera_config` and have it
        honoured -- the authorization would be decorative. This is the one place where
        getting it backwards silently undoes the whole permission model.
        """
        if not routing_key:
            return None
        head, _, op = routing_key.rpartition(".")
        return op or None

    async def _handle_request(self, message: AbstractIncomingMessage) -> None:
        headers = dict(message.headers or {})
        op_name = self._op_from_key(message.routing_key)
        entry = self._ops.get(op_name) if op_name else None

        if entry is None:
            # One behaviour for an unknown op, always. The old code had three: STFT
            # raised ValueError and killed its own consumer, detection returned None
            # and blew up in publish_callback, integrator answered properly.
            await self._reply_error(
                message,
                op_name or "?",
                "UnknownOp",
                f"{self.source} has no op {op_name!r}; known: {', '.join(self._ops)}",
            )
            return

        op, fn = entry
        try:
            req, _ = decode(op.request, op.request_codec, message.body, headers)
        except ValidationError as exc:
            await self._reply_error(message, op.name, "BadRequest", exc.json())
            return
        except Exception as exc:
            await self._reply_error(message, op.name, "BadRequest", str(exc))
            return

        try:
            result = await fn(req)
        except Exception as exc:
            log.exception("%s.%s failed", self.source, op.name)
            await self._reply_error(message, op.name, type(exc).__name__, str(exc))
            return

        try:
            model, payload = result if isinstance(result, tuple) else (result, None)
            body, extra = encode(model, op.response_codec, payload)
        except Exception as exc:
            log.exception("%s.%s produced an unencodable response", self.source, op.name)
            await self._reply_error(message, op.name, "BadResponse", str(exc))
            return

        hdrs = envelope.response(self.source, op.name, str(op.response_codec), **extra)
        await self._reply(message, body, hdrs)
        self.refresh_state()

    async def _reply_error(
        self, message: AbstractIncomingMessage, op: str, error_type: str, error_message: str
    ) -> None:
        hdrs = envelope.response(
            self.source, op, str(Codec.JSON),
            succ=False, error_type=error_type, error_message=error_message,
        )
        await self._reply(message, b"", hdrs)

    async def _reply(self, message: AbstractIncomingMessage, body: bytes, headers: dict[str, Any]) -> None:
        if not message.reply_to:
            log.warning("%s got a request with no reply_to; dropping the response", self.source)
            return
        # Replies go to the default exchange, straight to the caller's private queue.
        # PubSubServer used to broadcast its replies on a shared key, so every
        # subscriber received every other client's private RPC response.
        await self.channel.default_exchange.publish(
            Message(body=body, headers=headers, correlation_id=message.correlation_id),
            routing_key=message.reply_to,
        )

    # --- streaming --------------------------------------------------------

    async def _stream_loop(self) -> None:
        spec = self.cap.stream
        assert spec is not None
        while True:
            if not self._streaming:
                await asyncio.sleep(0.05)
                continue
            try:
                item = await self.handler.next()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s stream handler failed", self.source)
                await asyncio.sleep(0.1)
                continue

            if item is None:
                await asyncio.sleep(self.stream_idle_s)
                continue

            model, payload = item if isinstance(item, tuple) else (item, None)
            try:
                body, extra = encode(model, spec.codec, payload)
            except Exception:
                log.exception("%s could not encode a stream item", self.source)
                continue

            await self.exchange.publish(
                Message(body=body, headers=envelope.stream(self.source, spec.name, str(spec.codec), **extra)),
                routing_key=self.keys.publish,
                # A broadcast with nobody listening is normal, not an error. aio_pika
                # defaults to mandatory=True, so an unsubscribed stream would have the
                # broker return every frame as unroutable and raise DeliveryError --
                # once per frame, at 30fps, for a camera nobody is watching.
                mandatory=False,
            )

    async def _update_loop(self) -> None:
        """Drain the handler's pending updates and broadcast them."""
        spec = self.cap.update
        assert spec is not None
        while True:
            try:
                item = await self.handler.next_update()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s update handler failed", self.source)
                await asyncio.sleep(0.1)
                continue

            if item is None:
                await asyncio.sleep(self.stream_idle_s)
                continue

            model, payload = item if isinstance(item, tuple) else (item, None)
            try:
                await self.publish_update(model, payload)
            except Exception:
                log.exception("%s could not publish an update", self.source)

    async def publish_update(self, model: BaseModel, payload: Payload = None) -> None:
        """Broadcast on the update channel (pubsub capabilities only)."""
        spec = self.cap.update
        if spec is None:
            raise ContractError(f"{self.source} has no update channel")
        body, extra = encode(model, spec.codec, payload)
        await self.exchange.publish(
            Message(body=body, headers=envelope.update(self.source, spec.name, str(spec.codec), **extra)),
            routing_key=self.keys.update,
            mandatory=False,  # broadcasts do not require a listener
        )

    async def _publish_state(self, state: BaseModel) -> None:
        await self.exchange.publish(
            Message(body=state.model_dump_json().encode(), headers=envelope.state(self.source)),
            routing_key=self.keys.state,
            mandatory=False,
        )

    # --- control ----------------------------------------------------------

    async def _on_control(self, message: AbstractIncomingMessage) -> None:
        headers = dict(message.headers or {})
        # Same rule as requests: the routing key decides, not the header. Otherwise a
        # client allowed only `*.ctrl.start` could send `shutdown` in a header.
        verb = self._op_from_key(message.routing_key) or ""
        res = await self.dispatch_control(verb, message.body, headers)

        if not message.reply_to:
            return
        hdrs = envelope.control_response(
            self.source, verb,
            succ=res.succ, error_type=res.error_type, error_message=res.error_message,
        )
        hdrs.update(res.headers)
        await self.channel.default_exchange.publish(
            Message(body=res.body, headers=hdrs, correlation_id=message.correlation_id),
            routing_key=message.reply_to,
        )

    @control("state")
    async def _ctl_state(self, body: bytes, headers: dict) -> ControlResponse:
        self.refresh_state()
        return ControlResponse.ok(self.state.state.model_dump_json().encode())

    @control("start")
    async def _ctl_start(self, body: bytes, headers: dict) -> ControlResponse:
        if self.cap.stream is None:
            return ControlResponse.error("NotStreamable", f"{self.source} has no stream")
        self._streaming = True
        self.state.update(is_streaming=True)
        return ControlResponse.ok()

    @control("stop")
    async def _ctl_stop(self, body: bytes, headers: dict) -> ControlResponse:
        if self.cap.stream is None:
            return ControlResponse.error("NotStreamable", f"{self.source} has no stream")
        self._streaming = False
        self.state.update(is_streaming=False)
        return ControlResponse.ok()

    @control("shutdown")
    async def _ctl_shutdown(self, body: bytes, headers: dict) -> ControlResponse:
        """Ask the node to exit. Registered, unlike before -- every client has been
        sending `shutdown` at a server that only ever registered `terminate`."""
        asyncio.create_task(self._request_node_shutdown())
        return ControlResponse.ok()

    async def _request_node_shutdown(self) -> None:
        await asyncio.sleep(0.05)  # let the reply leave first
        node = getattr(self.handler, "_node", None) or getattr(self, "node", None)
        if node is not None:
            node.request_shutdown()
        else:
            log.warning("%s: shutdown requested but no node is attached", self.source)
