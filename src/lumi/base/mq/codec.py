"""Putting a payload on the wire, and taking it off again.

The awkward case is binary. A frame is a 700x540 uint8 array; a video fragment is
an H.264 buffer. Neither belongs inside JSON -- the old detection path base64'd a
full pattern and every instance mask into a JSON body *per frame*, which is the
single most expensive thing in the current system.

So for NPY and RAW, the pydantic model describes the *metadata* and rides in the
AMQP headers, while the body stays an opaque buffer. Callers get `(meta, payload)`.
For JSON the body is the model and `payload` is None.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from pydantic import BaseModel

from lumi.contracts import Codec

#: NPZ carries several named arrays at once -- a detection needs the pattern plus one
#: mask per instance, and np.save holds exactly one array.
Arrays = dict[str, np.ndarray]
Payload = np.ndarray | bytes | Arrays | None


class CodecError(Exception):
    """A body could not be encoded or decoded as its contract declares."""


def encode(model: BaseModel, codec: Codec, payload: Payload = None) -> tuple[bytes, dict[str, Any]]:
    """Model (+ optional binary payload) -> (body, extra headers)."""
    if codec is Codec.JSON:
        if payload is not None:
            raise CodecError(f"{codec} takes no separate binary payload")
        return model.model_dump_json().encode(), {}

    if codec is Codec.NPY:
        if not isinstance(payload, np.ndarray):
            raise CodecError(f"{codec} requires an ndarray payload, got {type(payload).__name__}")
        buf = io.BytesIO()
        np.save(buf, payload, allow_pickle=False)
        return buf.getvalue(), {"meta": model.model_dump_json()}

    if codec is Codec.NPZ:
        if not isinstance(payload, dict):
            raise CodecError(f"{codec} requires a dict of ndarrays, got {type(payload).__name__}")
        buf = io.BytesIO()
        np.savez_compressed(buf, **payload)
        return buf.getvalue(), {"meta": model.model_dump_json()}

    if codec is Codec.RAW:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise CodecError(f"{codec} requires a bytes payload, got {type(payload).__name__}")
        return bytes(payload), {"meta": model.model_dump_json()}

    raise CodecError(f"unknown codec {codec!r}")


def decode(
    model: type[BaseModel], codec: Codec, body: bytes, headers: dict[str, Any]
) -> tuple[BaseModel, Payload]:
    """(body, headers) -> (model, payload). Raises pydantic.ValidationError on a bad body."""
    if codec is Codec.JSON:
        # An empty body is how "no arguments" arrives; model_validate_json chokes on b"".
        return model.model_validate_json(body or b"{}"), None

    meta_json = headers.get("meta")
    if meta_json is None:
        raise CodecError(f"{codec} message is missing its 'meta' header")
    meta = model.model_validate_json(meta_json)

    if codec is Codec.NPY:
        return meta, np.load(io.BytesIO(body), allow_pickle=False)
    if codec is Codec.NPZ:
        with np.load(io.BytesIO(body), allow_pickle=False) as archive:
            return meta, {name: archive[name] for name in archive.files}
    if codec is Codec.RAW:
        return meta, body

    raise CodecError(f"unknown codec {codec!r}")
