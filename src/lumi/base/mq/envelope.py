"""The AMQP header envelope.

One envelope for every message on the bus. The old code had eight header TypedDicts
whose fields were spelled out by hand at ~50 call sites -- `request_type` and
`response_type` were passed the same literal every single time, and `succ`,
`error_type`, `error_message` were written out even when empty.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any


class MessageType(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    STREAM = "stream"
    UPDATE = "update"
    STATE = "state"
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def base(message_type: MessageType, source: str) -> dict[str, Any]:
    return {
        "message_type": str(message_type),
        "message_timestamp": _now(),
        "message_source": source,
    }


def request(
    source: str, op: str, codec: str, meta: str | None = None, actor: str | None = None
) -> dict[str, Any]:
    h = base(MessageType.REQUEST, source)
    h["op"] = op
    h["codec"] = codec
    if meta is not None:
        h["meta"] = meta
    if actor is not None:
        h["actor"] = actor
    return h


def response(
    source: str,
    op: str,
    codec: str,
    *,
    succ: bool = True,
    error_type: str = "",
    error_message: str = "",
    meta: str | None = None,
) -> dict[str, Any]:
    h = base(MessageType.RESPONSE, source)
    h.update(op=op, codec=codec, succ=succ, error_type=error_type, error_message=error_message)
    if meta is not None:
        h["meta"] = meta
    return h


def stream(source: str, name: str, codec: str, meta: str | None = None) -> dict[str, Any]:
    h = base(MessageType.STREAM, source)
    h.update(stream_type=name, codec=codec)
    if meta is not None:
        h["meta"] = meta
    return h


def update(source: str, name: str, codec: str, meta: str | None = None) -> dict[str, Any]:
    h = base(MessageType.UPDATE, source)
    h.update(update_type=name, codec=codec)
    if meta is not None:
        h["meta"] = meta
    return h


def state(source: str) -> dict[str, Any]:
    return base(MessageType.STATE, source)


def control_request(source: str, verb: str) -> dict[str, Any]:
    h = base(MessageType.CONTROL_REQUEST, source)
    h["control_request_type"] = verb
    return h


def control_response(
    source: str, verb: str, *, succ: bool = True, error_type: str = "", error_message: str = ""
) -> dict[str, Any]:
    h = base(MessageType.CONTROL_RESPONSE, source)
    h.update(
        control_request_type=verb,
        succ=succ,
        error_type=error_type,
        error_message=error_message,
    )
    return h
