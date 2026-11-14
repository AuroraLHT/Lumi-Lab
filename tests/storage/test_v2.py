"""Recording format v2: the writer, the reader, the archive over it, the storage
handler end to end with fake sources, and v1 -> v2 conversion.

T0: no broker.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import h5py
import numpy as np
import pytest

from lumi.contracts.payloads.camera import CameraConfig, CameraState
from lumi.contracts.payloads.chamber import LogBatch, LogEntry
from lumi.contracts.payloads.common import Empty, FrameHeader
from lumi.contracts.payloads.detection import CropSetup, DetectedBox, DetectionResult, DetectionState
from lumi.contracts.payloads.rheed import (
    BBox,
    BBoxList,
    IntegrationHeader,
    IntegrationResult,
    IntegrationSample,
    IntegratorState,
)
from lumi.contracts.payloads.storage import (
    ListRecordings,
    RecordingDetectionQuery,
    RecordingDetectionsQuery,
    RecordingEventIn,
    RecordingEventsQuery,
    RecordingFrameQuery,
    RecordingIntegrationQuery,
    RecordingLogQuery,
    RecordingName,
    StorageRequest,
)
from lumi.storage import open_recording
from lumi.storage.archive import RecordingArchive
from lumi.storage.convert import check, convert_v1
from lumi.storage.handlers import ArchiveHandler, StorageHandler
from lumi.storage.record import Recorder, RecorderConfig
from lumi.storage.v2 import (
    DetectionSchema,
    LogSchema,
    RecordingWriter,
    decode_pattern,
    encode_pattern,
    escape,
    pack_mask,
    unpack_mask,
)

T0 = 1_790_000_000.0
H, W = 24, 32
LOG_COLUMNS = ["Time", "HT Temp moni", "Shut Stat", "MFC1/2 moni", "time_stamp", "time"]


def detector_pattern(rng, shape=(H, W)) -> np.ndarray:
    """What the detector sends: a 12-bit frame min-max scaled, in float64."""
    frame = rng.integers(0, 4096, size=shape).astype(np.uint16)
    return (frame - frame.min()) / (frame.max() - frame.min() + 1e-5)


def write_v2(path, *, n_frames=6, interrupted=False):
    rng = np.random.default_rng(0)
    w = RecordingWriter(
        path, frame_shape=(H, W),
        log=LogSchema(["HT Temp moni", "Shut Stat", "MFC1/2 moni"], {"Shut Stat"}),
        integration=True,
        detection=DetectionSchema(["2D", "3D"], ["spot", "streak"], (H, W)),
        meta={"request": {"project_name": "x", "note": "a note", "operator": "ops"},
              "camera": {"config": {"gain": 200}}},
        attrs={"rheed_energy_kev": 30.0},
    )
    patterns, masks = [], []
    for i in range(n_frames):
        t = T0 + i
        w.add_frame(np.full((H, W), 100 * i, np.uint16), t, seq=10 + i)
        w.add_log(t, {"HT Temp moni": 600 + i, "Shut Stat": "0x0001", "MFC1/2 moni": None,
                      "new column": "surprise"})
        # Box 1 moves at i == 3.
        w.add_integration(t, {0: {"mean": 10.0 + i, "max": 50.0, "min": 1.0, "center_x": 3.0,
                                  "center_y": 2.0, "width": 6, "height": 4},
                              1: {"mean": 20.0 + i, "max": 60.0, "min": 2.0,
                                  "center_x": 5.0 if i < 3 else 7.0, "center_y": 2.0,
                                  "width": 6, "height": 4}})
        mask = np.zeros((H, W), bool)
        mask[2 + i:6 + i, 3:9] = True
        mask[4, 12] = True  # a mask need not be one rectangle
        pattern = detector_pattern(rng)
        patterns.append(pattern.astype(np.float32))
        masks.append(mask)
        w.add_detection(t, f"uuid-{i}",
                        [{"bbox_id": 0, "x": 3.0, "y": 2.0 + i, "width": 10.0, "height": 4.0,
                          "score": 0.9, "label": "1"},
                         {"bbox_id": 1, "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0,
                          "score": 0.5, "label": "0"}],
                        {"2D": 0.25, "3D": 0.75}, pattern, {0: mask}, {0: 7}, (0, 0, W, H))
    w.add_event(T0 + 1.5, "task_started", "to_temperature", {"target_c": 600})
    if interrupted:
        w.flush()
        w.h5f.close()  # what a crash leaves: everything flushed, no closing stamp
    else:
        w.close()
    return patterns, masks


def test_round_trip(tmp_path):
    path = tmp_path / "rec.hdf5"
    patterns, masks = write_v2(path)
    with open_recording(path) as rec:
        assert rec.version == 2
        assert rec.attrs["format"] == "lumi-recording" and rec.attrs["n_frames"] == 6
        assert rec.attrs["rheed_energy_kev"] == 30.0 and "closed" in rec.attrs
        assert rec.attrs["start"] == T0 and rec.attrs["end"] == T0 + 5
        assert rec.meta["request"]["note"] == "a note"

        assert len(rec.frames) == 6 and rec.frames[4][0, 0] == 400 and rec.frames[-1][0, 0] == 500
        assert list(rec.frames.times) == [T0 + i for i in range(6)]

        log = rec.log()
        assert list(log.columns) == ["time", "HT Temp moni", "Shut Stat", "MFC1/2 moni"]
        assert list(log["HT Temp moni"]) == [600.0 + i for i in range(6)]
        assert list(log["Shut Stat"]) == ["0x0001"] * 6
        assert np.isnan(log["MFC1/2 moni"]).all()
        assert rec.b.log_extra()[0] == {"new column": "surprise"}

        integ = rec.integration()
        assert len(integ) == 12 and list(rec.integration(1)["mean"]) == [20.0 + i for i in range(6)]
        boxes = rec.boxes()
        moved = boxes[boxes.bbox_id == 1]
        assert list(moved.center_x) == [5.0, 7.0] and list(moved.t_from) == [T0, T0 + 3]
        assert moved.t_to.iloc[0] == T0 + 3 and np.isnan(moved.t_to.iloc[1])

        det = rec.detections()
        assert list(det.n_boxes) == [2] * 6 and list(det.p_3D) == [0.75] * 6
        assert det.uuid[3] == "uuid-3"
        one = rec.detection(3)
        assert one["crop"] == (0, 0, W, H)
        assert [b["track"] for b in one["boxes"]] == [7, -1]
        assert np.array_equal(one["masks"][0], masks[3])
        assert not one["masks"][1].any()  # no mask sent for box 1
        assert np.array_equal(one["pattern"], patterns[3])  # lossless at float32

        ev = rec.events()
        assert list(ev.kind) == ["task_started"] and ev.detail[0] == {"target_c": 600}


def test_an_interrupted_recording_reads_up_to_its_last_flush(tmp_path):
    path = tmp_path / "crash.hdf5"
    write_v2(path, n_frames=3, interrupted=True)
    with open_recording(path) as rec:
        assert "closed" not in rec.attrs
        assert len(rec.frames) == 3 and len(rec.log()) == 3 and len(rec.detections()) == 3
    summary = RecordingArchive(tmp_path).summary("crash")
    assert summary["complete"] is False and summary["n_frames"] == 3


def test_a_live_recording_reads_from_another_process(tmp_path):
    """SWMR: History, or a notebook, can watch a recording while it is being made."""
    path = tmp_path / "live.hdf5"
    w = RecordingWriter(path, frame_shape=(H, W), log=LogSchema(["a", "s"], {"s"}))
    w.add_frame(np.ones((H, W), np.uint16), T0)
    w.add_log(T0, {"a": 1.0, "s": "first"})
    w.flush()
    script = textwrap.dedent(f"""
        from lumi.storage import open_recording
        with open_recording({str(path)!r}, live=True) as rec:
            print(len(rec.frames), list(rec.log()["s"]))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1", "['first']"]
    for i in range(1, 4):
        w.add_frame(np.ones((H, W), np.uint16), T0 + i)
        w.add_log(T0 + i, {"a": 1.0, "s": f"row {i}"})
    w.flush()
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("4 ")
    w.close()


def test_refuses_to_overwrite_unless_asked(tmp_path):
    path = tmp_path / "x.hdf5"
    RecordingWriter(path).close()
    with pytest.raises(FileExistsError):
        RecordingWriter(path)
    RecordingWriter(path, overwrite=True).close()


def test_pattern_encoding_is_exact_or_falls_back(tmp_path):
    rng = np.random.default_rng(1)
    for _ in range(20):
        p = detector_pattern(rng)
        k, denom = encode_pattern(p)
        assert np.array_equal(decode_pattern(k, denom), p.astype(np.float32))
    assert encode_pattern(np.zeros((4, 4)))[0].max() == 0
    # Not k / denom for any denom: kept as float32 instead.
    odd = rng.random((H, W)).astype(np.float32)
    assert encode_pattern(odd) is None
    path = tmp_path / "odd.hdf5"
    w = RecordingWriter(path, detection=DetectionSchema(["a"], [], (H, W)))
    w.add_detection(T0, "u", [], {"a": 1.0}, odd)
    w.add_detection(T0 + 1, "u", [], {"a": 1.0}, None)
    w.close()
    with open_recording(path) as rec:
        assert np.array_equal(rec.detection(0)["pattern"], odd)
        assert rec.detection(1)["pattern"] is None


def test_mask_packing_is_lossless():
    rng = np.random.default_rng(2)
    for _ in range(20):
        m = np.zeros((H, W), bool)
        y, x = rng.integers(0, H - 5), rng.integers(0, W - 5)
        m[y:y + 5, x:x + 5] = rng.random((5, 5)) > 0.4
        bits, y0, x0, h, w = pack_mask(m)
        assert np.array_equal(unpack_mask(np.frombuffer(bits, np.uint8), y0, x0, h, w, (H, W)), m)


def test_log_names_that_hdf5_would_misread_are_escaped():
    assert escape("MFC1/2 moni") == "MFC1%2F2 moni"
    assert escape(".hidden") == "%2Ehidden" and escape("_extra") == "%5Fextra"
    assert escape("HT Temp moni") == "HT Temp moni"


# --- the archive over v2 ----------------------------------------------------------------


async def test_archive_serves_v2(tmp_path):
    write_v2(tmp_path / "s_v2_0001.hdf5")
    handler = ArchiveHandler(RecordingArchive(tmp_path))
    listed = await handler.list_recordings(ListRecordings())
    s = listed.recordings[0]
    assert (s.format_version, s.n_frames, s.n_log_rows, s.n_integrations, s.n_detections) == (2, 6, 6, 6, 6)
    assert (s.complete, s.note, s.operator, s.n_events) == (True, "a note", "ops", 1)

    info = await handler.recording_info(RecordingName(name="s_v2_0001"))
    assert info.frame_shape == [H, W] and info.meta["camera"]["config"]["gain"] == 200
    assert [b.bbox_id for b in info.boxes] == [0, 1] and len(info.box_history) == 3
    assert info.attrs["format_version"] == 2 and info.detection_classes == ["2D", "3D"]

    meta, image = await handler.recording_frame(RecordingFrameQuery(name="s_v2_0001", index=2))
    assert image[0, 0] == 200 and meta.time == T0 + 2 and meta.time_stamp

    log = await handler.recording_log(RecordingLogQuery(
        name="s_v2_0001", columns=["HT Temp moni", "Shut Stat", "missing"]))
    assert log.columns["HT Temp moni"] == [600.0 + i for i in range(6)]
    assert log.columns["Shut Stat"] == ["0x0001"] * 6 and log.columns["missing"] == [None] * 6

    integ = await handler.recording_integration(RecordingIntegrationQuery(name="s_v2_0001", bbox_ids=[1]))
    assert [t.bbox_id for t in integ.traces] == [1] and integ.traces[0].n_points == 6

    ev = await handler.recording_events(RecordingEventsQuery(name="s_v2_0001"))
    assert ev.total == 1 and ev.events[0].kind == "task_started" and ev.events[0].time_iso

    dets = await handler.recording_detections(RecordingDetectionsQuery(name="s_v2_0001", max_points=3))
    assert dets.n_detections == 6 and dets.stride == 2 and dets.index == [0, 2, 4]
    assert dets.classification["2D"] == [0.25] * 3

    one = await handler.recording_detection(RecordingDetectionQuery(name="s_v2_0001", index=5))
    assert one.crop.width == W and one.boxes[0].y0 == 7.0 and one.classification["3D"] == 0.75


async def test_archive_reads_the_recording_in_progress(tmp_path):
    w = RecordingWriter(tmp_path / "live.hdf5", frame_shape=(H, W))
    w.add_frame(np.full((H, W), 7, np.uint16), T0)
    archive = RecordingArchive(tmp_path, live_name=lambda: "live", live_writer=lambda: w)
    handler = ArchiveHandler(archive)
    summary = (await handler.list_recordings(ListRecordings())).recordings[0]
    assert summary.recording and summary.n_frames == 1 and summary.complete is False
    _, image = await handler.recording_frame(RecordingFrameQuery(name="live", index=0))
    assert image[0, 0] == 7
    w.add_frame(np.full((H, W), 8, np.uint16), T0 + 1)
    _, image = await handler.recording_frame(RecordingFrameQuery(name="live", index=1))
    assert image[0, 0] == 8
    w.close()


# --- the storage handler, end to end --------------------------------------------------------


class FakeSource:
    def __init__(self, state, **ops):
        self.state, self.ops, self.callback, self.target = state, ops, None, "fake"

    async def get_state(self):
        return self.state

    async def start_streaming(self):
        pass

    async def unsubscribe_stream(self):
        self.callback = None

    def __getattr__(self, name):
        if name.startswith("on_"):
            async def subscribe(cb):
                self.callback = cb
            return subscribe
        if name in self.ops:
            async def op(*_):
                return self.ops[name]
            return op
        raise AttributeError(name)


def fake_sources():
    from lumi.contracts.payloads.chamber import LogEntry
    return {
        "camera": FakeSource(CameraState(frame_dims=[H, W]), get_camera_config=CameraConfig(gain=200)),
        "log": FakeSource(type("S", (), {"columns": ["HT Temp moni", "Shut Stat", "time"]})(),
                          log=LogBatch(entries=[LogEntry(time=T0, time_stamp="x",
                                                         values={"HT Temp moni": 1.0, "Shut Stat": "0x0"})])),
        "integrator": FakeSource(IntegratorState(registered_bboxes=[0]),
                                 bboxes=BBoxList(bboxes={0: BBox(x=1, y=2, width=3, height=4)})),
        "detection": FakeSource(DetectionState(classifier_classes=["2D"],
                                               crop=CropSetup(x=0, y=0, width=W, height=H))),
    }


async def test_storage_handler_records_every_stream(tmp_path, monkeypatch):
    import lumi.storage.handlers as handlers
    monkeypatch.setattr(handlers, "FRAME_RATE_LIMIT", 1e6)
    monkeypatch.setattr(handlers, "DETECTION_RATE_LIMIT", 1e6)
    sources = fake_sources()
    handler = StorageHandler(sources, root_folder=str(tmp_path))
    status = await handler.start_recording(StorageRequest(
        project_name="growth", save_frame=True, save_log=True, save_integration=True, save_ai=True,
        note="first", operator="ops", context={"project_id": 3, "pixel": 1}))
    assert status.ok, status.message

    rng = np.random.default_rng(3)
    for i in range(4):
        t = T0 + i
        await sources["camera"].callback(FrameHeader(time=t, uuid="f", time_stamp="s", frame_idx=i),
                                         np.full((H, W), i, np.uint16))
        await sources["log"].callback(LogEntry(time=t, time_stamp="s",
                                               values={"HT Temp moni": 500.0 + i, "Shut Stat": "0x1"}), None)
        await sources["integrator"].callback(IntegrationSample(
            header=IntegrationHeader(time=t, uuid="u", time_stamp="s", bbox_id=0, integration_uuid="i"),
            results={0: IntegrationResult(mean=i, max=9, min=0, height=4, width=3, center_x=2.5,
                                          center_y=4.0)}), None)
        mask = np.zeros((H, W), bool)
        mask[1:3, 1:4] = True
        await sources["detection"].callback(
            DetectionResult(time=t, time_stamp="s", uuid=f"d{i}", classification={"2D": 0.5},
                            boxes=[DetectedBox(bbox_id=0, x=1, y=1, width=3, height=2, score=0.8,
                                               label="2", mask_ref="mask_0")],
                            region2tracks={"0": 4}, pattern_ref="pattern"),
            {"pattern": detector_pattern(rng), "mask_0": mask})
    assert (await handler.add_recording_event(RecordingEventIn(kind="task_started", message="x"))).ok
    status = await handler.stop_recording(Empty())
    assert status.ok and handler.writer is None
    assert not (await handler.add_recording_event(RecordingEventIn(kind="late"))).ok

    with open_recording(tmp_path / "growth.hdf5") as rec:
        assert rec.meta["experiment"] == {"project_id": 3, "pixel": 1}
        assert rec.meta["request"]["note"] == "first" and "context" not in rec.meta["request"]
        assert rec.meta["camera"]["config"]["gain"] == 200
        assert rec.meta["integrator"]["bboxes"]["bboxes"]["0"]["width"] == 3
        assert rec.meta["detection"]["crop"]["width"] == W
        assert rec.meta["log"]["text_columns"] == ["Shut Stat"]
        assert len(rec.frames) == 4 and list(rec.b.frame_times()) == [T0 + i for i in range(4)]
        assert list(rec.f["frame/seq"][:]) == [0, 1, 2, 3]
        assert list(rec.log()["HT Temp moni"]) == [500.0 + i for i in range(4)]
        assert list(rec.integration()["mean"]) == [0.0, 1.0, 2.0, 3.0]
        assert rec.detection(2)["boxes"][0]["track"] == 4 and rec.detection(2)["masks"][0].sum() == 6
        kinds = list(rec.events().kind)
        assert kinds[0] == "recording_started" and "task_started" in kinds
        assert kinds[-1] == "recording_stopped"


async def test_storage_refuses_a_second_recording(tmp_path):
    handler = StorageHandler(fake_sources(), root_folder=str(tmp_path))
    assert (await handler.start_recording(StorageRequest(project_name="a", save_frame=True))).ok
    again = await handler.start_recording(StorageRequest(project_name="b", save_frame=True))
    assert not again.ok and "already recording" in again.message
    await handler.stop_recording(Empty())
    exists = await handler.start_recording(StorageRequest(project_name="a", save_frame=True))
    assert not exists.ok and "force_rewrite" in exists.message


# --- v1 -> v2 -----------------------------------------------------------------------------------


def write_v1(root, name="old"):
    rng = np.random.default_rng(4)
    rec = Recorder(RecorderConfig(
        project_name=name, root_folder=str(root), frame_dim=(H, W),
        frame_meta_columns=["time_stamp", "time"], log_columns=LOG_COLUMNS,
        pattern_dim=(H, W), detection_meta_columns=["time", "time_stamp", "uuid"],
        detector_classes=["a", "b"], classifier_classes=["2D", "3D"], initial_size=50,
        save_frame=True, save_log=True, save_ai=True, save_integration=True,
        compression="lzf", scaleoffset=0, attrs={"rheed_energy_kev": 21.0},
    ))
    assert rec.create_datasets()["succ"]
    for i in range(5):
        t = T0 + i
        rec.save_frame(rng.integers(0, 4096, (H, W)).astype(np.uint16), {"time_stamp": "s", "time": t})
        rec.save_integrations_buffered(
            {b: {"mean": 10.0 * b + i, "max": 50.0, "min": 1.0, "height": 4, "width": 6,
                 "center_x": 3.0 + b, "center_y": 2.0} for b in (0, 1)},
            {"time_stamp": "s", "time": t})
        rec.save_log({"Time": "x", "HT Temp moni": 600 + i, "Shut Stat": "0x0001",
                      "MFC1/2 moni": "", "time_stamp": "x", "time": t})
        mask = np.zeros((H, W), bool)
        mask[i:i + 3, 2:5] = True
        rec.save_prediction(
            pattern=detector_pattern(rng).astype(np.float32), masks=np.stack([mask, ~mask]),
            bboxes=np.array([[2, i, 5, i + 3], [0, 0, W, H]], np.float32),
            labels=np.array([1, 0], np.float32), scores=np.array([0.9, 0.4], np.float32),
            cls_result={"2D": 0.1, "3D": 0.9}, tracking={"0": 3, "1": 5},
            detection_meta={"time": t, "time_stamp": "2026-11-13 12:00:00", "uuid": f"u{i}"})
    rec.close_h5()
    return root / f"{name}.hdf5"


def test_convert_v1_is_lossless_and_leaves_the_original(tmp_path):
    src = write_v1(tmp_path)
    before = src.read_bytes()
    dst = convert_v1(src)
    assert dst.name == "old.v2.hdf5" and src.read_bytes() == before
    with open_recording(dst) as rec:
        assert rec.version == 2 and rec.attrs["rheed_energy_kev"] == 21.0
        assert rec.meta["converted_from"]["format_version"] == 1
        assert len(rec.frames) == 5 and rec.integration().shape[0] == 10
        assert list(rec.log()["Shut Stat"]) == ["0x0001"] * 5
        assert [b["track"] for b in rec.detection(2)["boxes"]] == [3, 5]
    check(src, dst)  # and again, explicitly
    assert RecordingArchive(tmp_path).summary("old.v2")["format_version"] == 2
    with pytest.raises(FileExistsError):
        convert_v1(src)


def test_convert_v1_reports_a_bad_copy(tmp_path):
    src = write_v1(tmp_path)
    dst = convert_v1(src)
    with h5py.File(dst, "r+") as f:
        f["frame/data"][1, 0, 0] += 1
    with pytest.raises(AssertionError, match="frames"):
        check(src, dst)


def test_the_v1_detection_reader_no_longer_needs_crop_columns(tmp_path):
    from lumi.storage.record import RecordReader
    src = write_v1(tmp_path)
    with RecordReader(src) as r:
        items = list(r.iter_detection_dataset())
    assert len(items) == 5 and items[0][4]["uuid"] == "u0"


def test_meta_is_plain_json(tmp_path):
    path = tmp_path / "m.hdf5"
    RecordingWriter(path, meta={"request": {"a": 1}, "skip": None}).close()
    with h5py.File(path, "r") as f:
        assert json.loads(f["meta/request"][()]) == {"a": 1} and "skip" not in f["meta"]
