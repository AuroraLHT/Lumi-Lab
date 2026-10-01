"""A camera frame turned into the two files a snapshot keeps.

The frame itself, as `.npy`, is the record: lossless, the camera's own dtype, so a
RHEED still can be measured later the same way a recorded frame can. The JPEG is for
looking at -- a person in the UI, an agent through MCP -- and is never the thing
analysed.

Contrast follows `lumi.mcp.frames`: 8-bit frames are encoded as they are; anything
else (the RHEED camera's 12-bit counts in uint16) is stretched between the frame's
own min and max, because against the dtype's full scale the picture is black. The
min and max are kept beside the snapshot, so brightness stays a reading rather than
an impression of the rendering.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np

JPEG_QUALITY = 90


@dataclass(frozen=True)
class EncodedFrame:
    raw: bytes
    jpeg: bytes
    width: int
    height: int
    channels: int
    dtype: str
    pixel_min: float
    pixel_max: float


def encode_frame(frame: np.ndarray, *, rgb: bool = True) -> EncodedFrame:
    """`.npy` and JPEG bytes for one frame. `rgb`: a 3-channel frame is in RGB order,
    as both cameras hand them out (cv2 wants BGR, so it is swapped for the JPEG only)."""
    if frame.ndim == 3 and frame.shape[2] == 1:
        frame = frame[:, :, 0]
    if frame.ndim not in (2, 3) or (frame.ndim == 3 and frame.shape[2] not in (3, 4)) or frame.size == 0:
        raise ValueError(f"a frame of shape {frame.shape} is not an image")

    buffer = io.BytesIO()
    np.save(buffer, frame, allow_pickle=False)

    low, high = float(frame.min()), float(frame.max())
    if frame.dtype == np.uint8:
        pixels = frame
    elif high > low:
        pixels = ((frame.astype(np.float32) - low) / (high - low) * 255.0).astype(np.uint8)
    else:
        # A uniform frame (a shuttered camera): stretching would invent a pattern.
        pixels = np.zeros(frame.shape, dtype=np.uint8)
    if pixels.ndim == 3:
        pixels = pixels[:, :, :3]
        if rgb:
            pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    ok, jpeg = cv2.imencode(".jpg", pixels, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("cv2.imencode rejected the frame")

    return EncodedFrame(
        raw=buffer.getvalue(), jpeg=jpeg.tobytes(),
        width=int(frame.shape[1]), height=int(frame.shape[0]),
        channels=1 if frame.ndim == 2 else int(frame.shape[2]),
        dtype=str(frame.dtype), pixel_min=low, pixel_max=high,
    )


def decode_raw(data: bytes) -> np.ndarray:
    return np.load(io.BytesIO(data), allow_pickle=False)
