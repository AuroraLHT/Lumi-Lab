"""The detection crop, in image coordinates.

T0: no model. rhana's Rheed.crop slices pattern[sx:ex, sy:ey] -- its "x" runs along
rows -- so the contract's x/width are the detector's sy/ey and y/height its sx/ex.
Reported the other way round, a 720-wide frame read as 540 wide and the Deck mapped
boxes back to the wrong place.
"""

from __future__ import annotations

import numpy as np

from lumi.contracts.payloads.detection import CropSetup
from lumi.detection.handlers import DetectionHandler


class FakeDetector:
    def __init__(self):
        self.state = type("State", (), {"crop_setup": {"sx": 0, "sy": 0, "ex": 540, "ey": 720}})()
        self.config = type("Config", (), {"detector_model_device": "cpu", "detector_model_path": "m"})()
        self.classifier_classes = ["2D"]

    def set_crop(self, sx, sy, ex, ey):
        self.state.crop_setup = {"sx": sx, "sy": sy, "ex": ex, "ey": ey}


def test_the_full_frame_is_720_wide_and_540_tall():
    crop = DetectionHandler(FakeDetector()).readout().crop
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 720, 540)


async def test_a_crop_set_is_the_crop_read_back_and_the_region_the_detector_sees():
    detector = FakeDetector()
    handler = DetectionHandler(detector)
    await handler.set_detection_crop(CropSetup(x=100, y=50, width=400, height=300))
    assert handler.readout().crop == CropSetup(x=100, y=50, width=400, height=300)
    c = detector.state.crop_setup
    frame = np.zeros((540, 720))
    frame[50:350, 100:500] = 1  # the region asked for, in image terms
    # What rhana's crop keeps: rows sx:ex, columns sy:ey.
    assert frame[c["sx"]:c["ex"], c["sy"]:c["ey"]].shape == (300, 400)
    assert frame[c["sx"]:c["ex"], c["sy"]:c["ey"]].all()
