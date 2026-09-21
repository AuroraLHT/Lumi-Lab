"""Fiducial markers on a camera feed: where they are, and what is under them.

Three parts, none of which knows about the message bus:

  `FiducialStore`        the operator's markers, thread-safe, persisted to a JSON file
  `measure`              one marker on one frame -> intensity statistics
  `FiducialStatsWorker`  consumes a camera fan-out queue and measures every marker on
                         every frame, keeping a bounded trace per marker

The worker is a thread rather than work done in the handler's `next()` for the same
reason the JPEG encoder is: `next()` runs on the node's event loop, and the trace has
to keep accumulating when nobody is subscribed to the stream -- a mask sweep is
usually reviewed after the fact, through `marker_history`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from pydantic import TypeAdapter, ValidationError

from lumi.contracts.payloads.fiducial import (
    CrossShape,
    FiducialMarker,
    MarkerStats,
    PolyShape,
    RectShape,
    Shape,
)

log = logging.getLogger(__name__)

#: Rec. 601 luma. The cameras hand out RGB.
_LUMA = np.array([0.299, 0.587, 0.114])

_EMPTY = MarkerStats(n_pixels=0)


# --- geometry ---------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """A marker rasterised for one frame size: the window it covers, and which pixels
    inside the window count. `mask` is None for the whole window (a rectangle)."""

    y0: int
    y1: int
    x0: int
    x1: int
    mask: np.ndarray | None

    @property
    def is_empty(self) -> bool:
        return self.y1 <= self.y0 or self.x1 <= self.x0


def rasterise(shape: Shape, height: int, width: int) -> Region:
    """The pixels of `shape` that fall inside a `height` x `width` frame.

    The window is clipped to the frame and the mask drawn in window coordinates, so a
    marker that hangs off the edge measures the part that is on the frame rather than
    failing.
    """
    if isinstance(shape, RectShape):
        x0, y0 = round(shape.x), round(shape.y)
        x1, y1 = round(shape.x + shape.width), round(shape.y + shape.height)
        # A sliver narrower than a pixel still measures the pixel it rounds to.
        x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
        return _clipped(x0, y0, x1, y1, height, width, draw=None)

    if isinstance(shape, CrossShape):
        cx, cy, arm = round(shape.x), round(shape.y), max(round(shape.size), 1)
        pad = shape.thickness // 2 + 1
        x0, y0, x1, y1 = cx - arm - pad, cy - arm - pad, cx + arm + pad + 1, cy + arm + pad + 1

        def draw(mask: np.ndarray, ox: int, oy: int) -> None:
            for a, b in (((cx - arm, cy), (cx + arm, cy)), ((cx, cy - arm), (cx, cy + arm))):
                cv2.line(mask, (a[0] - ox, a[1] - oy), (b[0] - ox, b[1] - oy),
                         255, shape.thickness)

        return _clipped(x0, y0, x1, y1, height, width, draw=draw)

    if isinstance(shape, PolyShape):
        pts = np.array([[round(p.x), round(p.y)] for p in shape.points], dtype=np.int32)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0) + 1

        def draw(mask: np.ndarray, ox: int, oy: int) -> None:
            cv2.fillPoly(mask, [pts - np.array([ox, oy], dtype=np.int32)], 255)

        return _clipped(int(x0), int(y0), int(x1), int(y1), height, width, draw=draw)

    raise TypeError(f"unknown marker shape {type(shape).__name__}")


def _clipped(x0, y0, x1, y1, height, width, draw) -> Region:
    cx0, cy0 = max(x0, 0), max(y0, 0)
    cx1, cy1 = min(x1, width), min(y1, height)
    if cx1 <= cx0 or cy1 <= cy0:
        return Region(0, 0, 0, 0, None)
    mask = None
    if draw is not None:
        mask = np.zeros((cy1 - cy0, cx1 - cx0), dtype=np.uint8)
        draw(mask, cx0, cy0)
    return Region(cy0, cy1, cx0, cx1, mask)


def luminance(window: np.ndarray) -> np.ndarray:
    """A window of a frame as one channel: greyscale passes through, colour is reduced
    to luma. Only ever called on a marker's window, never the whole frame."""
    if window.ndim == 2:
        return window
    return window[..., :3] @ _LUMA


def measure(frame: np.ndarray, region: Region) -> MarkerStats:
    """Mean / min / max / std over the pixels of `region` on `frame`."""
    if region.is_empty:
        return _EMPTY
    values = luminance(frame[region.y0:region.y1, region.x0:region.x1])
    if region.mask is not None:
        values = values[region.mask > 0]
    if values.size == 0:
        return _EMPTY
    return MarkerStats(
        n_pixels=int(values.size),
        mean=float(values.mean()),
        min=float(values.min()),
        max=float(values.max()),
        std=float(values.std()),
    )


# --- the operator's markers -------------------------------------------------

_MARKERS = TypeAdapter(list[FiducialMarker])


class FiducialStore:
    """The markers, by id. Shared between the handler (event loop) and the stats worker
    (its own thread), so every access is under a lock and the worker reads a
    `snapshot()` rather than the live dict.

    Persisted on every change, because a marker is set up by hand once and is worth
    keeping across a node restart -- unlike the statistics, which are cheap to
    regenerate. `path=None` keeps them in memory only.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._markers: dict[str, FiducialMarker] = {}
        #: Bumps on every change; the worker compares it to know when to re-rasterise.
        self.version = 0
        self._load()

    def set(self, marker: FiducialMarker) -> None:
        with self._lock:
            self._markers[marker.marker_id] = marker
            self.version += 1
            self._save()

    def remove(self, marker_id: str) -> None:
        with self._lock:
            if marker_id not in self._markers:
                raise KeyError(f"no marker {marker_id!r}")
            del self._markers[marker_id]
            self.version += 1
            self._save()

    def get(self, marker_id: str) -> FiducialMarker | None:
        with self._lock:
            return self._markers.get(marker_id)

    def list(self) -> list[FiducialMarker]:
        with self._lock:
            return list(self._markers.values())

    def snapshot(self) -> tuple[int, dict[str, FiducialMarker]]:
        with self._lock:
            return self.version, dict(self._markers)

    # --- persistence ---

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            markers = _MARKERS.validate_python(json.loads(self.path.read_text()))
        except (OSError, ValueError, ValidationError):
            # Do not let a hand-edited or truncated file take the chamber node down, and
            # do not let the next save silently overwrite it either.
            aside = self.path.with_suffix(self.path.suffix + ".corrupt")
            log.exception("could not read the marker file %s; moving it to %s and "
                          "starting with no markers", self.path, aside)
            try:
                os.replace(self.path, aside)
            except OSError:
                pass
            return
        self._markers = {m.marker_id: m for m in markers}
        log.info("loaded %d fiducial marker(s) from %s", len(self._markers), self.path)

    def _save(self) -> None:
        if self.path is None:
            return
        payload = _MARKERS.dump_json(list(self._markers.values()), indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename, so a crash mid-write leaves the previous file intact
            # rather than a truncated one.
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, self.path)
        except OSError:
            # The marker is live in memory either way. Losing persistence is worth a
            # log line, not a failed request.
            log.exception("could not persist markers to %s", self.path)


# --- the worker -------------------------------------------------------------


@dataclass
class FiducialStatsConfig:
    #: Samples waiting for the stream. Small: this is a live view, so when a consumer
    #: falls behind the stale sample is the one to drop.
    queue_size: int = 4
    #: Samples of trace kept per marker. At 25 fps, 5000 is a little over three minutes.
    history: int = 5000
    #: Bounds shutdown latency only; the queue get() blocks.
    idle_time: float = 0.1


class FiducialStatsWorker(threading.Thread):
    """Measures every marker on every frame of a camera fan-out queue.

    Output is `(stats, header)` with `stats` a `{marker_id: MarkerStats}` -- the same
    `(payload, header)` pairing the other workers put on their queues -- and each
    marker's trace is retained in `history`.
    """

    def __init__(
        self,
        store: FiducialStore,
        camera_queue: "queue.Queue",
        config: FiducialStatsConfig | None = None,
        name: str = "",
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self.store = store
        self.camera_queue = camera_queue
        self.config = config or FiducialStatsConfig()
        self.samples: "queue.Queue" = queue.Queue(maxsize=self.config.queue_size)
        self.history: dict[str, deque] = {}
        self.latest: tuple[dict[str, MarkerStats], dict] | None = None
        self.frame_shape: tuple[int, int] | None = None  # (height, width)
        self.n_processed = 0
        self.n_failures = 0
        self.error: str | None = None
        self.last_frame_at: float | None = None
        self._regions: dict[str, Region] = {}
        self._regions_key: tuple | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # --- measuring ---

    def process(self, frame: np.ndarray, header: dict | None) -> dict[str, MarkerStats]:
        """Measure every marker on `frame` and record the result."""
        header = dict(header or {})
        height, width = frame.shape[:2]
        regions = self._regions_for(height, width)
        stats = {mid: measure(frame, region) for mid, region in regions.items()}

        with self._lock:
            self.frame_shape = (height, width)
            self.latest = (stats, header)
            for mid, s in stats.items():
                self.history.setdefault(mid, deque(maxlen=self.config.history)).append((s, header))
            for gone in set(self.history) - set(stats):
                del self.history[gone]
            self.n_processed += 1
        return stats

    def _regions_for(self, height: int, width: int) -> dict[str, Region]:
        version, markers = self.store.snapshot()
        key = (version, height, width)
        if key != self._regions_key:
            self._regions = {mid: rasterise(m.shape, height, width) for mid, m in markers.items()}
            self._regions_key = key
        return self._regions

    # --- reads for the handler ---

    def trace(self, marker_id: str, limit: int | None = None) -> list[tuple[MarkerStats, dict]]:
        with self._lock:
            samples = list(self.history.get(marker_id, ()))
        return samples[-limit:] if limit else samples

    def latest_sample(self) -> tuple[dict[str, MarkerStats], dict] | None:
        with self._lock:
            return self.latest

    def frame_size(self) -> tuple[int, int] | None:
        """(width, height) of the last frame measured."""
        with self._lock:
            return None if self.frame_shape is None else (self.frame_shape[1], self.frame_shape[0])

    # --- thread ---

    def run(self) -> None:
        """Measure until stopped, surviving a bad frame -- an exception escaping run()
        would kill the thread while the capability went on looking healthy."""
        while not self._stop_event.is_set():
            try:
                frame, header = self.camera_queue.get(timeout=self.config.idle_time)
            except queue.Empty:
                continue

            try:
                stats = self.process(frame, header)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.n_failures += 1
                if self.n_failures == 1 or self.n_failures % 100 == 0:
                    log.exception("fiducial measurement failed (%s), dropping the frame", self.error)
                continue

            self.error = None
            self.last_frame_at = time.time()
            self._put((stats, header))

    def _put(self, content) -> None:
        """Never block on a slow consumer; drop the stale sample instead."""
        try:
            self.samples.put_nowait(content)
        except queue.Full:
            try:
                self.samples.get_nowait()
            except queue.Empty:
                pass
            try:
                self.samples.put_nowait(content)
            except queue.Full:
                pass

    def stop(self) -> None:
        log.info("fiducial stats worker receives a stop signal")
        self._stop_event.set()
