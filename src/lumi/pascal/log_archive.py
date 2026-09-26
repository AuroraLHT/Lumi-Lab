"""The chamber's past growth logs, read back from the folder PASCAL writes them into.

The live `log` op only ever had the latest row. Every earlier row is still on disk --
PASCAL starts a CSV per logging session -- so a history view needs them read back by
time: "the chamber log while this experiment ran".

The one wrinkle is the date. The `Time` column is `2:51:59 PM` with no date at all
(`process_row` stamps live rows with *today*, which is right for a row read as it is
written and wrong for anything older). So each file is anchored:

- by its name, when it is `chamber_log_YYYYMMDD_HHMMSS.csv` -- the name
  `ExperimentManager.start_mi_logging` gives every file it starts;
- otherwise by its modification time, which is when its last row was written.

Within a file, time of day running backwards by more than twelve hours is taken as a
midnight crossing.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from lumi.contracts.payloads.chamber import DEFAULT_LOG_COLUMNS

NAME_RE = re.compile(r"(\d{8})_(\d{6})")
HALF_DAY = 12 * 3600
#: Parsed files kept in memory. A session's log is a few MB of text.
CACHE_FILES = 8


def time_of_day(text: str | bytes) -> int | None:
    """`2:51:59 PM` -> seconds since midnight. By hand rather than strptime: listing
    scans every row of every file, and a long session is 150k rows."""
    if isinstance(text, bytes):
        text = text.decode("ascii", "replace")
    try:
        clock, meridiem = text.strip().split(" ")
        h, m, sec = clock.split(":")
        hour = int(h) % 12 + (12 if meridiem.upper() == "PM" else 0)
        return hour * 3600 + int(m) * 60 + int(sec)
    except ValueError:
        return None


def day_offsets(tods: list[int]) -> list[int]:
    """Days elapsed at each row: time of day running back by over twelve hours is a
    midnight crossing. A long session crosses several."""
    offsets, day = [], 0
    for i, tod in enumerate(tods):
        if i and tod < tods[i - 1] - HALF_DAY:
            day += 1
        offsets.append(day)
    return offsets


def first_day(name: str, modified: float, first_tod: int, last_tod: int, rollovers: int) -> dt.date:
    """The calendar date of a file's first row. See the module docstring."""
    m = NAME_RE.search(name)
    if m:
        try:
            named = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            named = None
        if named is not None:
            named_tod = named.hour * 3600 + named.minute * 60 + named.second
            # Named just before midnight, first row just after.
            return named.date() + dt.timedelta(days=1 if first_tod < named_tod - HALF_DAY else 0)
    mtime = dt.datetime.fromtimestamp(modified)
    mtime_tod = mtime.hour * 3600 + mtime.minute * 60 + mtime.second
    last = mtime.date() - dt.timedelta(days=1 if last_tod > mtime_tod + 3600 else 0)
    return last - dt.timedelta(days=rollovers)


def to_epoch(day: dt.date, tod: int) -> float:
    """Local wall-clock, which is what the controller logs."""
    return dt.datetime.combine(day, dt.time()).timestamp() + tod


def as_value(text: str) -> float | str | None:
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return text


def stride_for(n: int, max_points: int) -> int:
    return max(1, -(-n // max_points))


class LogArchive:
    def __init__(self, folder: str | Path, *, live_path=lambda: None) -> None:
        self.folder = Path(folder)
        #: The file the log reader is tailing, or None.
        self.live_path = live_path
        self._info_cache: dict[str, tuple[tuple, dict]] = {}
        self._parsed: OrderedDict[str, tuple[tuple, dict]] = OrderedDict()
        self._lock = threading.Lock()

    # --- files

    def names(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted((p.name for p in self.folder.glob("*.csv")), reverse=True)

    def path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
            raise ValueError(f"not a log file name: {name!r}")
        path = self.folder / name
        if path.resolve().parent != self.folder.resolve() or path.suffix != ".csv":
            raise ValueError(f"not a log file name: {name!r}")
        if not path.is_file():
            raise KeyError(f"no chamber log named {name!r} in {self.folder}")
        return path

    def _is_live(self, path: Path) -> bool:
        live = self.live_path()
        try:
            return live is not None and Path(live).resolve() == path.resolve()
        except OSError:
            return False

    # --- a file's extent, without reading all of it

    def info(self, name: str) -> dict:
        path = self.path(name)
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._info_cache.get(name)
        if cached and cached[0] == key:
            return {**cached[1], "live": self._is_live(path)}

        # Only the time column, but all of it: the number of midnights a file crosses
        # is only known by walking it, and the extent has to agree with what a window
        # read of the same file (_load) reports.
        with open(path, "rb") as f:
            lines = f.read().splitlines()[1:]
        tods = [t for t in (time_of_day(ln.split(b",", 1)[0]) for ln in lines if ln.strip())
                if t is not None]
        out = {"name": name, "size_bytes": stat.st_size, "modified": stat.st_mtime, "n_rows": len(tods)}
        if tods:
            rollovers = day_offsets(tods)[-1]
            day = first_day(name, stat.st_mtime, tods[0], tods[-1], rollovers)
            out["start"] = to_epoch(day, tods[0])
            out["end"] = to_epoch(day + dt.timedelta(days=rollovers), tods[-1])
        with self._lock:
            self._info_cache[name] = (key, out)
        return {**out, "live": self._is_live(path)}

    # --- a whole file, parsed

    def _load(self, name: str) -> dict:
        """{"time": epoch array, "header": [...], "rows": [[str, ...], ...]}"""
        path = self.path(name)
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._parsed.get(name)
            if cached and cached[0] == key:
                self._parsed.move_to_end(name)
                return cached[1]

        text = path.read_text(errors="replace")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        rows, tods = [], []
        for row in reader:
            if not row:
                continue
            tod = time_of_day(row[0])
            if tod is None:  # a half-written last line on a live file
                continue
            rows.append(row)
            tods.append(tod)

        times = np.empty(len(tods))
        if tods:
            offsets = day_offsets(tods)
            day = first_day(name, stat.st_mtime, tods[0], tods[-1], offsets[-1])
            # Per row, not base + offset * 86400: a midnight inside a DST change is not
            # 86400 s after the previous one.
            times = np.array([to_epoch(day + dt.timedelta(days=o), t) for o, t in zip(offsets, tods)])
        parsed = {"time": times, "header": header, "rows": rows}
        with self._lock:
            self._parsed[name] = (key, parsed)
            while len(self._parsed) > CACHE_FILES:
                self._parsed.popitem(last=False)
        return parsed

    # --- a window, across files

    def window(self, file: str | None, since: float | None, until: float | None,
               columns: list[str] | None, max_points: int) -> dict:
        if file is not None:
            names = [file]
        elif since is None and until is None:
            names = self.names()[:1]  # nothing asked for: the newest file
        else:
            names = []
            for name in self.names():
                info = self.info(name)
                start, end = info.get("start"), info.get("end")
                if start is None:
                    continue
                if (until is None or start <= until) and (since is None or end >= since):
                    names.append(name)

        wanted = list(columns) if columns else list(DEFAULT_LOG_COLUMNS)
        times: list[np.ndarray] = []
        values: dict[str, list] = {c: [] for c in wanted}
        for name in names:
            parsed = self._load(name)
            t = parsed["time"]
            keep = np.ones(len(t), dtype=bool)
            if since is not None:
                keep &= t >= since
            if until is not None:
                keep &= t <= until
            idx = np.nonzero(keep)[0]
            if not idx.size:
                continue
            times.append(t[idx])
            where = {c: i for i, c in enumerate(parsed["header"])}
            rows = parsed["rows"]
            for c in wanted:
                i = where.get(c)
                values[c].extend(
                    [as_value(rows[j][i]) if i is not None and i < len(rows[j]) else None for j in idx]
                )

        all_times = np.concatenate(times) if times else np.empty(0)
        order = np.argsort(all_times, kind="stable")
        k = stride_for(len(order), max_points)
        picked = order[::k]
        return {
            "time": [float(x) for x in all_times[picked]],
            "columns": {c: [values[c][j] for j in picked] for c in wanted},
            "n_rows": int(len(order)),
            "stride": k,
        }
