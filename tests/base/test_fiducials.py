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
    measure,
    rasterise,
)
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.fiducial import (
    CrossShape,
    FiducialMarker,
    MarkerHistoryQuery,
    MarkerId,
    Point,
    PolyShape,
    RectShape,
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


def test_a_cross_measures_its_arms_not_the_square_it_spans():
    """The cross is a thin sampling line. Fill the square's corners with a value the
    arms do not have: if they leaked in, the max would show it."""
    frame = np.zeros((H, W), dtype=np.uint8)
    frame[20, 30] = 100  # the centre, where the arms cross
    frame[16, 26] = 255  # a corner of the square the arms span, on neither arm

    region = rasterise(CrossShape(kind="cross", x=30, y=20, size=5, thickness=1), H, W)
    stats = measure(frame, region)

    assert stats.max == 100  # the corner's 255 is not under the cross
    assert stats.n_pixels == 2 * 11 - 1  # two arms of 11 pixels sharing the centre


def test_a_thicker_cross_covers_more_pixels():
    thin = measure(gradient(), rasterise(CrossShape(kind="cross", x=30, y=20, size=6), H, W))
    thick = measure(
        gradient(), rasterise(CrossShape(kind="cross", x=30, y=20, size=6, thickness=3), H, W)
    )
    assert thick.n_pixels > thin.n_pixels


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
    assert stats.mean is None and stats.std is None


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
        FiducialMarker(marker_id="", shape=rect(0, 0, 1, 1))


def test_the_shape_is_chosen_by_its_kind_tag():
    parsed = FiducialMarker.model_validate(
        {"marker_id": "m", "shape": {"kind": "poly", "points": [{"x": 0, "y": 0},
                                                                {"x": 5, "y": 0},
                                                                {"x": 0, "y": 5}]}}
    )
    assert isinstance(parsed.shape, PolyShape)


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
