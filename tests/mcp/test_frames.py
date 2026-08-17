"""A camera tool has to return the picture, not a description of the picture.

Tier T0: pure arrays, no broker.

The bug these pin: `_on_call_tool` unpacked a binary op's `(headers, payload)`
and kept only the headers, so `rheed.camera.image` came back as
`{"dtype": "uint16", "shape": [540, 720]}` with the frame discarded -- and with
nothing in the reply to suggest anything was missing.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from PIL import Image

from lumi.mcp.frames import MAX_DIMENSION, binary_content, frame_to_image


def decode(image) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image.data)))


def test_a_frame_comes_back_as_an_image():
    frame = np.zeros((540, 720), dtype=np.uint8)
    frame[100:200, 300:400] = 255

    image, note = frame_to_image(frame)

    assert image.type == "image"
    assert image.mime_type == "image/png"
    decoded = decode(image)
    assert decoded.size == (720, 540)
    # The bright patch is still where it was put -- PIL indexes (x, y).
    assert decoded.getpixel((350, 150)) == 255
    assert decoded.getpixel((10, 10)) == 0
    assert "uint8" in note


def test_a_12_bit_frame_is_stretched_not_truncated():
    """The real case. Counts sit in 0..4095 inside a uint16, so scaling against the
    dtype gives a black rectangle and casting to uint8 wraps the highlights."""
    frame = np.full((64, 64), 200, dtype=np.uint16)
    frame[0:8, 0:8] = 4095

    image, note = frame_to_image(frame)
    decoded = decode(image)

    assert decoded.getpixel((2, 2)) == 255
    assert decoded.getpixel((40, 40)) == 0
    # And the numbers the agent should reason about are the sensor's, not the render's.
    assert "200..4095" in note
    assert "relative" in note


def test_a_uniform_frame_is_not_turned_into_a_pattern():
    """A shuttered camera must look shuttered; stretching min..max here would
    amplify nothing into a vivid texture."""
    image, note = frame_to_image(np.full((32, 32), 7, dtype=np.uint16))

    assert np.asarray(decode(image)).max() == 0
    assert "relative" not in note


def test_a_colour_frame_survives_as_colour():
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    frame[:, :, 0] = 255

    decoded = decode(frame_to_image(frame)[0])
    assert decoded.mode == "RGB"
    assert decoded.getpixel((8, 8)) == (255, 0, 0)


def test_a_single_channel_axis_is_squeezed():
    decoded = decode(frame_to_image(np.zeros((8, 12, 1), dtype=np.uint8))[0])
    assert decoded.size == (12, 8)


def test_a_large_frame_is_downscaled_before_it_reaches_the_model():
    """Every pixel becomes base64 in a JSON-RPC message and then context."""
    image, note = frame_to_image(np.zeros((3000, 4000), dtype=np.uint8))

    assert max(decode(image).size) == MAX_DIMENSION
    assert "downscaled" in note


@pytest.mark.parametrize("dtype", [np.uint16, np.int32, np.float32])
def test_every_frame_dtype_the_cameras_emit_renders(dtype):
    frame = (np.arange(64 * 64).reshape(64, 64) % 1000).astype(dtype)
    image, _ = frame_to_image(frame)
    assert np.asarray(decode(image)).max() == 255


# --- what binary_content does with everything else --------------------------


def test_an_ndarray_payload_is_rendered():
    content, note = binary_content(np.zeros((8, 8), dtype=np.uint8), "npy")
    assert len(content) == 1 and content[0].type == "image"
    assert "8, 8" in note


def test_an_array_that_is_not_an_image_says_so_instead_of_vanishing():
    content, note = binary_content(np.arange(10), "npy")

    assert content == []
    assert "not an image" in note
    assert "(10,)" in note


def test_an_already_encoded_jpeg_is_passed_through_untouched():
    """Codec.RAW carrying a JFIF buffer -- decoding and re-encoding it would cost a
    generation of loss for nothing."""
    buffer = io.BytesIO()
    Image.fromarray(np.zeros((8, 8), dtype=np.uint8)).save(buffer, format="JPEG")

    content, note = binary_content(buffer.getvalue(), "raw")
    assert len(content) == 1
    assert content[0].mime_type == "image/jpeg"
    assert base64.b64decode(content[0].data) == buffer.getvalue()
    assert "image/jpeg" in note


def test_opaque_bytes_are_described_not_dropped():
    content, note = binary_content(b"\x00\x01\x02\x03", "raw")
    assert content == []
    assert "4 bytes" in note


def test_a_missing_payload_is_reported():
    for payload in (None, b""):
        content, note = binary_content(payload, "npy")
        assert content == []
        assert "empty" in note


def test_named_arrays_are_listed():
    content, note = binary_content({"stft": np.zeros(4), "freq": np.zeros(4)}, "npz")
    assert content == []
    assert "freq, stft" in note
