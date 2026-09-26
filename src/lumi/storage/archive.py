"""Reading finished HDF5 recordings back: the other half of the recorder.

The recorder writes one file per growth; nothing read them except a notebook that
knew the layout. This is that knowledge in one place, for the `storage.archive`
capability: summaries, a frame by index, the integration traces, the embedded log.

Plain synchronous code -- h5py blocks -- that the handler runs in a thread. Every read
opens the file, reads what it needs and closes it again, so a file is never held open
between requests and a recording can be deleted or copied off while the node runs.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from lumi.config import settings
from lumi.contracts.payloads.chamber import DEFAULT_LOG_COLUMNS

SUFFIX = ".hdf5"


def stride_for(n: int, max_points: int) -> int:
    """Every k-th point, so at most `max_points` survive. Thinning by stride rather
    than averaging keeps each point a real sample at a real time."""
    return max(1, -(-n // max_points))


def iso(epoch: float | None) -> str | None:
    """Epoch seconds -> the UTC ISO form every `_iso` field carries."""
    if epoch is None or not np.isfinite(epoch):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def as_value(text) -> float | str | None:
    """A stored log cell -> a number where it is one, else its text."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if text is None or text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return str(text)


def _size(ds) -> int:
    """Rows actually written. Datasets grow in pre-allocated blocks of 1000, so the
    shape over-counts; the recorder keeps the true count in a `size` attribute."""
    return int(ds.attrs.get("size", ds.shape[0]))


def _texts(ds, n: int) -> np.ndarray:
    return ds.asstr()[:n] if n else np.empty((0,) + ds.shape[1:], dtype=object)


def _floats(values) -> np.ndarray:
    return np.array([np.nan if v in (None, "") else float(v) for v in values], dtype=float)


class RecordingNotFound(KeyError):
    pass


class RecordingArchive:
    def __init__(self, root: str | Path, *, live_name=lambda: None) -> None:
        self.root = Path(root)
        #: Returns the stem being recorded right now, or None. That file is open for
        #: writing in this same process and HDF5 will not open it a second time.
        self.live_name = live_name
        #: Dataset names, as settings.storage.databases gives them to the recorder.
        self.ds = settings.storage.databases
        self._scale_cache: OrderedDict[tuple, tuple[float, float]] = OrderedDict()
        self._summary_cache: dict[str, tuple[tuple, dict]] = {}
        self._lock = threading.Lock()

    # --- files

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted((p.stem for p in self.root.glob(f"*{SUFFIX}")), reverse=True)

    def path(self, name: str) -> Path:
        """The file for a recording name, refusing anything that is not a bare name in
        the archive folder -- the name arrives from a browser."""
        if not name or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
            raise ValueError(f"not a recording name: {name!r}")
        path = self.root / f"{name}{SUFFIX}"
        if path.resolve().parent != self.root.resolve():
            raise ValueError(f"not a recording name: {name!r}")
        if not path.is_file():
            raise RecordingNotFound(f"no recording named {name!r} in {self.root}")
        return path

    def _open(self, name: str) -> h5py.File:
        if name == self.live_name():
            raise RuntimeError(f"{name!r} is still being recorded; it can be read once "
                               "the recording stops")
        return h5py.File(self.path(name), "r")

    # --- summaries

    def summary(self, name: str) -> dict:
        path = self.path(name)
        stat = path.stat()
        base = {"name": name, "size_bytes": stat.st_size, "modified": stat.st_mtime}
        if name == self.live_name():
            return {**base, "recording": True}
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._summary_cache.get(name)
        if cached and cached[0] == key:
            return {**base, **cached[1]}
        try:
            with h5py.File(path, "r") as f:
                extra = self._counts(f)
        except Exception as exc:  # a crashed recording can be unreadable; list it anyway
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._summary_cache[name] = (key, extra)
        return {**base, **extra}

    def _counts(self, f: h5py.File) -> dict:
        out: dict = {}
        times: np.ndarray | None = None
        if self.ds.frame in f:
            out["n_frames"] = _size(f[self.ds.frame])
            times = self._frame_times(f)
        if self.ds.log in f:
            out["n_log_rows"] = _size(f[self.ds.log])
            if times is None or not times.size:
                times = self._log_times(f)
        if self.ds.integration_root in f:
            out["n_integrations"] = _size(f[self.ds.integration_root])
        if times is not None and times.size:
            out["start"], out["end"] = float(np.nanmin(times)), float(np.nanmax(times))
        return out

    def info(self, name: str) -> dict:
        summary = self.summary(name)
        if summary.get("error"):
            return summary
        with self._open(name) as f:
            info: dict = {"datasets": {k: _size(f[k]) for k in f if isinstance(f[k], h5py.Dataset)}}
            if self.ds.frame in f:
                frames = f[self.ds.frame]
                info["frame_shape"] = list(frames.shape[1:])
                info["frame_dtype"] = str(frames.dtype)
                info["frame_times"] = [float(t) for t in self._frame_times(f)]
            if self.ds.log in f:
                info["log_columns"] = [str(c) for c in f[self.ds.log].attrs.get("columns", [])]
            info["boxes"] = [box for box, _ in self._traces(f, None, None, None).values()]
        return {**summary, **info}

    # --- frames

    def _frame_times(self, f: h5py.File) -> np.ndarray:
        meta = f[self.ds.frame_meta]
        n = min(_size(f[self.ds.frame]), _size(meta))
        columns = [str(c) for c in meta.attrs.get("columns", ["time_stamp", "time"])]
        return _floats(_texts(meta, n)[:, columns.index("time")]) if n else np.empty(0)

    def frame(self, name: str, index: int) -> tuple[dict, np.ndarray]:
        with self._open(name) as f:
            if self.ds.frame not in f:
                raise ValueError(f"{name!r} has no frames")
            frames = f[self.ds.frame]
            n = _size(frames)
            if not 0 <= index < n:
                raise IndexError(f"frame {index} out of range: {name!r} has {n} frames")
            image = frames[index]
            meta = {"name": name, "index": index, "n_frames": n,
                    "dtype": str(image.dtype), "shape": list(image.shape)}
            meta_ds = f[self.ds.frame_meta]
            if index < _size(meta_ds):
                columns = [str(c) for c in meta_ds.attrs.get("columns", ["time_stamp", "time"])]
                row = meta_ds.asstr()[index]
                values = dict(zip(columns, row))
                meta["time_stamp"] = values.get("time_stamp")
                meta["time"] = as_value(values.get("time"))
        return meta, image

    def frame_scale(self, name: str) -> tuple[float, float]:
        """(low, high) raw values for black and white, fixed for the whole recording:
        the 0.5 and 99.9 percentiles over up to eight frames spread across it."""
        path = self.path(name)
        key = (name, path.stat().st_mtime_ns)
        with self._lock:
            if key in self._scale_cache:
                return self._scale_cache[key]
        with self._open(name) as f:
            frames = f[self.ds.frame]
            n = _size(frames)
            if n == 0:
                raise ValueError(f"{name!r} has no frames")
            picks = np.unique(np.linspace(0, n - 1, num=min(n, 8)).astype(int))
            sample = np.stack([frames[i] for i in picks])
        low, high = (float(v) for v in np.percentile(sample, [0.5, 99.9]))
        if high <= low:
            high = low + 1.0
        with self._lock:
            self._scale_cache[key] = (low, high)
            while len(self._scale_cache) > 64:
                self._scale_cache.popitem(last=False)
        return low, high

    # --- integrations

    def _traces(self, f: h5py.File, bbox_ids, since, until) -> dict[int, tuple[dict, dict]]:
        """{bbox_id: (box, {"time","mean","max","min"} arrays)}."""
        if self.ds.integration not in f:
            return {}
        data_ds, meta_ds = f[self.ds.integration], f[self.ds.integration_meta]
        n = min(_size(data_ds), _size(meta_ds))
        if n == 0:
            return {}
        columns = [str(c) for c in data_ds.attrs["columns"]]
        data = data_ds[:n]
        meta_columns = [str(c) for c in meta_ds.attrs.get("columns", ["time_stamp", "time"])]
        times = _floats(_texts(meta_ds, n)[:, meta_columns.index("time")])
        col = {c: data[:, i] for i, c in enumerate(columns)}
        out = {}
        for bbox_id in np.unique(col["bbox_id"]).astype(int):
            if bbox_ids is not None and bbox_id not in bbox_ids:
                continue
            rows = col["bbox_id"] == bbox_id
            first = np.argmax(rows)
            box = {"bbox_id": int(bbox_id),
                   **{k: float(col[k][first]) for k in ("center_x", "center_y", "width", "height")}}
            keep = rows.copy()
            if since is not None:
                keep &= times >= since
            if until is not None:
                keep &= times <= until
            out[int(bbox_id)] = (box, {"time": times[keep], "mean": col["mean"][keep],
                                       "max": col["max"][keep], "min": col["min"][keep]})
        return out

    def integration(self, name: str, bbox_ids, since, until, max_points: int) -> dict:
        with self._open(name) as f:
            traces = self._traces(f, bbox_ids, since, until)
        out = []
        for box, series in traces.values():
            n = len(series["time"])
            k = stride_for(n, max_points)
            out.append({**box, "n_points": n, "stride": k,
                        **{key: [float(v) for v in values[::k]] for key, values in series.items()}})
        return {"name": name, "traces": out}

    # --- the embedded chamber log

    def _log_times(self, f: h5py.File) -> np.ndarray:
        ds = f[self.ds.log]
        columns = [str(c) for c in ds.attrs.get("columns", [])]
        n = _size(ds)
        if not n or "time" not in columns:
            return np.empty(0)
        return _floats(ds.asstr()[:n, columns.index("time")])

    def log(self, name: str, columns, since, until, max_points: int) -> dict:
        wanted = list(columns) if columns else list(DEFAULT_LOG_COLUMNS)
        with self._open(name) as f:
            if self.ds.log not in f:
                raise ValueError(f"{name!r} has no chamber log")
            ds = f[self.ds.log]
            stored = [str(c) for c in ds.attrs.get("columns", [])]
            rows = _texts(ds, _size(ds))
        times = _floats(rows[:, stored.index("time")]) if len(rows) else np.empty(0)
        keep = np.ones(len(times), dtype=bool)
        if since is not None:
            keep &= times >= since
        if until is not None:
            keep &= times <= until
        picked = np.nonzero(keep)[0]
        k = stride_for(len(picked), max_points)
        picked = picked[::k]
        return {
            "name": name, "n_rows": int(keep.sum()), "stride": k,
            "time": [float(t) for t in times[picked]],
            "columns": {c: ([as_value(v) for v in rows[picked, stored.index(c)]]
                            if c in stored else [None] * len(picked)) for c in wanted},
        }
