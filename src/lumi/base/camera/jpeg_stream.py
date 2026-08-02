"""MJPEG encoding for a camera's live stream.

The counterpart to video_stream.VideoCompressor, for capabilities that stream to be
*looked at* rather than decoded into arrays. It consumes one of the camera's fan-out
queues and produces `(jpeg_bytes, header)` -- deliberately the same shape
VideoCompressor puts on `.fragments`, so the handler side is the same eight lines.

Why a second encoder rather than pointing the chamber at the H.264 one: see the
comment on CHAMBER's camera stream in lumi/contracts/chamber.py. In short, every
stage of this pipeline drops frames on purpose, and JPEG frames are independent
where H.264 fragments are not.

Encoding happens here, on a worker thread, rather than in the handler's `next()`.
Two reasons. `cv2.imencode` of a 640x480 frame is a few milliseconds of pure CPU with
the GIL held, and `next()` runs on the node's event loop -- at 25fps that is a
percentage of the loop stolen from every other capability on the node. And a handler
encodes per *subscriber*, while a thread encodes once for all of them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class JpegEncoderConfig:
    #: libjpeg quality, 0-100. 80 is the usual "no visible artefacts on a photo"
    #: point and lands a 640x480 frame at roughly 30-50 KB.
    quality: int = 80
    queue_size: int = 4
    #: How long to wait on the camera queue before re-checking the stop event. Only
    #: bounds shutdown latency -- it is not a poll interval, the get() blocks.
    idle_time: float = 0.1
    #: Full-scale value of the incoming pixels, used to scale non-uint8 frames down to
    #: the 8 bits JPEG can carry (a 12-bit sensor reads 0-4095). Fixed rather than
    #: auto-ranged per frame on purpose: rescaling by each frame's own maximum makes
    #: the picture pulse as the scene brightness changes, which reads as a fault.
    max_intensity: int = 255


class JpegEncoder(threading.Thread):
    """Encodes frames from a camera fan-out queue as JPEG.

    Health is reported the same way VideoCompressor reports it, and for the same
    reason: `is_streaming` on the wire is a server-owned transport flag that stays
    True over a producer thread that died twenty minutes ago, so a frame count that
    stops advancing is the only signal that tells the two apart.
    """

    def __init__(
        self,
        camera,
        camera_queue: "queue.Queue",
        config: JpegEncoderConfig,
        frame_processing: Callable | None = None,
        name: str | int = "",
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self.camera = camera
        self.camera_queue = camera_queue
        self.config = config
        self.frame_processing = frame_processing
        self.frames: "queue.Queue" = queue.Queue(maxsize=config.queue_size)
        self._stop_event = threading.Event()

        self.error: str | None = None
        self.n_failures = 0
        self.n_encoded = 0
        self.n_dropped = 0
        self.last_frame_at: float | None = None

    # --- encoding ---------------------------------------------------------

    def encode(self, frame: np.ndarray) -> tuple[bytes, int, int, int]:
        """Frame -> (jpeg bytes, width, height, channels). Raises on a bad frame."""
        if frame.ndim == 3 and frame.shape[2] == 3:
            # The cameras hand out RGB (both SimCamera and WebCamera run a
            # COLOR_BGR2RGB on the way in), and cv2.imencode expects BGR. Skipping
            # this does not fail -- it silently swaps the red and blue channels,
            # which on a chamber view looks like a plasma colour change rather than
            # a bug.
            payload = cv2.cvtColor(self._to_uint8(frame), cv2.COLOR_RGB2BGR)
            channels = 3
        elif frame.ndim == 2:
            # Encoded single-channel rather than widened to RGB: a third of the
            # bytes, and the browser paints a greyscale JPEG identically.
            payload = self._to_uint8(frame)
            channels = 1
        else:
            raise ValueError(f"cannot encode a frame of shape {frame.shape}")

        ok, buf = cv2.imencode(
            ".jpg", payload, [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.quality)]
        )
        if not ok:
            raise RuntimeError("cv2.imencode rejected the frame")
        height, width = payload.shape[:2]
        return buf.tobytes(), width, height, channels

    def _to_uint8(self, frame: np.ndarray) -> np.ndarray:
        if frame.dtype == np.uint8:
            return frame
        scale = 255.0 / max(self.config.max_intensity, 1)
        return np.clip(frame.astype(np.float32) * scale, 0, 255).astype(np.uint8)

    # --- thread -----------------------------------------------------------

    def run(self) -> None:
        """Encode until stopped, surviving a bad frame.

        The loop is wrapped for the same reason VideoCompressor's is: an exception
        escaping run() kills the thread while the capability goes on advertising a
        healthy stream, and the only way back is a process restart. A malformed
        frame should cost that frame.
        """
        while not self._stop_event.is_set():
            try:
                content = self.camera_queue.get(timeout=self.config.idle_time)
            except queue.Empty:
                continue

            try:
                frame, header = content
                if self.frame_processing is not None:
                    frame = self.frame_processing(frame, header)
                blob, width, height, channels = self.encode(frame)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.n_failures += 1
                # Once per failure would be once per frame on a persistently bad
                # source -- 25 tracebacks a second into the node log.
                if self.n_failures == 1 or self.n_failures % 100 == 0:
                    log.exception("JPEG encode failed (%s), dropping the frame", self.error)
                continue

            self._put((blob, {**(header or {}), "width": width,
                              "height": height, "channels": channels}))

    def _put(self, content) -> None:
        """Never block the encoder on a slow consumer; drop the stale frame instead."""
        try:
            self.frames.put_nowait(content)
        except queue.Full:
            try:
                self.frames.get_nowait()
                self.n_dropped += 1
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(content)
            except queue.Full:
                return
        self.n_encoded += 1
        self.last_frame_at = time.time()
        self.error = None

    def is_producing(self, stale_after: float = 5.0) -> bool:
        """True when the thread is alive *and* recently published.

        `is_alive()` alone is not enough -- a thread parked on a camera queue that
        stopped filling is alive and useless.
        """
        if not self.is_alive():
            return False
        if self.last_frame_at is None:
            return True  # started, no frame grabbed yet
        return (time.time() - self.last_frame_at) < stale_after

    def clear(self) -> None:
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        log.info("JPEG encoder thread receives a stop signal")
        self._stop_event.set()
        self.clear()
