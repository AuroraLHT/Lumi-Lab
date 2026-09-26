"""The chamber's past logs, read back by time (`chamber.log` list_log_files / log_window).

The log's `Time` column has no date, so most of what can go wrong is dating: a file
that crosses midnight, one that runs for days, one whose name does not carry a date.
The CSVs here are rendered by the simulator's own formatter, i.e. PASCAL's format.
"""

from __future__ import annotations

import csv
import datetime as dt
import os

import pytest

from lumi.contracts.payloads.chamber import ListLogFiles, LogWindowQuery
from lumi.pascal.handlers import ChamberLogHandler
from lumi.pascal.log_archive import LogArchive
from lumi.pascal.sim import LOG_COLUMNS, ChamberModel, render_row


def write_log(folder, name, start: dt.datetime, seconds: int, step: int = 1, mtime=True):
    model = ChamberModel()
    path = folder / name
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(LOG_COLUMNS)
        for i in range(0, seconds, step):
            values = model.snapshot()
            values["HT Temp set"] = 200 + i  # a column that says which row this is
            out.writerow(render_row(values, start + dt.timedelta(seconds=i)))
    if mtime:
        end = (start + dt.timedelta(seconds=seconds - step)).timestamp()
        os.utime(path, (end, end))
    return path


@pytest.fixture
def folder(tmp_path):
    write_log(tmp_path, "chamber_log_20260901_100000.csv", dt.datetime(2026, 9, 1, 10, 0, 0), 600)
    write_log(tmp_path, "chamber_log_20260901_101500.csv", dt.datetime(2026, 9, 1, 10, 15, 0), 600)
    return tmp_path


@pytest.fixture
def handler(folder):
    return ChamberLogHandler(None, None, LogArchive(folder))


def epoch(*args) -> float:
    return dt.datetime(*args).timestamp()


@pytest.mark.asyncio
async def test_files_are_dated_from_their_names(handler):
    out = await handler.list_log_files(ListLogFiles())
    assert out.total == 2
    newest, oldest = out.files
    assert newest.name == "chamber_log_20260901_101500.csv"
    assert oldest.n_rows == 600
    assert oldest.start == epoch(2026, 9, 1, 10, 0, 0)
    assert oldest.end == epoch(2026, 9, 1, 10, 9, 59)
    assert oldest.start_iso.endswith("Z") and oldest.live is False


@pytest.mark.asyncio
async def test_a_window_spans_files(handler):
    out = await handler.log_window(LogWindowQuery(
        since=epoch(2026, 9, 1, 10, 9, 0), until=epoch(2026, 9, 1, 10, 16, 0),
        columns=["HT Temp set", "Shut Stat"]))
    # 60 s from the end of the first file, nothing in the gap, 61 s of the second.
    assert out.n_rows == 60 + 61
    assert out.time == sorted(out.time)
    assert out.columns["HT Temp set"][0] == 200 + 540.0
    assert out.columns["Shut Stat"][0] == "0x0000"  # status words stay text


@pytest.mark.asyncio
async def test_a_window_is_thinned_to_max_points(handler):
    out = await handler.log_window(LogWindowQuery(file="chamber_log_20260901_100000.csv", max_points=100))
    assert out.n_rows == 600 and out.stride == 6 and len(out.time) == 100


@pytest.mark.asyncio
async def test_no_file_and_no_window_reads_the_newest_file(handler):
    out = await handler.log_window(LogWindowQuery(columns=["HT Temp set"]))
    assert out.time[0] == epoch(2026, 9, 1, 10, 15, 0)


@pytest.mark.asyncio
async def test_default_columns_and_missing_ones(handler):
    out = await handler.log_window(LogWindowQuery(file="chamber_log_20260901_100000.csv",
                                                  columns=["HT Temp moni", "Nope"]))
    assert all(isinstance(v, float) for v in out.columns["HT Temp moni"])
    assert set(out.columns["Nope"]) == {None}
    default = await handler.log_window(LogWindowQuery(file="chamber_log_20260901_100000.csv"))
    assert "HT Temp moni" in default.columns and "A Pump1" not in default.columns


def test_a_file_crossing_midnight_keeps_counting_forward(tmp_path):
    write_log(tmp_path, "chamber_log_20260901_235500.csv", dt.datetime(2026, 9, 1, 23, 55, 0), 600)
    archive = LogArchive(tmp_path)
    info = archive.info("chamber_log_20260901_235500.csv")
    assert info["end"] == epoch(2026, 9, 2, 0, 4, 59)
    times = archive.window(None, None, None, ["HT Temp set"], 10_000)["time"]
    assert times == sorted(times) and len(times) == 600


def test_a_session_running_for_days(tmp_path):
    # 50 hours at one row a minute: two midnights. A first-and-last-row guess gets the
    # end a day early -- before the start.
    write_log(tmp_path, "chamber_log_20260901_120000.csv", dt.datetime(2026, 9, 1, 12, 0, 0),
              50 * 3600, step=60)
    info = LogArchive(tmp_path).info("chamber_log_20260901_120000.csv")
    assert info["end"] - info["start"] == 50 * 3600 - 60


def test_an_unnamed_file_is_dated_by_when_it_was_last_written(tmp_path):
    write_log(tmp_path, "growth.csv", dt.datetime(2026, 8, 3, 14, 0, 0), 120)
    info = LogArchive(tmp_path).info("growth.csv")
    assert info["start"] == epoch(2026, 8, 3, 14, 0, 0)


def test_a_half_written_last_line_is_skipped(folder):
    path = folder / "chamber_log_20260901_100000.csv"
    with open(path, "a") as f:
        f.write("10:10:0")
    archive = LogArchive(folder)
    assert archive.info(path.name)["n_rows"] == 600
    assert len(archive.window(path.name, None, None, None, 10_000)["time"]) == 600


def test_the_tailed_file_is_marked_live(folder):
    live = folder / "chamber_log_20260901_101500.csv"
    archive = LogArchive(folder, live_path=lambda: str(live))
    assert archive.info(live.name)["live"] is True
    assert archive.info("chamber_log_20260901_100000.csv")["live"] is False


@pytest.mark.parametrize("name", ["../x.csv", "a/b.csv", ".hidden.csv", "notes.txt"])
def test_names_outside_the_folder_are_refused(folder, name):
    with pytest.raises(ValueError):
        LogArchive(folder).path(name)


@pytest.mark.asyncio
async def test_without_a_folder_the_ops_say_so():
    with pytest.raises(RuntimeError, match="no log folder"):
        await ChamberLogHandler(None, None).list_log_files(ListLogFiles())
