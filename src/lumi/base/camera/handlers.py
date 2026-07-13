"""Camera handlers. Shared by the RHEED node and the chamber node.

One copy. Before this, `CameraClient`, `LiveCameraClient`, `VideoFragmentsClient` and
`LiveVideoFragmentsClient` were defined *byte-for-byte identically* in both
client/rheed.py and client/pascal.py, and the server halves were re-exported through
two more modules.

A handler is plain domain code. It has no idea it is on a message bus: no routing
keys, no AMQP headers, no codecs, no envelope construction.
"""

from __future__ import annotations

import logging
import queue

import numpy as np

from lumi.contracts.payloads.camera import (
    CameraConfig,
    CameraState,
    FragmentIndex,
    FragmentsSize,
    ImageMeta,
    InitialFragmentsMeta,
    VideoFragmentMeta,
    VideoState,
)
from lumi.contracts.payloads.common import Ack, Empty

log = logging.getLogger(__name__)


class CameraHandler:
    """Serves the `camera` capability: pull a frame, or subscribe to the feed.

    That these are one capability rather than two is the point of Kind.DUPLEX -- the
    old code ran a `camera` server and a separate `live_camera` server, on two
    routing-key sets, for one physical camera.
    """

    def __init__(self, camera, stream_queue: "queue.Queue | None" = None) -> None:
        self.camera = camera
        self.stream_queue = stream_queue

    async def image(self, req: Empty) -> tuple[ImageMeta, np.ndarray]:
        frame, header = self.camera.get_frame()
        if frame is None:
            raise RuntimeError("the camera has no frame available")
        return self._meta(frame, header), frame

    async def get_camera_config(self, req: Empty) -> CameraConfig:
        return CameraConfig(**_known_fields(CameraConfig, self.camera.get_camera_config()))

    async def update_camera_config(self, req: CameraConfig) -> Ack:
        # exclude_none, so a partial update leaves the omitted knobs alone rather than
        # blanking them -- which is what the old dict-based version relied on.
        changes = req.model_dump(exclude_none=True)
        if not changes:
            return Ack()
        succ, err = self.camera.update_camera_config(**changes)
        if not succ:
            raise RuntimeError(err or "the camera rejected the config update")
        log.info("camera config updated: %s", changes)
        return Ack()

    async def next(self) -> tuple[ImageMeta, np.ndarray] | None:
        if self.stream_queue is None:
            return None
        try:
            frame, header = self.stream_queue.get_nowait()
        except queue.Empty:
            return None
        return self._meta(frame, header), frame

    def _meta(self, frame: np.ndarray, header: dict | None) -> ImageMeta:
        header = header or {}
        return ImageMeta(
            time=float(header.get("time", 0.0)),
            uuid=str(header.get("uuid", "")),
            time_stamp=str(header.get("time_stamp", "")),
            dtype=str(frame.dtype),
            shape=list(frame.shape),
        )

    def state(self) -> CameraState:
        return CameraState(
            is_running=self.camera.is_alive() if hasattr(self.camera, "is_alive") else True,
            frame_dims=list(self.camera.frame_dims),
            fps=getattr(self.camera.config, "fps", None),
        )


class VideoHandler:
    """Serves the `video` capability: H.264 fragments for browser playback."""

    def __init__(self, compressor, stream_queue: "queue.Queue | None" = None) -> None:
        self.compressor = compressor
        self.stream_queue = stream_queue

    async def initial_fragments(self, req: Empty) -> tuple[InitialFragmentsMeta, bytes]:
        content = self.compressor.get_startup_fragments()
        blobs = [fragment for fragment, _ in content]
        headers = [dict(h or {}) for _, h in content]
        return (
            InitialFragmentsMeta(
                count=len(blobs),
                sizes=[len(b) for b in blobs],
                headers=headers,
            ),
            b"".join(blobs),
        )

    async def initial_fragments_size(self, req: Empty) -> FragmentsSize:
        return FragmentsSize(size=self.compressor.number_of_startup_fragments())

    async def video_fragment(self, req: FragmentIndex) -> tuple[VideoFragmentMeta, bytes]:
        fragment, header = self.compressor.get_history_fragment(req.fragment_idx)
        header = header or {}
        return (
            VideoFragmentMeta(
                fragment_idx=req.fragment_idx,
                time=header.get("time"),
                time_stamp=header.get("time_stamp"),
            ),
            fragment,
        )

    async def next(self) -> tuple[VideoFragmentMeta, bytes] | None:
        if self.stream_queue is None:
            return None
        try:
            item = self.stream_queue.get_nowait()
        except queue.Empty:
            return None
        fragment, header = item if isinstance(item, tuple) else (item, {})
        header = header or {}
        return (
            VideoFragmentMeta(time=header.get("time"), time_stamp=header.get("time_stamp")),
            fragment,
        )

    def state(self) -> VideoState:
        cfg = self.compressor.config
        return VideoState(
            is_running=self.compressor.is_alive() if hasattr(self.compressor, "is_alive") else True,
            fragment_duration=getattr(cfg, "frames_per_keyframe", 0) / max(getattr(cfg, "fps", 1), 1),
        )


def _known_fields(model: type, raw: dict) -> dict:
    """Keep only the keys the model declares. A camera driver may report knobs the
    contract does not model, and that should not be an error."""
    return {k: v for k, v in (raw or {}).items() if k in model.model_fields}
