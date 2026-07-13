"""The detection node: a Cascade Mask R-CNN over the RHEED pattern.

Lives on the RHEED exchange because it is an analysis of the RHEED stream.
"""

from __future__ import annotations

from .payloads.common import Ack, Empty
from .payloads.detection import (
    CropSetup,
    DetectionOverlay,
    DetectionResult,
    DetectionState,
    OverlayState,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op, StreamSpec

DETECTION = Capability(
    name="detection",
    kind=Kind.DUPLEX,
    doc="Instance segmentation and classification of the RHEED pattern.",
    state=DetectionState,
    ops=(
        # NPZ: the structured result is JSON metadata, while the pattern and the
        # per-instance masks travel as named arrays beside it. The old format base64'd
        # all of them *into* the JSON body, every frame -- the single most expensive
        # thing on this path.
        Op("detection", Empty, DetectionResult, response_codec=Codec.NPZ,
           doc="Detections for the latest frame, with pattern and masks."),
        Op("set_detection_crop", CropSetup, Ack),
    ),
    stream=StreamSpec("detection", DetectionResult, codec=Codec.NPZ),
)

OVERLAY = Capability(
    name="overlay",
    kind=Kind.STREAM,
    doc="The same detections as `detection`, minus the masks and the pattern -- the "
        "view a browser draws. A separate capability, so it has its own routing key: "
        "the heavy arrays travel only to whoever binds the heavy key.",
    state=OverlayState,
    stream=StreamSpec("overlay", DetectionOverlay),
)

DETECTION_NODE = EquipmentContract(
    name="detection",
    exchange="RHEED",
    version="2.0",
    capabilities=(DETECTION, OVERLAY),
)
