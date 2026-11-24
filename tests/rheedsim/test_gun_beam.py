"""The simulated RHEED image follows the twin: dark with the gun off, its brightness
with the emission current, oscillations included; and its growth oscillations with
the pulses deposited.

T0: no broker.
"""

from __future__ import annotations

import numpy as np

from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.contracts.payloads.pld import PldChamberState, PldLaser, PldRheed
from lumi.rheed.hardware import GrowthClock, GunBeam
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


def chamber(fired: int, *, open_: bool = True, firing: bool = False, rate: float = 10.0,
            requested: int = 400):
    return PldChamberState(
        sample_shutter_away=open_,
        laser=PldLaser(status="firing" if firing else "finished", pulses_fired=fired,
                       rate_hz=rate, pulses_requested=requested))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_the_oscillations_follow_the_deposition():
    clock = Clock()
    growth = GrowthClock(pulses_per_monolayer=40, depth=0.4, decay_ml=10, clock=clock)
    assert growth.scale() == 1.0  # nothing grown
    growth.update(chamber(0, firing=True))
    clock.t = 2.0  # 20 pulses at 10 Hz since the reading: half a monolayer, the dip
    assert growth.monolayers() == 0.5
    assert growth.scale() == 1.0 - 0.4 * np.exp(-0.05)
    growth.update(chamber(40, firing=True))
    assert growth.monolayers() == 1.0  # the reading takes over from the estimate
    assert abs(growth.scale() - 1.0) < 1e-9  # a whole monolayer: recovered
    clock.t = 100.0
    assert growth.monolayers() == 1 + 360 / 40  # counted on no further than the train
    growth.update(chamber(400))
    assert growth.monolayers() == 10.0  # finished: holds still
    clock.t = 200.0
    assert growth.monolayers() == 10.0


def test_only_pulses_with_the_shutter_open_grow_the_film():
    clock = Clock()
    growth = GrowthClock(pulses_per_monolayer=40, clock=clock)
    growth.update(chamber(0, open_=False, firing=True))
    clock.t = 5.0
    assert growth.monolayers() == 0.0  # pre-ablation behind the shutter
    growth.update(chamber(50, open_=False))
    growth.update(chamber(0, firing=True))  # a new train, shutter open
    growth.update(chamber(20, firing=True))
    assert growth.monolayers() == 0.5
    growth.update(None)  # no reading: no counting on
    clock.t = 50.0
    assert growth.monolayers() == 0.5


def sim_camera(**config) -> SimCamera:
    base = dict(frame_dims=(540, 720), source=str(SIM_FRAMES["sto"].path), fps=1, queue_size=1,
                idle_time=0.0, is_base_oscillation=False, base_oscillation_frequency=0.1,
                base_oscillation_amplitude=100, is_feature_oscillation=False,
                feature_bbox=(100, 100, 620, 300), feature_oscillation_frequency=0.0,
                feature_oscillation_amplitude=0.0, exposure_time=60000, gain=200, gamma=1.0,
                max_intensity=4095)
    return SimCamera(config=SimCameraConfig(**{**base, **config}), name="test", daemon=True)


SPECULAR = (slice(253, 289), slice(357, 393))  # the agent's box on the sto frame
BACKGROUND = (slice(160, 200), slice(30, 70))


def test_the_spots_dip_and_the_background_between_them_does_not():
    clock = Clock()
    growth = GrowthClock(pulses_per_monolayer=40, depth=0.4, decay_ml=1e9, clock=clock)
    camera = sim_camera(feature_scale=growth.scale)
    before = camera.on_grab()[0].astype(float)
    growth.update(chamber(0))
    growth.update(chamber(20))  # half a monolayer: the deepest dip
    after = camera.on_grab()[0].astype(float)
    assert after[BACKGROUND].mean() / before[BACKGROUND].mean() > 0.995  # flat, to a count
    ratio = after[SPECULAR].sum() / before[SPECULAR].sum()
    assert 0.6 < ratio < 0.65  # the whole dip, nearly: the mask is ~1 on the specular spot
    # No hard edge: the mask falls off over pixels, not at a rectangle.
    mask = camera.feature_mask()
    assert mask.max() == 1.0 and mask[BACKGROUND].max() < 0.05
    assert 0.0 < float(np.median(np.abs(np.diff(mask, axis=1))[mask[:, 1:] > 0.05])) < 0.1


def test_the_surface_recovers_partly_when_the_laser_stops():
    clock = Clock()
    growth = GrowthClock(pulses_per_monolayer=40, depth=0.4, decay_ml=1e9, recovery=0.6,
                         recovery_s=20.0, resume_s=2.0, clock=clock)
    growth.update(chamber(0, firing=True, rate=10.0))
    clock.t = 2.0
    growth.update(chamber(20, firing=True, rate=10.0))  # half a monolayer in
    growth.update(chamber(20))  # stopped mid-dip
    stopped = growth.scale()
    assert abs(stopped - 0.6) < 1e-9
    clock.t = 2.0 + 20.0
    assert abs(growth.scale() - (1 - 0.4 * (0.4 + 0.6 * np.exp(-1)))) < 1e-9
    clock.t = 1000.0
    assert abs(growth.scale() - (1 - 0.4 * 0.4)) < 1e-6  # 60 % of the dip gone, no more
    assert growth.monolayers() == 0.5  # nothing grown meanwhile
    growth.update(chamber(0, firing=True, rate=10.0))  # firing again: the dip comes back
    clock.t = 1000.0 + 10 * 2.0
    assert abs(growth.shown() - 1.0) < 1e-3


def test_noise_makes_each_frame_differ_by_about_the_shot_noise():
    camera = sim_camera(noise_read=3.0, noise_shot=1.0)
    a, b = (camera.on_grab()[0].astype(float) for _ in range(2))
    assert (a != b).any()
    spot = sim_camera().on_grab()[0].astype(float)[SPECULAR]
    expected = np.sqrt(9.0 + spot[spot < 4000].mean())  # unclipped pixels
    measured = np.std((a - b)[SPECULAR][spot < 4000]) / np.sqrt(2)
    assert 0.8 * expected < measured < 1.2 * expected
    assert (sim_camera().on_grab()[0] == sim_camera().on_grab()[0]).all()  # none by default
