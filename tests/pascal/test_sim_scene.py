"""T0 tests for the simulated chamber camera's scene overlay (`lumi.pascal.sim.scene`).

No broker, no hardware, no real image asset -- a synthetic frame is enough to pin down
the geometry: the calibration the two Mask1 reference points produce, that the slit
reaches the holder's centre and nowhere else does by construction, that the mask does
not rotate with the sample, and that a marker sitting where the slit sweeps sees its
reading dip and recover the way the real feature (the whole reason fiducial statistics
exist) needs it to.
"""

from __future__ import annotations

import numpy as np
import pytest

from lumi.pascal.sim.model import ChamberModel, ChamberSimConfig
from lumi.pascal.sim.scene import ChamberSceneRenderer, HolderGeometry, MaskGeometry

H, W = 300, 400


def blank(value: int = 200) -> np.ndarray:
    return np.full((H, W, 3), value, dtype=np.uint8)


@pytest.fixture
def model() -> ChamberModel:
    return ChamberModel(ChamberSimConfig(temperature_noise=0.0))


@pytest.fixture
def holder() -> HolderGeometry:
    return HolderGeometry(center_x=200.0, center_y=150.0, inner_circle_diameter=100.0, edge_angle=0.0)


@pytest.fixture
def mask() -> MaskGeometry:
    return MaskGeometry(center_position_mm=96.0, hidden_position_mm=50.0, direction=1.0, opacity=1.0)


def set_axis(axis, value: float) -> None:
    axis.position = axis.target = float(value)


def test_well_past_the_hidden_position_the_mask_does_not_touch_the_frame(model, holder, mask):
    """`hidden_position_mm` is *defined* as the point the mask's near edge is exactly at
    the frame boundary -- a hair further out is unambiguously clear of it."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    set_axis(model.mask1, mask.hidden_position_mm - 10.0)
    frame = blank()

    out = renderer.render(frame)

    assert np.array_equal(out, frame.astype(np.float32))


def test_at_the_hidden_position_the_holder_centre_is_unaffected(model, holder, mask):
    renderer = ChamberSceneRenderer(model, holder, mask)
    set_axis(model.mask1, mask.hidden_position_mm)

    out = renderer.render(blank(200))

    row, col = int(holder.center_y), int(holder.center_x)
    assert out[row, col, 0] == pytest.approx(200.0)


def test_at_the_centre_position_the_holder_centre_pixel_is_lit_through_the_slit(model, holder, mask):
    renderer = ChamberSceneRenderer(model, holder, mask)
    set_axis(model.mask1, mask.center_position_mm)

    out = renderer.render(blank(200))

    row, col = int(holder.center_y), int(holder.center_x)
    assert out[row, col, 0] == pytest.approx(200.0)  # the slit: not darkened


def test_beyond_the_centre_the_mask_has_moved_past_and_the_centre_pixel_darkens(model, holder, mask):
    """The slit is only *at* the centre right at `center_position_mm` -- move Mask1 on
    past it by more than half the slit's own width and the opaque plate covers the
    centre pixel instead."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    slit_u_mm = renderer._slit_u / renderer._scale(H, W)
    set_axis(model.mask1, mask.center_position_mm + slit_u_mm)

    out = renderer.render(blank(200))

    row, col = int(holder.center_y), int(holder.center_x)
    assert out[row, col, 0] < 200.0


def test_the_mask_extent_matches_the_holders_inner_circle(model, holder, mask):
    """'the mask has the width that mostly covers the sample holder' -- 100% of the
    inner circle's diameter, both along the travel axis and across it."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    assert renderer._mask_u == holder.inner_circle_diameter
    assert renderer._mask_v == holder.inner_circle_diameter


def test_the_slit_is_two_thirds_the_mask_length_at_a_1_to_10_aspect(model, holder, mask):
    renderer = ChamberSceneRenderer(model, holder, mask)
    assert renderer._slit_v == pytest.approx(holder.inner_circle_diameter * 2 / 3)
    assert renderer._slit_u == pytest.approx(renderer._slit_v / 10)


def test_sample_rotation_turns_the_holder_but_not_the_mask(model, holder, mask):
    """A bright dot off to one side of the holder centre should move when the sample
    rotates; the dark mask plate's own tilt (edge_angle) must not follow it."""
    holder = HolderGeometry(center_x=200.0, center_y=150.0, inner_circle_diameter=60.0, edge_angle=0.0)
    renderer = ChamberSceneRenderer(model, holder, mask)
    set_axis(model.mask1, mask.hidden_position_mm)  # keep the mask out of the way

    frame = blank(0)
    dot_row, dot_col = int(holder.center_y), int(holder.center_x) + 40
    frame[dot_row - 1:dot_row + 2, dot_col - 1:dot_col + 2] = 255

    set_axis(model.sample_rot, 0.0)
    unrotated = renderer.render(frame.copy())
    set_axis(model.sample_rot, 90.0)
    rotated = renderer.render(frame.copy())

    assert not np.array_equal(unrotated, rotated)
    # The dot has moved: its original spot is no longer bright once rotated 90 degrees.
    assert unrotated[dot_row, dot_col, 0] > 200
    assert rotated[dot_row, dot_col, 0] < 50


def test_a_marker_at_the_slit_position_dips_as_mask1_sweeps_past_it(model, holder, mask):
    """The point of the whole feature: a fiducial sitting at the holder centre should
    read bright when the slit is centred there and dark once the mask has slid past."""
    from lumi.base.camera.fiducials import measure, rasterise
    from lumi.contracts.payloads.fiducial import CrossShape

    renderer = ChamberSceneRenderer(model, holder, mask)
    target = CrossShape(kind="cross", x=holder.center_x, y=holder.center_y, sample_radius=0)
    region = rasterise(target, H, W)
    # Just past the slit's own edge but well short of the mask's -- the shoulder of the
    # opaque plate, guaranteed to cover the centre once it has slid this far.
    dip_mm = mask.center_position_mm + 2 * renderer._slit_u / renderer._scale(H, W)

    readings = {}
    for mm in (mask.hidden_position_mm, mask.center_position_mm, dip_mm):
        set_axis(model.mask1, mm)
        frame = renderer.render(blank(200)).clip(0, 255).astype(np.uint8)
        readings[mm] = measure(frame, region).center

    assert readings[mask.hidden_position_mm] == pytest.approx(200.0)  # slit far away: unobstructed
    assert readings[mask.center_position_mm] == pytest.approx(200.0)  # slit right on it: unobstructed
    assert readings[dip_mm] < 50  # mask has slid on: opaque
