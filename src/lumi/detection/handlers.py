"""Detection handler.

Two things change from the old DetectionMessageQueueServer, and neither is a
straight port:

1. **Frames arrive by subscription, not by RPC.** The old server held a
   CameraMessageQueueClient and did a *round trip to the RHEED node for every single
   detection request*. Detection now subscribes to `rheed.camera`'s stream and feeds
   the detector as frames arrive, which is both cheaper and how a continuous detector
   should work.

2. **`request_detection` was already broken.** It did

       body, headers = await self.camera_client.get_live_image()

   but get_live_image() returns one ResponseMessageQueueMessage, not a 2-tuple, so it
   raised on the first request that carried a body. It has not worked; it is not
   ported.

Masks and the pattern now ride in an NPZ archive beside the JSON, rather than being
base64'd into it a frame at a time.
"""

from __future__ import annotations

import logging
import queue

import numpy as np

from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.detection import (
    CropSetup,
    DetectedBox,
    DetectionOverlay,
    DetectionReadout,
    DetectionResult,
    OverlayBox,
    OverlayReadout,
)

log = logging.getLogger(__name__)


class DetectionHandler:
    def __init__(self, detector, *, drop_masks: bool = False) -> None:
        self.detector = detector
        self.drop_masks = drop_masks
        self._latest: tuple[DetectionResult, dict[str, np.ndarray]] | None = None
        self._sinks: list = []

    # --- fed by the camera subscription -----------------------------------

    def on_frame(self, frame: np.ndarray, header: dict) -> None:
        """Hand a frame to the detector thread. Called from the camera stream.

        Non-blocking: if the detector is still busy with the previous frame we drop
        this one rather than queueing. A detector that falls behind should analyse the
        *newest* pattern, not work through a backlog of stale ones.
        """
        try:
            self.detector.input_queue.put_nowait((frame, header))
        except queue.Full:
            pass

    # --- contract ops -----------------------------------------------------

    async def detection(self, req: Empty) -> tuple[DetectionResult, dict[str, np.ndarray]]:
        """The most recent detection, whether or not it is new."""
        self._drain()
        if self._latest is None:
            raise RuntimeError("no detection available yet -- is the camera streaming?")
        return self._latest

    async def set_detection_crop(self, req: CropSetup) -> Ack:
        # The detector speaks in corners, the contract in origin+size.
        self.detector.set_crop(
            sx=req.x, sy=req.y, ex=req.x + req.width, ey=req.y + req.height
        )
        log.info("detection crop set to %s", req)
        return Ack()

    async def next(self) -> tuple[DetectionResult, dict[str, np.ndarray]] | None:
        """Only *new* detections go on the stream; `detection` re-serves the last one."""
        return self._drain()

    def _drain(self):
        """Take everything the detector has produced and keep the newest.

        Draining rather than taking one: inference is slower than the camera, so if
        several results have piled up the interesting one is the last.
        """
        newest = None
        while True:
            try:
                raw, header = self.detector.output_queue.get_nowait()
            except queue.Empty:
                break
            newest = _to_contract(raw, header, drop_masks=self.drop_masks)
        if newest is not None:
            self._latest = newest
            for sink in self._sinks:
                sink(newest[0])
        return newest

    def add_sink(self, fn) -> None:
        """Fan a new detection out to another capability (the overlay stream)."""
        self._sinks.append(fn)

    def readout(self) -> DetectionReadout:
        crop = getattr(self.detector.state, "crop_setup", None) or {}
        return DetectionReadout(
            classifier_classes=list(getattr(self.detector, "classifier_classes", []) or []),
            device=getattr(self.detector.config, "detector_model_device", None),
            crop=(
                CropSetup(
                    x=crop["sx"], y=crop["sy"],
                    width=crop["ex"] - crop["sx"], height=crop["ey"] - crop["sy"],
                )
                if crop
                else None
            ),
            model_path=getattr(self.detector.config, "detector_model_path", None),
        )


class OverlayHandler:
    """Serves the `overlay` stream: boxes only, no masks, no pattern.

    Fed by DetectionHandler rather than by the detector directly, so inference still
    happens exactly once. The only difference between the two capabilities is what goes
    on the wire -- and therefore who pays for it.
    """

    def __init__(self) -> None:
        self._pending: DetectionOverlay | None = None
        self._n_boxes = 0

    def on_detection(self, result: DetectionResult) -> None:
        self._pending = DetectionOverlay(
            time=result.time,
            time_stamp=result.time_stamp,
            uuid=result.uuid,
            boxes=[
                OverlayBox(
                    bbox_id=b.bbox_id, x=b.x, y=b.y, width=b.width, height=b.height,
                    score=b.score, label=b.label,
                )
                for b in result.boxes
            ],
            classification=result.classification,
        )
        self._n_boxes = len(result.boxes)

    async def next(self) -> DetectionOverlay | None:
        pending, self._pending = self._pending, None
        return pending

    def readout(self) -> OverlayReadout:
        return OverlayReadout(n_boxes=self._n_boxes)


def _to_contract(
    raw: dict, header: dict, *, drop_masks: bool = False
) -> tuple[DetectionResult, dict[str, np.ndarray]]:
    """The detector's internal dict -> the wire contract.

    This is the *only* place that knows the detector's internal shape. The old code
    destructured it by hand in three separate files -- encode_detections, storage's
    _start_ai_storage, and the websocket mapper -- each with its own idea of the keys.
    """
    header = dict(header or {})
    arrays: dict[str, np.ndarray] = {}

    pattern_ref = None
    seg = raw.get("instance_segementation")
    if seg is not None and getattr(seg, "rd", None) is not None:
        arrays["pattern"] = np.asarray(seg.rd.pattern)
        pattern_ref = "pattern"

    boxes: list[DetectedBox] = []
    for key, box in (raw.get("bboxes") or {}).items():
        bbox_id = int(key)
        x1, y1, x2, y2 = box["bbox"]

        mask_ref = None
        if not drop_masks and box.get("mask") is not None:
            mask_ref = f"mask_{bbox_id}"
            arrays[mask_ref] = np.asarray(box["mask"])

        boxes.append(
            DetectedBox(
                bbox_id=bbox_id,
                x=float(x1), y=float(y1),
                width=float(x2 - x1), height=float(y2 - y1),
                score=float(box["score"]),
                label=str(box.get("label")),
                mask_ref=mask_ref,
            )
        )

    result = DetectionResult(
        time=float(header.get("time", 0.0)),
        time_stamp=str(header.get("time_stamp", "")),
        uuid=str(header.get("uuid", "")),
        boxes=boxes,
        classification={str(k): float(v) for k, v in (raw.get("classification") or {}).items()},
        region2tracks={
            str(k): int(v) for k, v in (raw.get("region2tracks") or {}).items()
        },
        pattern_ref=pattern_ref,
    )
    return result, arrays
