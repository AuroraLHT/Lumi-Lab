"""Camera stills kept with the growth: take_snapshot and the ops that read them back.

T0: a real GrowthDB and file stores in tmp_path, stand-in cameras, the handler called
directly.
"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np
import pydantic
import pytest

from lumi.contracts.payloads.experiment import ListSnapshots, RetireSnapshot, SnapshotId, TakeSnapshot
from lumi.experiment.db import GrowthDB
from lumi.experiment.files import MeasurementFileStore
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration
from lumi.experiment.snapshots import encode_frame


class Camera:
    def __init__(self, frame: np.ndarray):
        self.frame = frame

    async def image(self):
        return None, self.frame


#: A 12-bit RHEED frame in uint16, with one bright spot.
RHEED_FRAME = np.full((48, 64), 100, dtype=np.uint16)
RHEED_FRAME[20:24, 30:34] = 4000
#: The chamber camera, RGB: a red block on black.
CHAMBER_FRAME = np.zeros((30, 40, 3), dtype=np.uint8)
CHAMBER_FRAME[:, :20, 0] = 255


@pytest.fixture
async def handler(tmp_path):
    db = GrowthDB(str(tmp_path / "growth.db"))
    await db.connect()
    await db.create_database()
    h = ExperimentHandler(
        sources={
            **{k: None for k in ("chamber_mi", "chamber_log", "chamber_config", "storage")},
            "rheed_camera": Camera(RHEED_FRAME),
            "chamber_camera": Camera(CHAMBER_FRAME),
        },
        growth_db=db,
        pld_config=PLDChamberConfiguration(
            center_mask_pos=100.0, center_rheed_pos=0.0, plumb_center=0.0,
            target_to_mask_distance=60.16, mask_to_sample_distance=0.0,
            rheed_limit=(-3.0, 3.0), mask_block_position=75.0),
        bounds=ExperimentBounds(
            mask_travel_max=160.0, temperature_min=160.0, temperature_max=1000.0,
            temperature_pid_engage_threshold=220.0, warm_up_current=7.8, warm_up_step=0.1,
            warm_up_current_ramp_rate=0.015, warm_up_wait_interval=0.01,
            warm_up_max_waittime=1.0, motor_ready_timeout=0.5),
        target_mapper={},
        file_store=MeasurementFileStore(tmp_path / "files", max_bytes=100_000),
        snapshot_store=MeasurementFileStore(tmp_path / "snapshots", max_bytes=1_000_000),
    )
    yield h
    await db.close()


async def test_a_snapshot_keeps_the_frame_exactly_and_a_jpeg_to_look_at(handler, tmp_path):
    info = await handler.take_snapshot(TakeSnapshot(camera="rheed", stage="depo_start", note="first pulse"))

    assert (info.camera, info.stage, info.note) == ("rheed", "depo_start", "first pulse")
    assert (info.width, info.height, info.channels, info.dtype) == (64, 48, 1, "uint16")
    assert (info.pixel_min, info.pixel_max) == (100.0, 4000.0)
    assert info.taken_at is not None and info.state == "active"

    _, frame = await handler.snapshot_frame(SnapshotId(snapshot_id=info.snapshot_id))
    assert frame.dtype == np.uint16 and np.array_equal(frame, RHEED_FRAME)

    _, jpeg = await handler.snapshot_jpeg(SnapshotId(snapshot_id=info.snapshot_id))
    assert jpeg[:3] == b"\xff\xd8\xff" and len(jpeg) == info.jpeg_bytes
    # Stretched by the frame's own range, or a 12-bit frame would be all black.
    picture = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert picture[22, 32] > 200 and picture[0, 0] < 30

    # Both files in one folder, named by stage.
    (folder,) = (tmp_path / "snapshots").iterdir()
    assert sorted(p.name for p in folder.iterdir()) == ["depo_start.jpg", "depo_start.npy"]
    row = await handler.growth_db.get_snapshot(info.snapshot_id)
    assert row["raw_sha256"] == hashlib.sha256((folder / "depo_start.npy").read_bytes()).hexdigest()


async def test_a_chamber_snapshot_keeps_its_colours(handler):
    info = await handler.take_snapshot(TakeSnapshot(camera="chamber", stage="heated"))
    assert (info.channels, info.dtype) == (3, "uint8")

    _, jpeg = await handler.snapshot_jpeg(SnapshotId(snapshot_id=info.snapshot_id))
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    # Red stays red: the RGB frame is swapped to BGR for the encoder, not left as is.
    assert bgr[15, 5, 2] > 200 and bgr[15, 5, 0] < 50


async def test_snapshots_list_by_sample_session_camera_and_stage(handler):
    handler.current_sample_id = _returns(7)
    await handler.take_snapshot(TakeSnapshot(camera="chamber", stage="start"))
    await handler.take_snapshot(TakeSnapshot(camera="rheed", stage="depo_start"))
    handler.current_sample_id = _returns(None)
    await handler.take_snapshot(TakeSnapshot(camera="chamber", stage="cooled"))

    everything = (await handler.list_snapshots(ListSnapshots())).snapshots
    assert [s.stage for s in everything] == ["start", "depo_start", "cooled"]
    assert [s.sample_id for s in everything] == [7, 7, None]

    on_sample = (await handler.list_snapshots(ListSnapshots(sample_id=7))).snapshots
    assert len(on_sample) == 2
    rheed = (await handler.list_snapshots(ListSnapshots(camera="rheed"))).snapshots
    assert [s.stage for s in rheed] == ["depo_start"]
    cooled = (await handler.list_snapshots(ListSnapshots(stage="cooled"))).snapshots
    assert len(cooled) == 1


async def test_retiring_hides_a_snapshot_but_keeps_its_files(handler):
    info = await handler.take_snapshot(TakeSnapshot(camera="rheed", stage="other"))
    retired = await handler.retire_snapshot(RetireSnapshot(snapshot_id=info.snapshot_id))
    assert retired.state == "retired"
    assert (await handler.list_snapshots(ListSnapshots())).snapshots == []
    assert len((await handler.list_snapshots(ListSnapshots(include_retired=True))).snapshots) == 1
    _, frame = await handler.snapshot_frame(SnapshotId(snapshot_id=info.snapshot_id))
    assert np.array_equal(frame, RHEED_FRAME)


async def test_stages_are_a_fixed_list():
    with pytest.raises(pydantic.ValidationError):
        TakeSnapshot(camera="rheed", stage="halfway")
    with pytest.raises(pydantic.ValidationError):
        TakeSnapshot(camera="webcam", stage="start")


async def test_refusals(handler):
    handler.sources["chamber_camera"] = None
    with pytest.raises(RuntimeError, match="no chamber camera"):
        await handler.take_snapshot(TakeSnapshot(camera="chamber", stage="start"))
    with pytest.raises(ValueError, match="no snapshot"):
        await handler.snapshot_jpeg(SnapshotId(snapshot_id=99))


def test_a_uniform_frame_is_not_stretched_into_a_pattern():
    encoded = encode_frame(np.full((8, 8), 1234, dtype=np.uint16))
    picture = cv2.imdecode(np.frombuffer(encoded.jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert picture.max() < 5


def _returns(value):
    async def resolve():
        return value
    return resolve
