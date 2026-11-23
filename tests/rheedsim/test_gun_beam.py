"""The simulated RHEED image follows the twin's gun: dark with it off, its brightness
with the emission current, oscillations included.

T0: no broker.
"""

from __future__ import annotations

import numpy as np

from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.contracts.payloads.pld import PldRheed
from lumi.rheed.hardware import GunBeam
from lumi.rheed.sim_frames import SIM_FRAMES


def gun(**state) -> PldRheed:
    return PldRheed(present=True, link_ok=True, **state)


def test_the_beam_follows_the_gun():
    beam = GunBeam(reference_ua=8.0)
    assert beam.scale() == 1.0  # no reading yet: the image as it was
    beam.update(gun())
    assert beam.scale() == 0.0  # filament and HV off: no beam
    beam.update(gun(filament_on=True, hv_on=False, emission_ua=0.0))
    assert beam.scale() == 0.0
    beam.update(gun(filament_on=True, hv_on=True, emission_ua=6.0))
    assert beam.scale() == 0.75
    beam.update(gun(filament_on=True, hv_on=True, emission_ua=None))
    assert beam.scale() == 1.0
    beam.update(PldRheed(present=False))
    assert beam.scale() == 1.0  # no RHEED link: nothing to follow
    beam.update(None)
    assert beam.scale() == 1.0


def test_the_sim_camera_scales_the_whole_frame():
    beam = GunBeam(reference_ua=8.0)
    camera = SimCamera(config=SimCameraConfig(
        frame_dims=(540, 720), source=str(SIM_FRAMES["sto"].path), fps=1, queue_size=1,
        idle_time=0.0, is_base_oscillation=True, base_oscillation_frequency=0.1,
        base_oscillation_amplitude=100, is_feature_oscillation=False, feature_bbox=(0, 0, 1, 1),
        feature_oscillation_frequency=0.0, feature_oscillation_amplitude=0.0,
        exposure_time=60000, gain=200, gamma=1.0, max_intensity=4095,
        frame_scale=beam.scale), name="test", daemon=True)
    beam.update(gun())
    frame, _ = camera.on_grab()
    assert frame.max() == 0  # no beam, no oscillation either
    beam.update(gun(filament_on=True, hv_on=True, emission_ua=4.0))
    half = camera.on_grab()[0].astype(float).mean()
    beam.update(gun(filament_on=True, hv_on=True, emission_ua=8.0))
    full = camera.on_grab()[0].astype(float).mean()
    assert half / full == np.clip(half / full, 0.45, 0.55)
