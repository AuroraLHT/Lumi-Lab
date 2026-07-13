"""Codecs. Tier T0: no broker.

The binary path is the one that matters: a frame must not go through JSON.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import BaseModel

from lumi.base.mq.codec import CodecError, decode, encode
from lumi.contracts import Codec
from lumi.contracts.payloads.camera import ImageMeta
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.storage import StorageRequest


def test_json_round_trip():
    req = StorageRequest(project_name="growth-42", save_frame=True, force_rewrite=True)
    body, extra = encode(req, Codec.JSON)
    assert extra == {}
    out, payload = decode(StorageRequest, Codec.JSON, body, {})
    assert out == req
    assert payload is None


def test_empty_body_decodes_as_no_arguments():
    """`image` takes no arguments and the client sends b"". model_validate_json
    chokes on an empty buffer, so this is a real edge, not a hypothetical."""
    out, _ = decode(Empty, Codec.JSON, b"", {})
    assert isinstance(out, Empty)


def test_npy_keeps_the_array_out_of_json():
    frame = np.arange(720 * 540, dtype=np.uint8).reshape(540, 720)
    meta = ImageMeta(time=1.0, uuid="u", time_stamp="ts", dtype="uint8", shape=[540, 720])

    body, extra = encode(meta, Codec.NPY, frame)

    # The metadata rides in the headers; the body is the buffer and nothing else.
    assert "meta" in extra
    assert b"growth" not in body
    assert len(body) >= frame.nbytes  # not base64-inflated by 33%

    out_meta, out_arr = decode(ImageMeta, Codec.NPY, body, extra)
    assert out_meta == meta
    np.testing.assert_array_equal(out_arr, frame)


def test_npy_preserves_dtype_and_shape():
    for arr in (
        np.zeros((3, 4), dtype=np.float32),
        np.ones((2, 2, 3), dtype=np.uint8),
        np.array([1.5, 2.5], dtype=np.float64),
    ):
        meta = ImageMeta(time=0.0, uuid="u", time_stamp="ts")
        body, extra = encode(meta, Codec.NPY, arr)
        _, out = decode(ImageMeta, Codec.NPY, body, extra)
        assert out.dtype == arr.dtype
        assert out.shape == arr.shape
        np.testing.assert_array_equal(out, arr)


def test_raw_round_trip():
    from lumi.contracts.payloads.camera import VideoFragmentMeta

    fragment = b"\x00\x00\x00\x01fake-h264-nal-unit"
    meta = VideoFragmentMeta(fragment_idx=7)
    body, extra = encode(meta, Codec.RAW, fragment)
    assert body == fragment

    out_meta, out = decode(VideoFragmentMeta, Codec.RAW, body, extra)
    assert out_meta.fragment_idx == 7
    assert out == fragment


def test_npy_rejects_a_non_array_payload():
    meta = ImageMeta(time=0.0, uuid="u", time_stamp="ts")
    with pytest.raises(CodecError, match="ndarray"):
        encode(meta, Codec.NPY, b"not an array")


def test_json_rejects_a_binary_payload():
    with pytest.raises(CodecError, match="no separate binary payload"):
        encode(Empty(), Codec.JSON, np.zeros(3))


def test_binary_decode_without_meta_header_is_an_error():
    with pytest.raises(CodecError, match="meta"):
        decode(ImageMeta, Codec.NPY, b"", {})


def test_pickle_is_never_allowed():
    """allow_pickle=False on both sides. A frame arriving from the bus must not be
    able to execute code."""
    import io

    buf = io.BytesIO()
    np.save(buf, np.array([{"evil": True}], dtype=object), allow_pickle=True)
    meta = ImageMeta(time=0.0, uuid="u", time_stamp="ts")
    with pytest.raises(ValueError):
        decode(ImageMeta, Codec.NPY, buf.getvalue(), {"meta": meta.model_dump_json()})
