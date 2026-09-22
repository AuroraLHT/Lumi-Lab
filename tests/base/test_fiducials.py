"""Fiducial markers on the chamber camera. Tier T0: no broker, no camera.

What is pinned here is the reason the feature exists: a marker's intensity trace dips as
the mask edge crosses it, so the statistics have to be *right* (the right pixels, the
right numbers), the markers have to survive a restart, and an operator's mistakes -- a
marker off the frame, a corrupt file -- have to cost a marker rather than the node.
"""

from __future__ import annotations

import queue

import numpy as np
import pytest
from pydantic import ValidationError

from lumi.base.camera.fiducials import (
    FiducialStatsConfig,
    FiducialStatsWorker,
    FiducialStore,
    geometric_center,
    measure,
    rasterise,
)
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.fiducial import (
    CircleShape,
    CrossShape,
    FiducialMarker,
    MarkerHistoryQuery,
    MarkerId,
    Point,
    PolyShape,
    RectShape,
    RoleAssignment,
    RoleQuery,
)
from lumi.pascal.handlers import FiducialHandler

H, W = 48, 64


def gradient() -> np.ndarray:
    """Greyscale where every pixel's value is its own column, so any region's expected
    statistics can be worked out by hand."""
    return np.tile(np.arange(W, dtype=np.uint8), (H, 1))


def rect(x, y, w, h) -> RectShape:
    return RectShape(kind="rect", x=x, y=y, width=w, height=h)


def marker(marker_id: str, shape) -> FiducialMarker:
    return FiducialMarker(marker_id=marker_id, shape=shape)


def header(uid: str = "u", t: float = 1.0) -> dict:
    # The cameras write `time` as a string.
    return {"time": str(t), "uuid": uid, "time_stamp": "2026-09-20 12:00:00"}


# --- the statistics ----------------------------------------------------------


def test_rect_statistics_are_over_exactly_its_pixels():
    stats = measure(gradient(), rasterise(rect(10, 5, 4, 3), H, W))

    cols = np.array([10, 11, 12, 13])
    assert stats.n_pixels == 12
    assert stats.min == 10 and stats.max == 13
    assert stats.mean == pytest.approx(cols.mean())
    assert stats.std == pytest.approx(cols.std())  # population std, not sample


def test_a_uniform_region_has_zero_std():
    frame = np.full((H, W), 7, dtype=np.uint8)
    stats = measure(frame, rasterise(rect(0, 0, W, H), H, W))
    assert (stats.mean, stats.min, stats.max, stats.std) == (7, 7, 7, 0)


def cross(x, y, size=10, radius=1) -> CrossShape:
    return CrossShape(kind="cross", x=x, y=y, size=size, sample_radius=radius)


def circle(x, y, radius) -> CircleShape:
    return CircleShape(kind="circle", x=x, y=y, radius=radius)


def triangle() -> PolyShape:
    return PolyShape(kind="poly", points=[Point(x=10, y=10), Point(x=30, y=10), Point(x=10, y=30)])


# --- the cross is a point ----------------------------------------------------


def test_a_cross_at_radius_zero_measures_the_single_pixel_under_its_centre():
    stats = measure(gradient(), rasterise(cross(30, 20, radius=0), H, W))

    assert stats.n_pixels == 1
    assert stats.center == 30  # the gradient's value at column 30
    assert stats.mean == stats.min == stats.max == stats.center
    assert stats.std == 0


def test_a_cross_averages_a_3x3_patch_by_default():
    stats = measure(gradient(), rasterise(CrossShape(kind="cross", x=30, y=20), H, W))

    assert stats.n_pixels == 9
    assert (stats.min, stats.max) == (29, 31)  # columns 29..31
    assert stats.mean == pytest.approx(30)
    assert stats.std == pytest.approx(np.array([29, 30, 31]).std())


def test_the_patch_steadies_the_reading_but_the_centre_stays_the_raw_pixel():
    """A noisy pixel: the patch mean sits near the truth, `center` is the noise itself."""
    frame = np.full((H, W), 100, dtype=np.uint8)
    frame[20, 30] = 190  # a hot centre pixel

    stats = measure(frame, rasterise(cross(30, 20), H, W))

    assert stats.center == 190
    assert stats.mean == pytest.approx((8 * 100 + 190) / 9)


@pytest.mark.parametrize("radius,pixels", [(0, 1), (1, 9), (2, 25), (3, 49)])
def test_the_patch_is_a_square_of_the_given_radius(radius, pixels):
    assert measure(gradient(), rasterise(cross(30, 20, radius=radius), H, W)).n_pixels == pixels


def test_a_patch_hanging_off_the_frame_edge_is_clipped_not_shifted():
    corner = measure(gradient(), rasterise(cross(0.5, 0.5), H, W))
    assert corner.n_pixels == 4 and corner.center == 0
    far = measure(gradient(), rasterise(cross(W - 0.5, H - 0.5), H, W))
    assert far.n_pixels == 4 and far.center == W - 1


def test_the_arms_of_a_cross_are_display_only():
    """Everything on the arms outside the patch is a value the reading must not see."""
    frame = np.zeros((H, W), dtype=np.uint8)
    frame[20, 20:28] = 255  # the horizontal arm, either side of the 3x3 patch
    frame[20, 33:41] = 255
    frame[10:18, 30] = 255  # the vertical arm
    frame[23:31, 30] = 255
    frame[19:22, 29:32] = 100  # the patch itself

    stats = measure(frame, rasterise(cross(30, 20, size=10), H, W))

    assert (stats.center, stats.mean, stats.max) == (100, 100, 100)


def test_the_size_of_a_cross_does_not_change_what_it_measures():
    small = measure(gradient(), rasterise(cross(30, 20, size=2), H, W))
    large = measure(gradient(), rasterise(cross(30, 20, size=25), H, W))
    assert small == large


def test_a_cross_measures_the_pixel_the_point_is_over():
    """Pixel 30 covers [30, 31), so 30.9 is still over it -- rounding would pick 31, and
    a click on the canvas would then measure a different pixel from the one it landed on."""
    assert measure(gradient(), rasterise(cross(30.9, 20.9), H, W)).center == 30
    assert measure(gradient(), rasterise(cross(31.0, 20.0), H, W)).center == 31
    # and the patch is centred on that pixel: 30.9 -> columns 29..31, not 30..32.
    assert measure(gradient(), rasterise(cross(30.9, 20.9), H, W)).mean == pytest.approx(30)


def test_a_cross_off_the_frame_reads_nothing():
    stats = measure(gradient(), rasterise(cross(500, 20), H, W))
    assert stats.n_pixels == 0 and stats.center is None and stats.mean is None


def test_a_moving_mask_edge_steps_a_cross_from_bright_to_dark_as_it_passes():
    """The point version of the dip: the cross's centre goes dark on exactly the frame
    the mask edge reaches its pixel, which is the sharpest reading the marker can give."""
    store = FiducialStore()
    store.set(marker("pt", cross(33, 20)))
    worker = FiducialStatsWorker(store, queue.Queue(), FiducialStatsConfig(history=100))

    for edge_x in range(0, W):  # the mask's right edge, one pixel at a time
        frame = np.full((H, W), 200, dtype=np.uint8)
        frame[:, :edge_x] = 20
        worker.process(frame, header(uid=str(edge_x)))

    centers = [(int(h["uuid"]), s.center) for s, h in worker.trace("pt")]
    first_dark = next(edge for edge, c in centers if c == 20)
    assert first_dark == 34  # the edge at x=34 covers columns 0..33, so pixel 33 goes dark
    assert all(c == 200 for edge, c in centers if edge < first_dark)


def test_the_patch_mean_falls_in_steps_as_the_edge_crosses_it():
    """The averaged reading: three columns in the patch, so the mask darkens it a third
    at a time, and it is fully dark only once the edge has cleared all three."""
    store = FiducialStore()
    store.set(marker("pt", cross(33, 20)))  # patch columns 32..34
    worker = FiducialStatsWorker(store, queue.Queue(), FiducialStatsConfig(history=100))

    for edge_x in range(0, W):
        frame = np.full((H, W), 200, dtype=np.uint8)
        frame[:, :edge_x] = 20
        worker.process(frame, header(uid=str(edge_x)))

    means = {int(h["uuid"]): s.mean for s, h in worker.trace("pt")}
    assert means[32] == 200 and means[35] == 20
    assert means[33] == pytest.approx((200 * 6 + 20 * 3) / 9)  # one column covered
    assert means[34] == pytest.approx((200 * 3 + 20 * 6) / 9)  # two
    assert means[32] > means[33] > means[34] > means[35]


# --- the circle --------------------------------------------------------------


def test_a_circle_measures_the_pixels_inside_it():
    stats = measure(np.full((H, W), 9, dtype=np.uint8), rasterise(circle(32, 24, 8), H, W))

    assert stats.n_pixels == pytest.approx(np.pi * 8 * 8, rel=0.05)
    assert stats.mean == 9


def test_a_circle_excludes_the_corners_of_its_bounding_square():
    frame = np.zeros((H, W), dtype=np.uint8)
    frame[16:18, 24:26] = 250  # the top-left corner of the 16x16 square around it

    assert measure(frame, rasterise(circle(32, 24, 8), H, W)).max == 0


def test_a_circle_is_symmetric_about_its_centre():
    """A gradient across x: the mean over a circle centred on a pixel boundary is that
    boundary's own value, so a lopsided mask would show as an offset."""
    assert measure(gradient(), rasterise(circle(32, 24, 8), H, W)).mean == pytest.approx(31.5)


def test_a_circle_smaller_than_a_pixel_still_measures_the_pixel_it_is_over():
    stats = measure(gradient(), rasterise(circle(10.9, 5.9, 0.1), H, W))
    assert stats.n_pixels == 1 and stats.mean == 10


def test_a_circle_hanging_off_the_frame_measures_the_part_on_it():
    whole = measure(np.full((H, W), 5, dtype=np.uint8), rasterise(circle(32, 24, 8), H, W))
    clipped = measure(np.full((H, W), 5, dtype=np.uint8), rasterise(circle(0, 24, 8), H, W))
    assert 0 < clipped.n_pixels < whole.n_pixels


def test_a_circle_wholly_off_the_frame_is_not_a_reading_of_zero():
    stats = measure(gradient(), rasterise(circle(500, 500, 8), H, W))
    assert stats.n_pixels == 0 and stats.mean is None and stats.center is None


# --- the centre reading, on every shape --------------------------------------


def test_a_rect_reports_the_pixel_at_its_middle():
    # x 10..20 -> middle 15.0 -> pixel 15
    assert measure(gradient(), rasterise(rect(10, 5, 10, 10), H, W)).center == 15


def test_a_circle_reports_the_pixel_at_its_centre():
    assert measure(gradient(), rasterise(circle(22.4, 24, 6), H, W)).center == 22


def test_a_polygon_reports_the_pixel_at_its_area_centroid():
    # The triangle's centroid is (16.67, 16.67), and the gradient reads its column.
    assert measure(gradient(), rasterise(triangle(), H, W)).center == 16


def test_a_polygon_centroid_is_the_area_centroid_not_the_vertex_mean():
    """An L made of a 20x10 and a 10x20 block: the vertex mean sits at (10, 13.3), the
    area centroid at (7.5, 12.5). They are three columns apart, so the gradient tells
    them apart."""
    ell = PolyShape(kind="poly", points=[Point(x=x, y=y) for x, y in
                                         [(0, 0), (20, 0), (20, 10), (10, 10), (10, 30), (0, 30)]])
    assert measure(gradient(), rasterise(ell, H, W)).center == 7


def test_a_self_intersecting_polygon_still_has_a_centre_inside_its_extent():
    """Drawn by hand in the UI, this one crosses itself and ends on a repeated vertex.
    The shoelace centroid of it is at (-1212, -1560) -- nowhere near the shape -- which
    read as the centre being off the frame and so as no centre reading at all."""
    pts = [(401.8, 307.7), (383.0, 243.4), (303.6, 212.9), (279.8, 377.1), (364.1, 399.1),
           (161.8, 124.9), (243.1, 84.3), (206.5, 165.5), (206.5, 165.5)]
    scribble = PolyShape(kind="poly", points=[Point(x=x, y=y) for x, y in pts])
    frame = np.full((480, 640), 7, dtype=np.uint8)

    x, y = geometric_center(scribble)
    assert min(p[0] for p in pts) <= x <= max(p[0] for p in pts)
    assert min(p[1] for p in pts) <= y <= max(p[1] for p in pts)
    assert measure(frame, rasterise(scribble, 480, 640)).center == 7


def test_a_polygon_with_no_area_still_has_a_centre():
    line = PolyShape(kind="poly", points=[Point(x=10, y=10), Point(x=20, y=10), Point(x=30, y=10)])
    assert measure(gradient(), rasterise(line, H, W)).center == 20


def test_the_centre_is_read_even_where_the_statistics_are_not_representative():
    """Every shape carries the same field, which is what lets a consumer that wants a
    point ignore what was drawn."""
    for shape in (cross(30, 20), circle(30, 20, 5), rect(25, 15, 10, 10), triangle()):
        assert measure(gradient(), rasterise(shape, H, W)).center is not None


def test_a_marker_whose_centre_is_off_the_frame_still_measures_what_is_on_it():
    stats = measure(gradient(), rasterise(rect(W - 4, 0, 40, 4), H, W))  # middle at x = 80
    assert stats.n_pixels == 4 * 4
    assert stats.center is None


def test_colour_centre_is_luminance_too():
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[..., 1] = 200
    assert measure(frame, rasterise(cross(10, 10), H, W)).center == pytest.approx(0.587 * 200)


def test_a_polygon_measures_its_interior():
    triangle = PolyShape(kind="poly", points=[Point(x=10, y=10), Point(x=30, y=10), Point(x=10, y=30)])
    stats = measure(np.full((H, W), 9, dtype=np.uint8), rasterise(triangle, H, W))

    # Half of a 20x20 square, give or take the edge pixels.
    assert 180 < stats.n_pixels < 240
    assert stats.mean == 9


def test_a_polygon_excludes_what_is_outside_it_but_inside_its_bounding_box():
    frame = np.zeros((H, W), dtype=np.uint8)
    frame[25:31, 25:31] = 200  # the empty corner of the triangle's bounding box
    triangle = PolyShape(kind="poly", points=[Point(x=10, y=10), Point(x=30, y=10), Point(x=10, y=30)])

    assert measure(frame, rasterise(triangle, H, W)).max == 0


def test_a_marker_hanging_off_the_frame_measures_the_part_that_is_on_it():
    stats = measure(gradient(), rasterise(rect(W - 4, 0, 10, 2), H, W))
    assert stats.n_pixels == 4 * 2
    assert stats.min == W - 4


def test_a_marker_wholly_off_the_frame_is_not_a_reading_of_zero():
    stats = measure(gradient(), rasterise(rect(500, 500, 10, 10), H, W))
    assert stats.n_pixels == 0
    assert stats.mean is None and stats.std is None and stats.center is None


def test_colour_frames_are_reduced_to_luminance():
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[..., 1] = 200  # pure green, in RGB
    stats = measure(frame, rasterise(rect(0, 0, 8, 8), H, W))
    assert stats.mean == pytest.approx(0.587 * 200)


def test_wide_mono_frames_are_not_clipped_to_8_bit():
    frame = np.full((H, W), 3000, dtype=np.uint16)
    assert measure(frame, rasterise(rect(0, 0, 8, 8), H, W)).mean == 3000


def test_a_shape_that_cannot_mean_anything_is_refused():
    with pytest.raises(ValidationError):
        PolyShape(kind="poly", points=[Point(x=0, y=0), Point(x=1, y=1)])
    with pytest.raises(ValidationError):
        RectShape(kind="rect", x=0, y=0, width=0, height=5)
    with pytest.raises(ValidationError):
        CrossShape(kind="cross", x=0, y=0, size=-1)
    with pytest.raises(ValidationError):
        CircleShape(kind="circle", x=0, y=0, radius=0)
    with pytest.raises(ValidationError):
        CrossShape(kind="cross", x=0, y=0, sample_radius=-1)
    with pytest.raises(ValidationError):
        FiducialMarker(marker_id="", shape=rect(0, 0, 1, 1))


def test_the_shape_is_chosen_by_its_kind_tag():
    parsed = FiducialMarker.model_validate(
        {"marker_id": "m", "shape": {"kind": "poly", "points": [{"x": 0, "y": 0},
                                                                {"x": 5, "y": 0},
                                                                {"x": 0, "y": 5}]}}
    )
    assert isinstance(parsed.shape, PolyShape)

    parsed = FiducialMarker.model_validate(
        {"marker_id": "c", "shape": {"kind": "circle", "x": 5, "y": 6, "radius": 3}}
    )
    assert isinstance(parsed.shape, CircleShape)


# --- the purpose: find the mask by the dip -----------------------------------


def test_a_moving_mask_edge_shows_up_as_a_dip_in_the_marker_trace():
    """A dark mask slides right across a bright scene. A marker in the path sees its
    mean fall as the edge covers it, and the frame where it bottoms out is the frame the
    mask is on top of the marker -- which is the whole calibration idea."""
    store = FiducialStore()
    store.set(marker("edge", rect(30, 10, 6, 20)))
    worker = FiducialStatsWorker(store, queue.Queue(), FiducialStatsConfig(history=100))

    for edge_x in range(0, W, 2):  # the mask's right edge, moving right
        frame = np.full((H, W), 200, dtype=np.uint8)
        frame[:, :edge_x] = 20  # the mask
        worker.process(frame, header(uid=str(edge_x), t=float(edge_x)))

    trace = worker.trace("edge")
    means = [s.mean for s, _ in trace]

    assert means[0] == 200 and means[-1] == 20  # bright before, covered after
    assert all(a >= b for a, b in zip(means, means[1:]))  # only ever falls
    # Half covered exactly when the edge is at the marker's middle (x = 33).
    half = min(range(len(means)), key=lambda i: abs(means[i] - 110))
    assert abs(int(trace[half][1]["uuid"]) - 33) <= 2


# --- persistence -------------------------------------------------------------


def test_markers_survive_a_restart(tmp_path):
    path = tmp_path / "markers.json"
    store = FiducialStore(path)
    store.set(marker("a", rect(1, 2, 3, 4)))
    store.set(marker("b", CrossShape(kind="cross", x=5, y=6)))

    again = FiducialStore(path)
    assert [m.marker_id for m in again.list()] == ["a", "b"]
    assert again.get("a").shape == rect(1, 2, 3, 4)


def test_a_marker_file_saved_with_the_old_cross_thickness_still_loads(tmp_path):
    """`thickness` went away when the cross became a point. A file written before that
    must load, not be shunted aside as corrupt -- the operator's other markers are in it."""
    path = tmp_path / "m.json"
    path.write_text(
        '[{"marker_id": "old", "shape": {"kind": "cross", "x": 5, "y": 6, "size": 8, "thickness": 3}},'
        ' {"marker_id": "r", "shape": {"kind": "rect", "x": 1, "y": 2, "width": 3, "height": 4}}]'
    )

    store = FiducialStore(path)

    assert [m.marker_id for m in store.list()] == ["old", "r"]
    assert not (tmp_path / "m.json.corrupt").exists()
    assert store.get("old").shape.sample_radius == 1  # takes the default patch


def test_setting_an_existing_id_replaces_it(tmp_path):
    store = FiducialStore(tmp_path / "m.json")
    store.set(marker("a", rect(0, 0, 1, 1)))
    store.set(marker("a", rect(9, 9, 2, 2)))
    assert len(store.list()) == 1
    assert store.get("a").shape.x == 9


def test_removal_is_persisted_and_an_unknown_id_is_an_error(tmp_path):
    path = tmp_path / "m.json"
    store = FiducialStore(path)
    store.set(marker("a", rect(0, 0, 1, 1)))
    store.remove("a")
    assert FiducialStore(path).list() == []
    with pytest.raises(KeyError):
        store.remove("a")


def test_a_corrupt_file_costs_the_markers_not_the_node(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('[{"marker_id": "a", "shape": {"kind": "rect"')  # truncated

    store = FiducialStore(path)

    assert store.list() == []
    # Moved aside rather than left in place to be overwritten by the next save.
    assert (tmp_path / "m.json.corrupt").exists()
    store.set(marker("b", rect(0, 0, 1, 1)))
    assert [m.marker_id for m in FiducialStore(path).list()] == ["b"]


def test_a_store_with_no_path_is_memory_only():
    store = FiducialStore(None)
    store.set(marker("a", rect(0, 0, 1, 1)))
    assert len(store.list()) == 1


# --- roles: naming a marker for a purpose -------------------------------------


def test_a_role_points_at_a_marker_and_can_be_repointed():
    store = FiducialStore(None)
    store.set(marker("a", rect(0, 0, 1, 1)))
    store.set(marker("b", rect(1, 1, 1, 1)))

    store.set_role("sample_holder", "a")
    assert store.get_role("sample_holder") == "a"
    assert store.list_roles() == {"sample_holder": "a"}

    store.set_role("sample_holder", "b")  # repointing replaces, does not add
    assert store.get_role("sample_holder") == "b"
    assert store.list_roles() == {"sample_holder": "b"}


def test_an_unset_role_reads_as_none_and_removing_an_unknown_one_is_an_error():
    store = FiducialStore(None)
    assert store.get_role("nope") is None
    with pytest.raises(KeyError):
        store.remove_role("nope")


def test_a_role_may_name_a_marker_that_does_not_exist_yet():
    """Setting the role and drawing the marker can happen in either order -- the store
    does not enforce that the marker_id already exists."""
    store = FiducialStore(None)
    store.set_role("sample_holder", "not-drawn-yet")
    assert store.get_role("sample_holder") == "not-drawn-yet"


def test_removing_a_role_does_not_touch_its_marker():
    store = FiducialStore(None)
    store.set(marker("a", rect(0, 0, 1, 1)))
    store.set_role("sample_holder", "a")
    store.remove_role("sample_holder")
    assert store.get_role("sample_holder") is None
    assert store.get("a") is not None


def test_roles_survive_a_restart(tmp_path):
    path = tmp_path / "m.json"
    store = FiducialStore(path)
    store.set(marker("a", rect(0, 0, 1, 1)))
    store.set_role("sample_holder", "a")

    again = FiducialStore(path)
    assert again.list_roles() == {"sample_holder": "a"}
    assert [m.marker_id for m in again.list()] == ["a"]


def test_a_legacy_bare_list_file_still_loads_with_no_roles(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('[{"marker_id": "a", "shape": {"kind": "rect", "x": 0, "y": 0, "width": 1, "height": 1}}]')

    store = FiducialStore(path)

    assert [m.marker_id for m in store.list()] == ["a"]
    assert store.list_roles() == {}
    assert not (tmp_path / "m.json.corrupt").exists()


def test_a_role_map_that_is_not_a_str_to_str_mapping_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('{"markers": [], "roles": {"sample_holder": 5}}')

    store = FiducialStore(path)

    assert store.list() == []
    assert store.list_roles() == {}
    assert (tmp_path / "m.json.corrupt").exists()


# --- the worker --------------------------------------------------------------


def make_worker(**config) -> tuple[FiducialStore, FiducialStatsWorker]:
    store = FiducialStore()
    return store, FiducialStatsWorker(store, queue.Queue(), FiducialStatsConfig(**config))


def test_a_marker_added_between_frames_is_measured_from_the_next_frame():
    store, worker = make_worker()
    assert worker.process(gradient(), header()) == {}

    store.set(marker("a", rect(0, 0, 4, 4)))
    assert set(worker.process(gradient(), header())) == {"a"}


def test_moving_a_marker_takes_effect_on_the_next_frame():
    store, worker = make_worker()
    store.set(marker("a", rect(0, 0, 4, 4)))
    assert worker.process(gradient(), header())["a"].mean == pytest.approx(1.5)

    store.set(marker("a", rect(40, 0, 4, 4)))
    assert worker.process(gradient(), header())["a"].mean == pytest.approx(41.5)


def test_a_removed_marker_takes_its_trace_with_it():
    store, worker = make_worker()
    store.set(marker("a", rect(0, 0, 4, 4)))
    worker.process(gradient(), header())
    assert worker.trace("a")

    store.remove("a")
    worker.process(gradient(), header())
    assert worker.trace("a") == []


def test_the_trace_is_bounded_and_keeps_the_newest():
    store, worker = make_worker(history=5)
    store.set(marker("a", rect(0, 0, 4, 4)))
    for i in range(20):
        worker.process(gradient(), header(uid=str(i)))

    assert [h["uuid"] for _, h in worker.trace("a")] == ["15", "16", "17", "18", "19"]
    assert [h["uuid"] for _, h in worker.trace("a", limit=2)] == ["18", "19"]


def test_the_stream_queue_drops_the_stale_sample_rather_than_block():
    store, worker = make_worker(queue_size=2)
    for i in range(5):
        worker._put(({}, header(uid=str(i))))
    assert [worker.samples.get_nowait()[1]["uuid"] for _ in range(2)] == ["3", "4"]


def test_the_thread_survives_a_bad_frame():
    store, worker = make_worker(idle_time=0.01)
    store.set(marker("a", rect(0, 0, 4, 4)))
    worker.camera_queue.put((np.zeros((0,), dtype=np.uint8), header(uid="bad")))  # no shape
    worker.camera_queue.put((gradient(), header(uid="good")))
    worker.start()
    try:
        sample = worker.samples.get(timeout=2)
    finally:
        worker.stop()
        worker.join(timeout=2)

    assert sample[1]["uuid"] == "good"
    assert worker.n_failures == 1


# --- the handler -------------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    store = FiducialStore(tmp_path / "m.json")
    worker = FiducialStatsWorker(store, queue.Queue(), FiducialStatsConfig())
    return FiducialHandler(store, worker)


async def test_set_list_and_remove_round_trip_through_the_handler(handler):
    await handler.set_marker(marker("a", rect(1, 2, 3, 4)))

    listed = await handler.list_markers(Empty())
    assert [m.marker_id for m in listed.markers] == ["a"]
    assert listed.frame_width is None  # no frame seen yet

    await handler.remove_marker(MarkerId(marker_id="a"))
    assert (await handler.list_markers(Empty())).markers == []


async def test_list_reports_the_frame_the_markers_are_measured_against(handler):
    handler.worker.process(gradient(), header())
    listed = await handler.list_markers(Empty())
    assert (listed.frame_width, listed.frame_height) == (W, H)


async def test_stats_and_history_come_back_typed_and_stamped(handler):
    await handler.set_marker(marker("a", rect(0, 0, 4, 4)))
    with pytest.raises(RuntimeError):  # nothing measured yet
        await handler.marker_stats(Empty())

    handler.worker.process(gradient(), header(uid="f1", t=12.5))
    latest = await handler.marker_stats(Empty())
    assert latest.uuid == "f1" and latest.time == 12.5
    assert latest.stats["a"].mean == pytest.approx(1.5)

    history = await handler.marker_history(MarkerHistoryQuery(marker_id="a"))
    assert [p.uuid for p in history.samples] == ["f1"]


async def test_history_of_an_unknown_marker_is_an_error(handler):
    with pytest.raises(KeyError):
        await handler.marker_history(MarkerHistoryQuery(marker_id="nope"))


async def test_the_stream_yields_measured_frames_then_idles(handler):
    await handler.set_marker(marker("a", rect(0, 0, 4, 4)))
    assert await handler.next() is None

    stats = handler.worker.process(gradient(), header(uid="f1"))
    handler.worker._put((stats, header(uid="f1")))
    sample = await handler.next()
    assert sample.uuid == "f1" and "a" in sample.stats
    assert await handler.next() is None


async def test_readout_reports_liveness(handler):
    await handler.set_marker(marker("a", rect(0, 0, 4, 4)))
    handler.worker.process(gradient(), header())
    readout = handler.readout()
    assert readout.marker_ids == ["a"] and readout.n_processed == 1


async def test_roles_round_trip_through_the_handler(handler):
    await handler.set_marker(marker("a", rect(0, 0, 4, 4)))
    await handler.set_role(RoleAssignment(role="sample_holder", marker_id="a"))

    roles = await handler.list_roles(Empty())
    assert roles.roles == {"sample_holder": "a"}

    await handler.remove_role(RoleQuery(role="sample_holder"))
    assert (await handler.list_roles(Empty())).roles == {}

    with pytest.raises(KeyError):
        await handler.remove_role(RoleQuery(role="sample_holder"))
