"""Measurements that are more than a number: curves (`series`) and attached files.

T0: a real GrowthDB and file store in tmp_path, the handler called directly.
"""

from __future__ import annotations

import hashlib
import json

import pydantic
import pytest

from lumi.contracts.payloads.experiment import (
    AddMeasurement,
    AttachMeasurementFile,
    ListMeasurements,
    MeasurementFileId,
    MeasurementId,
    MeasurementSeries,
    RetireMeasurementFile,
    SeriesAxis,
    UpdateMeasurement,
)
from lumi.experiment.db import GrowthDB
from lumi.experiment.files import MeasurementFileStore, safe_name
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
#: Stands in for an instrument file nothing here can parse (an Asylum .ibw).
IBW = bytes(range(256)) * 40


@pytest.fixture
async def handler(tmp_path):
    db = GrowthDB(str(tmp_path / "growth.db"))
    await db.connect()
    await db.create_database()
    substrate = await db.add_substrate("SrTiO3", "001", 5, 5, 0.5, 1.0, positions="[0.0]")
    await db.add_sample(substrate, kind="position", pixel_index=0, position_mm=0.0)
    h = ExperimentHandler(
        sources={k: None for k in ("chamber_mi", "chamber_log", "chamber_config", "rheed_camera", "storage")},
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
    )
    yield h
    await db.close()


def xrd(n=5, name="theta-2theta") -> MeasurementSeries:
    return MeasurementSeries(
        name=name,
        x=SeriesAxis(name="2theta", unit="deg", values=[40.0 + i for i in range(n)]),
        y=[SeriesAxis(name="intensity", unit="counts", values=[float(i * i) for i in range(n)])],
        meta={"step_s": 0.5},
    )


async def add(handler, **kw) -> int:
    req = AddMeasurement(sample_id=1, kind=kw.pop("kind", "xrd"), **kw)
    return (await handler.add_measurement(req)).measurement_id


# --- series ------------------------------------------------------------------------


async def test_a_measurement_can_be_curves_with_no_scalar_at_all(handler):
    mid = await add(handler, series=[xrd()], source="Bruker D8")
    got = await handler.get_measurement(MeasurementId(measurement_id=mid))
    assert got.value is None
    assert got.n_series == 1 and got.series[0] == xrd()
    assert got.series[0].x.unit == "deg" and got.series[0].y[0].values[-1] == 16.0


async def test_listing_leaves_the_curves_out_unless_asked(handler):
    await add(handler, value=0.12, series=[xrd(), xrd(name="rocking")])
    plain = (await handler.list_measurements(ListMeasurements())).measurements[0]
    assert plain.series == [] and plain.n_series == 2 and plain.value == 0.12
    full = (await handler.list_measurements(ListMeasurements(include_series=True))).measurements[0]
    assert [s.name for s in full.series] == ["theta-2theta", "rocking"]


def test_every_y_column_matches_x():
    with pytest.raises(pydantic.ValidationError, match="has 2 values, x has 3"):
        MeasurementSeries(name="bad", x=SeriesAxis(name="T", values=[1, 2, 3]),
                          y=[SeriesAxis(name="R", values=[1, 2])])


def test_a_series_needs_a_y_column():
    with pytest.raises(pydantic.ValidationError):
        MeasurementSeries(name="bad", x=SeriesAxis(name="T", values=[1]), y=[])


def test_too_many_points_is_told_to_use_a_file():
    with pytest.raises(pydantic.ValidationError, match="attach the data as a file"):
        AddMeasurement(sample_id=1, kind="afm", series=[xrd(n=120_000)])


async def test_updating_series_replaces_them_and_leaving_it_out_keeps_them(handler):
    mid = await add(handler, series=[xrd()])
    await handler.update_measurement(UpdateMeasurement(measurement_id=mid, value=1.5))
    kept = await handler.get_measurement(MeasurementId(measurement_id=mid))
    assert kept.n_series == 1 and kept.value == 1.5
    await handler.update_measurement(UpdateMeasurement(measurement_id=mid, series=[xrd(name="new")]))
    replaced = await handler.get_measurement(MeasurementId(measurement_id=mid))
    assert [s.name for s in replaced.series] == ["new"]


async def test_scalar_only_measurements_are_unchanged(handler):
    mid = await add(handler, kind="rheed_metric", value=0.7, detail={"period": 50})
    got = await handler.get_measurement(MeasurementId(measurement_id=mid))
    assert (got.value, got.detail, got.series, got.files) == (0.7, {"period": 50}, [], [])


# --- files -------------------------------------------------------------------------


async def test_an_attached_file_comes_back_byte_for_byte(handler, tmp_path):
    mid = await add(handler, kind="afm", value=0.4)
    info = await handler.attach_measurement_file(
        AttachMeasurementFile(measurement_id=mid, file_name="STO_0001.ibw", role="raw",
                              meta={"scan_um": 5}), IBW)
    assert info.size_bytes == len(IBW) and info.sha256 == hashlib.sha256(IBW).hexdigest()
    assert info.media_type == "application/octet-stream"  # nothing knows .ibw; kept anyway
    assert info.meta == {"scan_um": 5}

    meta, data = await handler.measurement_file(MeasurementFileId(file_id=info.file_id))
    assert data == IBW and meta.file_name == "STO_0001.ibw"
    # On disk under its own name, in a folder of its own.
    (on_disk,) = (tmp_path / "files").glob("*/STO_0001.ibw")
    assert on_disk.read_bytes() == IBW


async def test_images_are_typed_so_a_ui_can_show_them(handler):
    mid = await add(handler, kind="afm")
    info = await handler.attach_measurement_file(
        AttachMeasurementFile(measurement_id=mid, file_name="topo.png", role="image"), PNG)
    assert info.media_type == "image/png" and info.role == "image"
    got = await handler.get_measurement(MeasurementId(measurement_id=mid))
    assert [f.file_name for f in got.files] == ["topo.png"]


async def test_two_uploads_with_one_name_do_not_collide(handler):
    mid = await add(handler, kind="xrd")
    a = await handler.attach_measurement_file(AttachMeasurementFile(measurement_id=mid, file_name="scan.raw"), b"one")
    b = await handler.attach_measurement_file(AttachMeasurementFile(measurement_id=mid, file_name="scan.raw"), b"two")
    assert (await handler.measurement_file(MeasurementFileId(file_id=a.file_id)))[1] == b"one"
    assert (await handler.measurement_file(MeasurementFileId(file_id=b.file_id)))[1] == b"two"


async def test_retiring_a_file_hides_it_but_keeps_the_bytes(handler):
    mid = await add(handler, kind="xrd")
    info = await handler.attach_measurement_file(AttachMeasurementFile(measurement_id=mid, file_name="x.raw"), b"data")
    retired = await handler.retire_measurement_file(RetireMeasurementFile(file_id=info.file_id))
    assert retired.state == "retired"
    assert (await handler.get_measurement(MeasurementId(measurement_id=mid))).files == []
    assert (await handler.measurement_file(MeasurementFileId(file_id=info.file_id)))[1] == b"data"
    restored = await handler.retire_measurement_file(RetireMeasurementFile(file_id=info.file_id, retire=False))
    assert restored.state == "active"


async def test_refusals_write_nothing(handler, tmp_path):
    mid = await add(handler, kind="xrd")
    with pytest.raises(ValueError, match="limit"):
        await handler.attach_measurement_file(
            AttachMeasurementFile(measurement_id=mid, file_name="big.raw"), b"x" * 100_001)
    with pytest.raises(ValueError, match="empty"):
        await handler.attach_measurement_file(AttachMeasurementFile(measurement_id=mid, file_name="e.raw"), b"")
    with pytest.raises(ValueError, match="no measurement"):
        await handler.attach_measurement_file(AttachMeasurementFile(measurement_id=999, file_name="a.raw"), b"x")
    assert not (tmp_path / "files").exists() or not any((tmp_path / "files").iterdir())


@pytest.mark.parametrize("given, stored", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Data\\scan 01.raw", "scan 01.raw"),
    ('a<b>:"c".txt', "a_b___c_.txt"),
    ("...", "file"),
])
def test_upload_names_are_made_safe(given, stored):
    assert safe_name(given) == stored


async def test_a_growth_db_from_before_series_gains_the_column(tmp_path):
    db = GrowthDB(str(tmp_path / "old.db"))
    await db.connect()
    await db.conn.execute(
        "CREATE TABLE measurement (measurement_id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL,"
        " step_id INTEGER, experiment_id INTEGER, kind VARCHAR(50) NOT NULL, value FLOAT,"
        " detail TEXT, source VARCHAR(100), record_id INTEGER,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.conn.execute("INSERT INTO measurement (sample_id, kind, value) VALUES (1, 'xrd', 0.1)")
    await db.conn.commit()
    await db.create_database()
    mid = await db.add_measurement(1, "xrd", series=json.dumps([xrd().model_dump()]))
    rows = {r["measurement_id"]: r for r in await db.list_rows("measurement")}
    assert rows[1]["series"] is None and json.loads(rows[mid]["series"])[0]["name"] == "theta-2theta"
    await db.close()
