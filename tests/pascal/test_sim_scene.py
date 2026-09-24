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
    return MaskGeometry(center_position_mm=96.0, hidden_position_mm=50.0, direction=1.0,
                         arm_length=500.0, color=0.0, alpha=1.0)


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
    past the slit's own (narrow) extent along the travel axis, but not so far the
    plate's own head clears the point too, and the opaque plate covers the centre
    pixel instead."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    past_slit_px = renderer._slit_v / 2 + 5  # just past the slit's edge along travel...
    assert past_slit_px < renderer._mask_v_bounds[1]  # ...but still within the head
    set_axis(model.mask1, mask.center_position_mm + past_slit_px / renderer._scale(H, W))

    out = renderer.render(blank(200))

    row, col = int(holder.center_y), int(holder.center_x)
    assert out[row, col, 0] < 200.0


def test_the_mask_extent_matches_the_holders_inner_circle(model, holder, mask):
    """'the mask has the width that mostly covers the sample holder' -- 100% of the
    inner circle's diameter along the travel axis, and its head (where the slit is
    cut) is square with it across the axis too; the arm then extends further still,
    off the side away from the slit."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    half = holder.inner_circle_diameter / 2
    assert renderer._mask_u == holder.inner_circle_diameter
    assert renderer._mask_v_bounds == (-(half + mask.arm_length), half)


def test_the_slit_is_two_thirds_the_mask_length_at_a_1_to_10_aspect(model, holder, mask):
    """The slit's long axis runs along u, parallel to the plate's short edge (and so to
    the sample holder block); its narrow axis runs along v, parallel to the arm."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    assert renderer._slit_u == pytest.approx(holder.inner_circle_diameter * 2 / 3)
    assert renderer._slit_v == pytest.approx(renderer._slit_u / 10)


def test_increasing_mask1_slides_the_plate_from_top_left_toward_bottom_right(model, mask):
    """Mask1 translates the plate along its own length (the arm's direction), not
    across it -- a real paddle slides along the rail it is mounted on. A tilted
    edge_angle (as on the real chamber) is needed to tell "top-left to bottom-right"
    apart from a plain up/down slide, which edge_angle=0 can't distinguish."""
    holder = HolderGeometry(center_x=200.0, center_y=150.0, inner_circle_diameter=100.0, edge_angle=-39.0)
    renderer = ChamberSceneRenderer(model, holder, mask)

    set_axis(model.mask1, mask.hidden_position_mm + 2.0)
    near_hidden = renderer.render(blank(200))
    set_axis(model.mask1, mask.center_position_mm)
    at_centre = renderer.render(blank(200))

    def centroid(frame: np.ndarray) -> tuple[float, float]:
        """Mean (row, col) of the painted pixels -- where the visible part of the
        plate sits, on average."""
        rows, cols = np.nonzero(frame[:, :, 0] < 200)
        assert len(rows) > 0
        return float(rows.mean()), float(cols.mean())

    hidden_row, hidden_col = centroid(near_hidden)
    centre_row, centre_col = centroid(at_centre)
    # Further along (more Mask1): the visible plate has moved down and right, not up
    # and right -- i.e. toward the bottom-right corner, not the top-right one.
    assert centre_row > hidden_row
    assert centre_col > hidden_col


def test_the_arm_reaches_well_past_the_head_on_the_side_away_from_the_slit(model, holder, mask):
    """A real mask hangs off an arm mounted outside the frame -- the plate should cover
    a point far off to one side of the holder (beyond where the small square head used
    to reach) once positioned over the frame."""
    renderer = ChamberSceneRenderer(model, holder, mask)
    set_axis(model.mask1, mask.center_position_mm)

    out = renderer.render(blank(200))

    far_row = int(holder.center_y) - int(holder.inner_circle_diameter / 2) - 100  # into the arm, not the head
    assert far_row >= 0
    assert out[far_row, int(holder.center_x), 0] == pytest.approx(mask.color)


def test_alpha_one_paints_a_flat_colour_alpha_zero_leaves_the_frame_showing(model, holder):
    set_axis(model.mask1, 96.0)
    opaque_row, opaque_col = 150, 240  # inside the head (u<=50), outside the slit (u>33.3)

    solid = MaskGeometry(center_position_mm=96.0, hidden_position_mm=50.0, direction=1.0,
                          arm_length=0.0, color=77.0, alpha=1.0)
    out = ChamberSceneRenderer(model, holder, solid).render(blank(200))
    assert out[opaque_row, opaque_col, 0] == pytest.approx(77.0)

    invisible = MaskGeometry(center_position_mm=96.0, hidden_position_mm=50.0, direction=1.0,
                              arm_length=0.0, color=77.0, alpha=0.0)
    out = ChamberSceneRenderer(model, holder, invisible).render(blank(200))
    assert out[opaque_row, opaque_col, 0] == pytest.approx(200.0)


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
    # Just past the slit's own edge along the travel axis but well short of the
    # plate's -- the shoulder of the opaque head, guaranteed to cover the centre once
    # it has slid this far.
    past_slit_px = renderer._slit_v / 2 + 5
    assert past_slit_px < renderer._mask_v_bounds[1]
    dip_mm = mask.center_position_mm + past_slit_px / renderer._scale(H, W)

    readings = {}
    for mm in (mask.hidden_position_mm, mask.center_position_mm, dip_mm):
        set_axis(model.mask1, mm)
        frame = renderer.render(blank(200)).clip(0, 255).astype(np.uint8)
        readings[mm] = measure(frame, region).center

    assert readings[mask.hidden_position_mm] == pytest.approx(200.0)  # slit far away: unobstructed
    assert readings[mask.center_position_mm] == pytest.approx(200.0)  # slit right on it: unobstructed
    assert readings[dip_mm] < 50  # mask has slid on: opaque
