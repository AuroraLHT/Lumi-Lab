"""Turn a binary op's payload into something an LLM can actually look at.

Most ops answer in JSON and the MCP server just forwards it. A few do not: their
`response_codec` is NPY or RAW, so the model describes *headers* and the pixels
travel beside it (see contracts/spec.py, Codec; `CapabilityClient.call` hands
back `(meta, payload)`, with the payload already decoded -- an ndarray for NPY,
bytes for RAW). Of those, `rheed.camera.image` is exposed as a tool -- and a tool
that answers a request for a frame with `{"dtype": "uint16", "shape": [540, 720]}`
and nothing else has told the agent the size of the picture instead of showing it
the picture.

So: encode the frame as a PNG and hand it back as MCP ImageContent, which is the
wire form a host turns into an actual image block for the model.

The one judgement call is contrast. Frames come off these cameras as uint16
holding 12-bit counts, so the sensor's full-scale is 4095 while the dtype's is
65535; stretched against the dtype the whole image is black, and truncated to
uint8 it is a posterised mess. Neither is a picture of anything. This scales each
frame by its own min and max, which is what makes RHEED streaks legible, and
reports the real numbers in the accompanying text so that "the spot is brighter
now" is a claim the agent makes from the readings rather than from the rendering.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

import numpy as np
from mcp import types

log = logging.getLogger(__name__)

#: Longest edge, in pixels, of the image handed to the model. Not a display
#: choice -- every pixel is base64 in a JSON-RPC message and then tokens in a
#: context window, and the vision encoder downsamples anything larger anyway. A
#: 720-wide RHEED frame is untouched; a 4k chamber camera is not sent whole.
MAX_DIMENSION = 1024

#: Bodies that are already an encoded image (Codec.RAW carrying a JPEG, as the
#: chamber's viewing stream does) are passed through rather than decoded and
#: re-encoded, which would cost a round of generation loss for nothing.
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def frame_to_image(array: np.ndarray) -> tuple[types.ImageContent, str]:
    """`(image, note)` for one frame. Raises if the array is not an image.

    The note goes back beside the image: what the pixels really were before
    anything here touched them.
    """
    from PIL import Image

    # (H, W, 1) is a greyscale frame that happens to carry its channel axis.
    if array.ndim == 3 and array.shape[2] == 1:
        array = array[:, :, 0]
    if array.ndim not in (2, 3) or (array.ndim == 3 and array.shape[2] not in (3, 4)):
        raise ValueError(f"array of shape {array.shape} is not an image")

    low, high = float(array.min()), float(array.max())
    scaled, rescaled = _to_uint8(array, low, high)

    image = Image.fromarray(scaled)
    original = image.size
    if max(original) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(original)
        image = image.resize((max(1, round(original[0] * ratio)), max(1, round(original[1] * ratio))))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    note = f"frame: {tuple(array.shape)} {array.dtype}, pixel values {low:g}..{high:g}"
    if rescaled:
        note += " (rendered with those two as black and white, so brightness here is relative)"
    if image.size != original:
        note += f"; shown downscaled to {image.size[0]}x{image.size[1]}"

    return (
        types.ImageContent(
            type="image",
            data=base64.b64encode(buffer.getvalue()).decode("ascii"),
            mime_type="image/png",
        ),
        note,
    )


def _to_uint8(array: np.ndarray, low: float, high: float) -> tuple[np.ndarray, bool]:
    """`(uint8 array, whether the values were stretched)`."""
    if array.dtype == np.uint8:
        return array, False
    if high <= low:
        # A uniform frame -- a shuttered camera, or a dead sensor. Stretching it
        # would turn sensor noise into a vivid pattern out of nothing.
        return np.zeros(array.shape, dtype=np.uint8), False
    return (((array.astype(np.float32) - low) / (high - low)) * 255).astype(np.uint8), True


def binary_content(payload: Any, codec: str) -> tuple[list[types.ContentBlock], str]:
    """What to send for an op whose body is not JSON: `(content, note)`.

    Anything that is not a renderable image degrades to a sentence saying what
    came back instead. The previous behaviour -- dropping the payload and
    returning the headers alone -- is the one option not on the table, because it
    is indistinguishable from an op that genuinely had nothing to return.
    """
    if payload is None:
        return [], f"{codec} payload was empty"

    if isinstance(payload, np.ndarray):
        try:
            image, note = frame_to_image(payload)
            return [image], note
        except Exception as exc:
            log.warning("could not render a %s payload as an image: %s", codec, exc)
            return [], f"array {tuple(payload.shape)} {payload.dtype}, not an image ({exc})"

    if isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
        if not raw:
            return [], f"{codec} payload was empty"
        for magic, mime in _MAGIC:
            if raw.startswith(magic):
                return [
                    types.ImageContent(
                        type="image", data=base64.b64encode(raw).decode("ascii"), mime_type=mime
                    )
                ], f"{mime}, {len(raw)} bytes"
        return [], f"{len(raw)} bytes of opaque {codec} body, not an image"

    if isinstance(payload, dict):  # Codec.NPZ: named arrays, no single picture to show
        return [], f"{codec} payload with arrays: {', '.join(sorted(payload))}"

    return [], f"{codec} payload of type {type(payload).__name__}, not renderable"
