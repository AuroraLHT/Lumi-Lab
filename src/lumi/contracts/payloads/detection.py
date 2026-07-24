"""Detection payloads.

The old wire format base64'd the full RHEED pattern *and* every instance mask into
a single JSON body, per frame (see the former detection.communication.encode_detections).
That is the dominant cost on this path. The contract keeps the structured result in
JSON and moves the arrays to a NPY side-channel: `mask_ref` names a buffer carried
beside the JSON rather than inlined into it.
"""

from __future__ import annotations

from pydantic import BaseModel

from .common import ServerStateBase


class CropSetup(BaseModel):
    """Region of the frame fed to the detector."""

    x: int
    y: int
    width: int
    height: int


class DetectedBox(BaseModel):
    bbox_id: int
    x: float
    y: float
    width: float
    height: float
    score: float
    label: str | None = None
    # Name of the mask buffer in the accompanying NPY payload, if masks were
    # requested. None when the caller asked for masks to be dropped -- the
    # websocket bridge does exactly that before forwarding to a browser.
    mask_ref: str | None = None


class DetectionResult(BaseModel):
    time: float
    time_stamp: str
    uuid: str
    boxes: list[DetectedBox] = []
    classification: dict[str, float] = {}
    region2tracks: dict[str, list[int]] = {}
    # Reference to the pattern buffer in the NPY payload, if not dropped.
    pattern_ref: str | None = None


class OverlayBox(BaseModel):
    """A box, with no mask."""

    bbox_id: int
    x: float
    y: float
    width: float
    height: float
    score: float
    label: str | None = None


class DetectionOverlay(BaseModel):
    """What a browser draws on top of the video: boxes and labels, nothing else.

    This exists so that masks and the full RHEED pattern are never *sent* to a browser,
    rather than being sent and then stripped by a proxy. At 30fps the difference is
    hundreds of KB/s (plus a decode cost in the tab) against roughly 50 KB/s -- and with
    a separate routing key the heavy arrays only ever travel toward the consumers that
    bind the heavy key, which is storage.
    """

    time: float
    time_stamp: str
    uuid: str
    boxes: list[OverlayBox] = []
    classification: dict[str, float] = {}


class DetectionReadout(BaseModel):
    classifier_classes: list[str] = []
    device: str | None = None
    crop: CropSetup | None = None
    model_path: str | None = None


class DetectionState(DetectionReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the detector's readout.
    pass


class OverlayReadout(BaseModel):
    n_boxes: int = 0


class OverlayState(OverlayReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the overlay's readout.
    pass
