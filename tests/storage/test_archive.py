"""Reading finished recordings back (`storage.archive`).

T0: no broker. Every file here is written by the real `Recorder`, so these tests fail
if the reader and the writer ever disagree about the layout.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from lumi.contracts.payloads.storage import (
    ListRecordings,
    RecordingFrameQuery,
    RecordingIntegrationQuery,
    RecordingJpegQuery,
    RecordingLogQuery,
    RecordingName,
)
from lumi.storage.archive import RecordingArchive, RecordingNotFound
from lumi.storage.handlers import ArchiveHandler
from lumi.storage.record import Recorder, RecorderConfig

T0 = 1_790_000_000.0
LOG_COLUMNS = ["Time", "HT Temp moni", "Shut Stat", "time_stamp", "time"]
N_FRAMES = 12


def write_recording(root, name, n_frames=N_FRAMES, n_log=5, boxes=(0, 1)) -> None:
    rec = Recorder(RecorderConfig(
        project_name=name, root_folder=str(root), frame_dim=(20, 30),
        frame_meta_columns=["time_stamp", "time"], log_columns=LOG_COLUMNS,
        pattern_dim=None, detection_meta_columns=None, detector_classes=None,
        classifier_classes=None, initial_size=100,
        save_frame=True, save_log=True, save_ai=False, save_integration=True,
    ))
    assert rec.create_datasets()["succ"]
    for i in range(n_frames):
        t = T0 + i
        # Frame i is flat at 100*i, so a test can tell which one it got back.
        rec.save_frame(np.full((20, 30), 100 * i, dtype=np.uint16),
                       {"time_stamp": f"stamp-{i}", "time": t})
        rec.save_integrations_buffered(
            {b: {"mean": 10.0 * b + i, "max": 50.0, "min": 1.0, "height": 4, "width": 6,
                 "center_x": 3.0 + b, "center_y": 2.0} for b in boxes},
            {"time_stamp": f"stamp-{i}", "time": t},
        )
    for i in range(n_log):
        rec.save_log({"Time": "x", "HT Temp moni": 600 + i, "Shut Stat": "0x0001",
                      "time_stamp": "x", "time": T0 + 2 * i})
    rec.close_h5()


@pytest.fixture
def root(tmp_path):
    write_recording(tmp_path, "sample-a_proj_0001")
    write_recording(tmp_path, "sample-b_proj_0002", n_frames=3)
    return tmp_path


@pytest.fixture
def handler(root):
    return ArchiveHandler(RecordingArchive(root))


async def test_lists_recordings_with_written_counts_not_allocated_ones(handler):
    out = await handler.list_recordings(ListRecordings())
    assert out.total == 2
    a = next(r for r in out.recordings if r.name == "sample-a_proj_0001")
    # Datasets are pre-allocated to 100 rows; the counts are what was written.
    assert (a.n_frames, a.n_log_rows, a.n_integrations) == (N_FRAMES, 5, N_FRAMES)
    assert a.start == T0 and a.end == T0 + N_FRAMES - 1
    assert a.start_iso.endswith("Z") and a.modified_iso


async def test_search_and_paging(handler):
    out = await handler.list_recordings(ListRecordings(search="SAMPLE-B"))
    assert [r.name for r in out.recordings] == ["sample-b_proj_0002"]
    page = await handler.list_recordings(ListRecordings(limit=1, offset=1))
    assert page.total == 2 and len(page.recordings) == 1


async def test_info_gives_a_scrubber_what_it_needs(handler):
    info = await handler.recording_info(RecordingName(name="sample-a_proj_0001"))
    assert info.frame_shape == [20, 30] and info.frame_dtype == "uint16"
    assert info.frame_times == [T0 + i for i in range(N_FRAMES)]
    assert info.log_columns == LOG_COLUMNS
    assert [(b.bbox_id, b.center_x) for b in info.boxes] == [(0, 3.0), (1, 4.0)]
    assert info.datasets["frame"] == N_FRAMES


async def test_frame_by_index_is_lossless(handler):
    meta, image = await handler.recording_frame(RecordingFrameQuery(name="sample-a_proj_0001", index=7))
    assert image.dtype == np.uint16 and image.shape == (20, 30) and int(image[0, 0]) == 700
    assert meta.time == T0 + 7 and meta.time_stamp == "stamp-7" and meta.n_frames == N_FRAMES


async def test_a_frame_past_the_end_is_refused(handler):
    with pytest.raises(IndexError):
        await handler.recording_frame(RecordingFrameQuery(name="sample-a_proj_0001", index=N_FRAMES))


async def test_jpeg_contrast_is_fixed_per_recording_not_per_frame(handler):
    q = dict(name="sample-a_proj_0001")
    m1, jpg1 = await handler.recording_frame_jpeg(RecordingJpegQuery(**q, index=1))
    m2, jpg2 = await handler.recording_frame_jpeg(RecordingJpegQuery(**q, index=10))
    assert (m1.low, m1.high) == (m2.low, m2.high)
    # Same mapping, so a brighter raw frame is a brighter picture.
    img1 = cv2.imdecode(np.frombuffer(jpg1, np.uint8), cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imdecode(np.frombuffer(jpg2, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img2.mean() > img1.mean()
    assert (m1.width, m1.height) == (30, 20)


async def test_jpeg_takes_an_explicit_mapping_and_a_max_width(handler):
    meta, jpg = await handler.recording_frame_jpeg(RecordingJpegQuery(
        name="sample-a_proj_0001", index=5, low=0, high=500, max_width=16))
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert (meta.low, meta.high) == (0, 500)
    assert img.shape == (meta.height, meta.width) and meta.width == 16
    assert abs(int(img.mean()) - 255) <= 3  # 500 of 0..500 is white


async def test_integration_is_one_trace_per_box(handler):
    out = await handler.recording_integration(RecordingIntegrationQuery(name="sample-a_proj_0001"))
    traces = {t.bbox_id: t for t in out.traces}
    assert set(traces) == {0, 1}
    assert traces[1].mean == [10.0 + i for i in range(N_FRAMES)]
    assert traces[1].time == [T0 + i for i in range(N_FRAMES)]


async def test_integration_filters_and_thins(handler):
    out = await handler.recording_integration(RecordingIntegrationQuery(
        name="sample-a_proj_0001", bbox_ids=[0], since=T0 + 2, max_points=5))
    (trace,) = out.traces
    assert trace.bbox_id == 0 and trace.n_points == N_FRAMES - 2
    assert trace.stride == 2 and trace.time == [T0 + 2 + 2 * k for k in range(5)]


async def test_log_comes_back_as_typed_columns(handler):
    out = await handler.recording_log(RecordingLogQuery(
        name="sample-a_proj_0001", columns=["HT Temp moni", "Shut Stat", "Not A Column"]))
    assert out.time == [T0 + 2 * i for i in range(5)]
    assert out.columns["HT Temp moni"] == [600.0, 601.0, 602.0, 603.0, 604.0]
    assert out.columns["Shut Stat"] == ["0x0001"] * 5
    assert out.columns["Not A Column"] == [None] * 5


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", ".hidden", "", "x\\y"])
async def test_names_outside_the_archive_are_refused(handler, name):
    with pytest.raises(ValueError):
        await handler.recording_info(RecordingName(name=name))


async def test_an_unknown_name_is_not_found(handler):
    with pytest.raises(RecordingNotFound):
        await handler.recording_info(RecordingName(name="nope"))


async def test_the_file_being_recorded_is_listed_but_not_opened(root):
    handler = ArchiveHandler(RecordingArchive(root, live_name=lambda: "sample-b_proj_0002"))
    listed = {r.name: r for r in (await handler.list_recordings(ListRecordings())).recordings}
    assert listed["sample-b_proj_0002"].recording is True
    assert listed["sample-b_proj_0002"].n_frames is None
    with pytest.raises(RuntimeError, match="still being recorded"):
        await handler.recording_frame(RecordingFrameQuery(name="sample-b_proj_0002", index=0))


async def test_an_unreadable_file_is_listed_with_its_error(root, handler):
    (root / "crashed.hdf5").write_bytes(b"not hdf5 at all")
    listed = {r.name: r for r in (await handler.list_recordings(ListRecordings())).recordings}
    assert listed["crashed"].error and listed["sample-a_proj_0001"].error is None


def test_readout_counts_recordings(handler, root):
    assert handler.readout().n_recordings == 2
    assert handler.readout().root_folder == str(root)


def test_closing_a_recording_keeps_its_last_integrations(tmp_path):
    """The recorder buffers integrations and writes them ten at a time. It used to drop
    the last partial buffer on close, so every recording lost its final seconds of
    RHEED intensity -- 12 events came back as 10."""
    write_recording(tmp_path, "tail", n_frames=13)
    out = RecordingArchive(tmp_path).integration("tail", None, None, None, 1000)
    assert [t["n_points"] for t in out["traces"]] == [13, 13]
