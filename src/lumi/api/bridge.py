"""The browser bridge: one generic handler, driven by the contract.

The browser talks to this, not to the broker. That is the whole security argument:
the broker is never exposed to an untrusted client, and *this* process decides what a
logged-in user may do -- with real identity, sessions, and revocation, none of which a
direct broker connection can give you.

There is no per-capability code here and none is generated. `BusProxy` holds one
`CapabilityClient` per capability (built from `REGISTRY`), and `BridgeSession` routes a
browser frame to the right one after checking the user's role. Add a capability to the
contract and the bridge serves it with no change.

Enforcement reuses `lumi.contracts.policy.permits`, the exact predicate the broker
would apply to a connecting user of the same role -- so the app-layer check and the
(defense-in-depth) broker permissions cannot disagree.

Wire format (browser <-> bridge), binary-safe so frames/masks pass through:

    [u32 header_len][header json][payload bytes]

header.kind: request | response | subscribe | unsubscribe | stream | error
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange

from lumi.base.mq import CapabilityClient, RemoteError
from lumi.base.mq.codec import encode as encode_body
from lumi.contracts import REGISTRY, Capability, Codec
from lumi.contracts.policy import permits, permits_read

log = logging.getLogger(__name__)


# --- wire framing -----------------------------------------------------------


def pack(header: dict[str, Any], payload: bytes = b"") -> bytes:
    head = json.dumps(header).encode()
    return struct.pack(">I", len(head)) + head + payload


def unpack(frame: bytes) -> tuple[dict[str, Any], bytes]:
    (head_len,) = struct.unpack(">I", frame[:4])
    header = json.loads(frame[4 : 4 + head_len])
    return header, frame[4 + head_len :]


# --- the shared pool of bus clients -----------------------------------------


class BusProxy:
    """One CapabilityClient per capability, shared across all browser sessions.

    The bridge connects to the broker ONCE, with a privileged credential, and fans out
    to every browser. Browsers never hold broker credentials.
    """

    def __init__(self, connection: AbstractConnection, channel: AbstractChannel) -> None:
        self.connection = connection
        self.channel = channel
        self.clients: dict[str, CapabilityClient] = {}
        self.caps: dict[str, tuple[str, Capability]] = {}
        self._exchanges: dict[str, AbstractExchange] = {}

    async def start(self) -> None:
        for contract in REGISTRY.values():
            exchange = self._exchanges.get(contract.exchange)
            if exchange is None:
                exchange = await self.channel.declare_exchange(
                    contract.exchange, ExchangeType(contract.exchange_type), durable=True
                )
                self._exchanges[contract.exchange] = exchange
            for cap in contract.capabilities:
                target = f"{contract.name}.{cap.name}"
                client = CapabilityClient(cap, contract.name, channel=self.channel, exchange=exchange)
                await client.start()
                self.clients[target] = client
                self.caps[target] = (contract.name, cap)
        log.info("bridge connected to the bus: %d capabilities", len(self.clients))

    async def stop(self) -> None:
        for client in self.clients.values():
            await client.stop()

    def resolve(self, target: str) -> tuple[str, Capability, CapabilityClient]:
        """The SHARED client for a target. Safe for RPC (correlation-id demuxes concurrent
        calls); NOT for streams -- use make_stream_client for those."""
        if target not in self.clients:
            raise KeyError(f"unknown target {target!r}")
        equipment, cap = self.caps[target]
        return equipment, cap, self.clients[target]

    async def make_stream_client(self, target: str) -> CapabilityClient:
        """A fresh client with its own queue, for one session's subscription.

        Streams are stateful: a shared client has one callback and one queue, so two
        browsers watching the camera would overwrite each other and one unsubscribe
        would stop the feed for both. Each session gets its own instead -- which is
        also how N browsers each receive the full feed.
        """
        equipment, cap = self.caps[target]
        exchange = self._exchanges[REGISTRY[equipment].exchange]
        client = CapabilityClient(cap, equipment, channel=self.channel, exchange=exchange)
        await client.start()
        return client


# --- one browser connection -------------------------------------------------


@dataclass
class BridgeSession:
    """Proxies one browser websocket to the bus, gated by the user's role.

    `ws` is any object with async `send_bytes(bytes)` and an async iterator of
    incoming `bytes` -- i.e. a Starlette/FastAPI WebSocket, but not tied to it.
    """

    ws: Any
    proxy: BusProxy
    role: str
    user: str = "anonymous"
    # This session's own stream clients, one per subscribed target -- its own queues,
    # independent of every other browser.
    _stream_clients: dict[str, CapabilityClient] = field(default_factory=dict)

    async def run(self) -> None:
        try:
            async for frame in self.ws.iter_bytes():
                # One bad frame must not kill the session -- errors go back to the
                # browser, and the connection stays up.
                try:
                    await self._on_frame(frame)
                except Exception:
                    log.exception("bridge frame handling failed for %s", self.user)
        except Exception:
            # The websocket itself closed/broke; that is a normal end, logged quietly.
            log.debug("bridge session for %s ended", self.user, exc_info=True)
        finally:
            await self._cleanup()

    async def _on_frame(self, frame: bytes) -> None:
        header, payload = unpack(frame)
        kind = header.get("kind")
        if kind == "request":
            await self._request(header, payload)
        elif kind == "subscribe":
            await self._subscribe(header)
        elif kind == "unsubscribe":
            await self._unsubscribe(header)
        else:
            await self._error(header.get("correlation_id"), "BadFrame", f"unknown kind {kind!r}")

    async def _request(self, header: dict, payload: bytes) -> None:
        cid = header.get("correlation_id")
        target = header.get("target", "")
        op_name = header.get("op", "")
        try:
            equipment, cap, client = self.proxy.resolve(target)
        except KeyError as exc:
            return await self._error(cid, "UnknownTarget", str(exc))

        keys = cap.keys(equipment)
        # A control verb (start/stop/state) vs a normal op -- both are permission-checked
        # against the SAME predicate the broker would use.
        is_control = header.get("control", False)
        routing_key = keys.control(op_name) if is_control else keys.request(op_name)
        if not permits(self.role, REGISTRY[equipment].exchange, routing_key):
            return await self._error(
                cid, "Forbidden",
                f"role {self.role!r} may not {op_name} on {target}",
            )

        try:
            if is_control:
                result = await self._control(client, op_name)
            else:
                result = await self._call(cap, client, op_name, payload)
        except RemoteError as exc:
            return await self._error(cid, exc.error_type, exc.error_message)
        except TimeoutError:
            return await self._error(cid, "Timeout", f"{target}.{op_name} did not answer")
        except Exception as exc:
            log.exception("bridge request failed")
            return await self._error(cid, type(exc).__name__, str(exc))

        model, wire_payload = result
        resp = {"kind": "response", "correlation_id": cid}
        if wire_payload is not None:
            resp["meta"] = model.model_dump()
            await self.ws.send_bytes(pack(resp, wire_payload))
        else:
            resp["body"] = model.model_dump()
            await self.ws.send_bytes(pack(resp))

    async def _call(self, cap: Capability, client: CapabilityClient, op_name: str, payload: bytes):
        op = cap.op(op_name)
        req = op.request.model_validate_json(payload or b"{}")
        result = await client.call(op_name, req)
        if op.response_codec is Codec.JSON:
            return result, None
        # The transport handed back a decoded (model, ndarray/bytes). Re-encode the
        # binary half to wire bytes for the browser -- pack() cannot concatenate a raw
        # ndarray, and the browser expects the same npy/raw framing the bus uses.
        model, decoded = result
        wire, _ = encode_body(model, op.response_codec, decoded)
        return model, wire
        return result  # (model, payload) tuple

    async def _control(self, client: CapabilityClient, verb: str):
        if verb == "state":
            return await client.get_state(), None
        if verb == "start":
            await client.start_streaming()
        elif verb == "stop":
            await client.stop_streaming()
        from lumi.contracts.payloads.common import Ack

        return Ack(), None

    async def _subscribe(self, header: dict) -> None:
        target = header.get("target", "")
        stream = header.get("stream", "")
        try:
            equipment, cap, client = self.proxy.resolve(target)
        except KeyError as exc:
            return await self._error(header.get("correlation_id"), "UnknownTarget", str(exc))

        spec = cap.stream if (cap.stream and cap.stream.name == stream) else cap.update
        if spec is None:
            return await self._error(header.get("correlation_id"), "NoStream", f"{target} has no {stream}")

        keys = cap.keys(equipment)
        pub_key = keys.publish if spec is cap.stream else keys.update
        if not permits_read(self.role, REGISTRY[equipment].exchange, pub_key):
            # This is where masks are kept from a viewer: it has no read permission on
            # detection.detection.pub, only detection.overlay.pub.
            return await self._error(
                header.get("correlation_id"), "Forbidden",
                f"role {self.role!r} may not subscribe to {target}.{stream}",
            )

        codec = spec.codec

        async def forward(model, decoded) -> None:
            # Re-encode the binary payload to wire bytes (npy/raw) for the browser, the
            # same as for RPCs; JSON streams carry their data in meta and need no body.
            if decoded is not None and codec is not Codec.JSON:
                body, _ = encode_body(model, codec, decoded)
            else:
                body = b""
            await self.ws.send_bytes(pack(
                {"kind": "stream", "target": target, "stream": stream, "meta": model.model_dump()},
                body,
            ))

        # A per-session client, so this browser's subscribe/unsubscribe never touches
        # another's feed.
        stream_client = await self.proxy.make_stream_client(target)
        if spec is cap.stream:
            await stream_client.subscribe_stream(forward)
        else:
            await stream_client.subscribe_updates(forward)
        self._stream_clients[target] = stream_client

    async def _unsubscribe(self, header: dict) -> None:
        target = header.get("target", "")
        client = self._stream_clients.pop(target, None)
        if client is not None:
            await client.stop()

    async def _error(self, cid, error_type: str, message: str) -> None:
        await self.ws.send_bytes(pack(
            {"kind": "error", "correlation_id": cid, "error_type": error_type, "error_message": message}
        ))

    async def _cleanup(self) -> None:
        for client in self._stream_clients.values():
            try:
                await client.stop()
            except Exception:
                pass
        self._stream_clients.clear()
