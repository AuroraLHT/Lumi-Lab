"""One client class for every capability.

Replaces BasicClient, BasicStreamClient and PubSubClient -- and, above them, the
whole of src/lumi/client/, where eleven of nineteen classes existed only to forward
eight keyword arguments to their parent.

The generated clients subclass this and add one typed method per op. Nothing else
is hand-written.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable

from aio_pika import Message
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage
from pydantic import BaseModel

from lumi.contracts import Capability, Codec

from . import envelope
from .codec import Payload, decode, encode
from .queues import ControlQueue, ReplyQueue, SubQueue

log = logging.getLogger(__name__)

StreamCallback = Callable[[BaseModel, Payload], Awaitable[None]]
StateCallback = Callable[[BaseModel], Awaitable[None]]


class RemoteError(Exception):
    """The server answered, and said no."""

    def __init__(self, source: str, op: str, error_type: str, message: str) -> None:
        super().__init__(f"{source}.{op} failed: [{error_type}] {message}")
        self.source = source
        self.op = op
        self.error_type = error_type
        self.error_message = message


class CapabilityClient:
    def __init__(
        self,
        capability: Capability,
        equipment: str,
        *,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        timeout: float = 10.0,
        name: str | None = None,
        actor: str | None = None,
    ) -> None:
        self.cap = capability
        self.equipment = equipment
        self.channel = channel
        self.exchange = exchange
        self.timeout = timeout
        # Who is responsible for what this client does, for the step journal --
        # "notebook", "mcp:agent", "ui:hliang16". Distinct from `name`, which
        # identifies the client *instance* and is generated.
        #
        # Self-asserted, so it is only as trustworthy as the caller. That is fine for
        # a notebook on the lab machine, and it is why the two authenticated front
        # doors -- the websocket bridge and the MCP HTTP transport -- set it from the
        # verified JWT subject when they construct their clients, rather than
        # forwarding anything a remote caller supplied.
        self.actor = actor
        self.keys = capability.keys(equipment)
        self.target = f"{equipment}.{capability.name}"
        self.name = name or f"{self.target}.client.{uuid.uuid4().hex[:6]}"

        self._futures: dict[str, asyncio.Future] = {}
        self._reply: ReplyQueue | None = None
        self._control: ControlQueue | None = None
        self._stream: SubQueue | None = None
        self._update: SubQueue | None = None
        self._state_q: SubQueue | None = None

        self.server_state: BaseModel | None = None
        self._on_stream: StreamCallback | None = None
        self._on_update: StreamCallback | None = None
        self._on_state: StateCallback | None = None

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Open the reply queue. Enough to make calls; subscriptions are separate,
        so a client that only wants to issue RPCs does not also receive a 30fps feed."""
        self._reply = ReplyQueue(self.channel, self._on_reply)
        await self._reply.start()

    async def stop(self) -> None:
        for q in (self._reply, self._control, self._stream, self._update, self._state_q):
            if q is not None:
                await q.stop(delete=True)
        self._reply = self._control = self._stream = self._update = self._state_q = None
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
        self._futures.clear()

    async def __aenter__(self) -> "CapabilityClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --- requests ---------------------------------------------------------

    async def _on_reply(self, message: AbstractIncomingMessage) -> None:
        cid = message.correlation_id
        fut = self._futures.pop(cid, None) if cid else None
        if fut is None or fut.done():
            return  # a late reply to a call that already timed out
        fut.set_result((message.body, dict(message.headers or {})))

    async def call(
        self, op_name: str, request: BaseModel | None = None, payload: Payload = None
    ) -> BaseModel | tuple[BaseModel, Payload]:
        """Issue one request. Returns the response model, or (model, payload) for a
        binary codec. Raises RemoteError if the server said no, TimeoutError if it
        said nothing."""
        if self._reply is None:
            raise RuntimeError(f"{self.name}: call start() before calling {op_name}")

        op = self.cap.op(op_name)
        req = request if request is not None else op.request()
        body, extra = encode(req, op.request_codec, payload)

        cid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # Register before publishing. PubSubClient.request() did it the other way
        # round, so a fast reply could arrive before its own future existed.
        self._futures[cid] = fut

        headers = envelope.request(self.name, op.name, str(op.request_codec), actor=self.actor, **extra)
        try:
            await self.exchange.publish(
                Message(
                    body=body,
                    headers=headers,
                    correlation_id=cid,
                    reply_to=self._reply.queue.name,  # type: ignore[union-attr]
                ),
                # rheed.camera.req.image -- the op is in the key so the broker can
                # authorize it. The header carries it too, for logging, but the server
                # dispatches on the key.
                routing_key=self.keys.request(op.name),
            )
            # Always bounded. PubSubClient.request() had no timeout at all: a lost
            # response hung the caller forever.
            raw_body, raw_headers = await asyncio.wait_for(fut, self.timeout)
        except TimeoutError:
            self._futures.pop(cid, None)
            raise TimeoutError(
                f"{self.target}.{op.name} did not answer within {self.timeout}s"
            ) from None
        finally:
            self._futures.pop(cid, None)

        if not raw_headers.get("succ", False):
            raise RemoteError(
                self.target, op.name,
                raw_headers.get("error_type", "Unknown"),
                raw_headers.get("error_message", ""),
            )

        model, payload_out = decode(op.response, op.response_codec, raw_body, raw_headers)
        return model if op.response_codec is Codec.JSON else (model, payload_out)

    # --- subscriptions ----------------------------------------------------

    async def subscribe_stream(self, callback: StreamCallback) -> None:
        if self.cap.stream is None:
            raise RuntimeError(f"{self.target} has no stream")
        self._on_stream = callback
        self._stream = SubQueue(
            self.channel, self._make_sub_handler(self.cap.stream, lambda: self._on_stream),
            exchange=self.exchange, routing_key=self.keys.publish,
        )
        await self._stream.start()

    async def unsubscribe_stream(self) -> None:
        if self._stream is not None:
            await self._stream.stop(delete=True)
            self._stream = None

    async def subscribe_updates(self, callback: StreamCallback) -> None:
        if self.cap.update is None:
            raise RuntimeError(f"{self.target} has no update channel")
        self._on_update = callback
        self._update = SubQueue(
            self.channel, self._make_sub_handler(self.cap.update, lambda: self._on_update),
            exchange=self.exchange, routing_key=self.keys.update,
        )
        await self._update.start()

    def _make_sub_handler(self, spec: Any, get_cb: Callable[[], StreamCallback | None]):
        async def handler(message: AbstractIncomingMessage) -> None:
            cb = get_cb()
            if cb is None:
                return
            try:
                model, payload = decode(spec.payload, spec.codec, message.body, dict(message.headers or {}))
            except Exception:
                log.exception("%s: undecodable %s message", self.name, spec.name)
                return
            try:
                await cb(model, payload)
            except Exception:
                log.exception("%s: %s callback failed", self.name, spec.name)

        return handler

    async def subscribe_state(self, callback: StateCallback | None = None) -> None:
        """Track the server's state as it changes."""
        self._on_state = callback

        async def handler(message: AbstractIncomingMessage) -> None:
            try:
                self.server_state = self.cap.state.model_validate_json(message.body)
            except Exception:
                log.exception("%s: undecodable state message", self.name)
                return
            if self._on_state is not None:
                await self._on_state(self.server_state)

        self._state_q = SubQueue(
            self.channel, handler, exchange=self.exchange, routing_key=self.keys.state
        )
        await self._state_q.start()

    # --- control ----------------------------------------------------------

    async def _control_call(self, verb: str, body: bytes = b"") -> tuple[bytes, dict[str, Any]]:
        if self._reply is None:
            raise RuntimeError(f"{self.name}: call start() first")

        cid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._futures[cid] = fut

        try:
            await self.exchange.publish(
                Message(
                    body=body,
                    headers=envelope.control_request(self.name, verb),
                    correlation_id=cid,
                    reply_to=self._reply.queue.name,  # type: ignore[union-attr]
                ),
                routing_key=self.keys.control(verb),
            )
            raw_body, raw_headers = await asyncio.wait_for(fut, self.timeout)
        except TimeoutError:
            raise TimeoutError(f"{self.target}: control {verb!r} timed out after {self.timeout}s") from None
        finally:
            self._futures.pop(cid, None)

        if not raw_headers.get("succ", False):
            raise RemoteError(
                self.target, f"control:{verb}",
                raw_headers.get("error_type", "Unknown"),
                raw_headers.get("error_message", ""),
            )
        return raw_body, raw_headers

    async def get_state(self) -> BaseModel:
        body, _ = await self._control_call("state")
        return self.cap.state.model_validate_json(body)

    async def start_streaming(self) -> None:
        """Tell the server to start pushing. Note this is a *global* flag on the
        server, not per-subscriber -- as it was before. The last client to say stop
        stops the stream for everyone."""
        await self._control_call("start")

    async def stop_streaming(self) -> None:
        await self._control_call("stop")

    async def shutdown_server(self) -> None:
        """Ask the node hosting this capability to exit. This actually works now."""
        await self._control_call("shutdown")
