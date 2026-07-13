"""Emit the Python clients.

Real .py files, not classes conjured at import time: a generated client should be
greppable, readable in a debugger, and land in a stack trace at a line you can open.

This replaces src/lumi/client/ (837 lines, of which eleven of nineteen classes added
nothing but forwarding eight keyword arguments) and the client half of each
communication.py. Every one of those hand-written methods was the same five lines:

    async def get_bboxes(self):
        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="bboxes")
        return await self.request(request_message)

so they generate cleanly.
"""

from __future__ import annotations

from lumi.contracts import Capability, Codec, EquipmentContract

from .common import all_models, banner, client_class, import_block, pascal, payload_type, return_type

HEADER = '''"""Generated clients for the {equipment} node."""

from __future__ import annotations

from typing import Awaitable, Callable

import numpy as np
from aio_pika.abc import AbstractChannel, AbstractExchange

from lumi.base.mq import CapabilityClient
from lumi.contracts.{module} import {caps_import}
{model_imports}

'''


def _op_method(cap: Capability, op) -> str:
    takes_args = bool(op.request.model_fields)
    ret = return_type(op.response, op.response_codec)

    if takes_args:
        sig = f"    async def {op.name}(self, req: {op.request.__name__}) -> {ret}:"
        call = f'        return await self.call("{op.name}", req)  # type: ignore[return-value]'
    else:
        sig = f"    async def {op.name}(self) -> {ret}:"
        call = f'        return await self.call("{op.name}")  # type: ignore[return-value]'

    doc = op.doc or f"Call {cap.name}.{op.name}."
    body = [sig, f'        """{doc}"""']
    if op.response_codec is not Codec.JSON:
        binary = payload_type(op.response_codec)
        body.append(f"        # Returns (metadata, {binary}); the array never passes through JSON.")
    body.append(call)
    return "\n".join(body)


def _stream_methods(cap: Capability) -> list[str]:
    out: list[str] = []
    spec = cap.stream
    if spec is not None:
        binary = payload_type(spec.codec) or "None"
        out.append(
            f"""    async def on_{spec.name}(
        self, callback: Callable[[{spec.payload.__name__}, {binary}], Awaitable[None]]
    ) -> None:
        \"\"\"Subscribe to the {spec.name} stream.

        Subscribing does not start the stream: call start_streaming() for that. Each
        subscriber gets its own queue, so every subscriber sees every message.
        \"\"\"
        await self.subscribe_stream(callback)  # type: ignore[arg-type]"""
        )

    spec = cap.update
    if spec is not None:
        out.append(
            f"""    async def on_{spec.name}(
        self, callback: Callable[[{spec.payload.__name__}, None], Awaitable[None]]
    ) -> None:
        \"\"\"Subscribe to broadcast {spec.name} updates.\"\"\"
        await self.subscribe_updates(callback)  # type: ignore[arg-type]"""
        )
    return out


def _client(contract: EquipmentContract, cap: Capability) -> str:
    name = client_class(contract.name, cap.name)
    cap_const = cap.name.upper()

    parts = [
        f"class {name}(CapabilityClient):",
        f'    """{cap.doc or f"Client for {contract.name}.{cap.name}."}"""',
        "",
        "    def __init__(",
        "        self,",
        "        channel: AbstractChannel,",
        "        exchange: AbstractExchange,",
        "        *,",
        "        timeout: float = 10.0,",
        "        name: str | None = None,",
        "    ) -> None:",
        "        super().__init__(",
        f"            {cap_const}, {contract.name!r},",
        "            channel=channel, exchange=exchange, timeout=timeout, name=name,",
        "        )",
        "",
    ]

    for op in cap.ops:
        parts.append(_op_method(cap, op))
        parts.append("")

    parts.extend(m + "\n" for m in _stream_methods(cap))

    parts.append(f"    async def get_state(self) -> {cap.state.__name__}:  # type: ignore[override]")
    parts.append(f'        """The server\'s current state, typed."""')
    parts.append("        return await super().get_state()  # type: ignore[return-value]")
    parts.append("")
    return "\n".join(parts)


def _aggregate(contract: EquipmentContract) -> str:
    """One object holding every capability client for a node, so callers write
    `rheed.camera.image()` instead of assembling four clients by hand."""
    name = f"{pascal(contract.name)}Client"
    lines = [
        f"class {name}:",
        f'    """Every capability of the {contract.name} node, in one object.',
        "",
        "    async with RheedClient.connect(channel, exchange) as rheed:",
        "        meta, frame = await rheed.camera.image()",
        '    """',
        "",
        "    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange,",
        "                 *, timeout: float = 10.0) -> None:",
    ]
    for cap in contract.capabilities:
        cls = client_class(contract.name, cap.name)
        lines.append(f"        self.{cap.name} = {cls}(channel, exchange, timeout=timeout)")
    lines += [
        "",
        "    @property",
        "    def capabilities(self) -> dict:",
        "        return {",
    ]
    for cap in contract.capabilities:
        lines.append(f"            {cap.name!r}: self.{cap.name},")
    lines += [
        "        }",
        "",
        "    async def start(self) -> None:",
        "        for client in self.capabilities.values():",
        "            await client.start()",
        "",
        "    async def stop(self) -> None:",
        "        for client in self.capabilities.values():",
        "            await client.stop()",
        "",
        "    @classmethod",
        "    def connect(cls, channel: AbstractChannel, exchange: AbstractExchange,",
        "                *, timeout: float = 10.0) -> '_ConnectCtx':",
        "        return _ConnectCtx(cls(channel, exchange, timeout=timeout))",
        "",
        "",
        "class _ConnectCtx:",
        "    def __init__(self, client) -> None:",
        "        self._client = client",
        "",
        "    async def __aenter__(self):",
        "        await self._client.start()",
        "        return self._client",
        "",
        "    async def __aexit__(self, *exc) -> None:",
        "        await self._client.stop()",
        "",
    ]
    return "\n".join(lines)


def emit(contract: EquipmentContract) -> str:
    module = contract.name if contract.name != "detection" else "detection"
    cap_names = sorted({cap.name.upper() for cap in contract.capabilities})

    out = [banner()]
    out.append(
        HEADER.format(
            equipment=contract.name,
            module=module,
            caps_import=", ".join(cap_names),
            model_imports=import_block(all_models(contract)),
        )
    )
    for cap in contract.capabilities:
        out.append(_client(contract, cap))
        out.append("")
    out.append(_aggregate(contract))
    return "\n".join(out)
