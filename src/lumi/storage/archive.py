"""Reading recordings back: the other half of the recorder.

The `storage.archive` capability: summaries, a frame by index, the integration traces,
the embedded log, and for v2 files the metadata, events and detections. The file
layouts live in lumi.storage.reader's backends, which notebooks use too.

Plain synchronous code -- h5py blocks -- that the handler runs in a thread. Every read
opens the file, reads what it needs and closes it again, so a file is never held open
between requests and a recording can be deleted or copied off while the node runs.

The one exception is the recording being written right now. HDF5 will not open a file
a second time in the process that is writing it, so a v2 recording in progress is read
through the writer's own handle, under its lock. A v1 recording in progress (none are
written any more) cannot be read until it stops.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from lumi.config import settings
from lumi.contracts.payloads.chamber import DEFAULT_LOG_COLUMNS
from lumi.storage.reader import as_value, backend  # noqa: F401 -- as_value is re-exported

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


def _window(times: np.ndarray, since, until) -> np.ndarray:
    keep = np.ones(len(times), dtype=bool)
    if since is not None:
        keep &= times >= since
    if until is not None:
        keep &= times <= until
    return keep


def _cell(v):
    """An array element -> what JSON carries: a float (None for NaN) or text."""
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, (int, np.integer)):
        return float(v)
    return as_value(v)


class RecordingNotFound(KeyError):
    pass


class RecordingArchive:
    def __init__(self, root: str | Path, *, live_name=lambda: None, live_writer=lambda: None,
                 default_energy_kev: float | None = None) -> None:
        self.root = Path(root)
        #: Reported for recordings made before files carried their beam energy.
        self.default_energy_kev = float(default_energy_kev if default_energy_kev is not None
                                        else settings.get("rheed.energy_kev", 25.0) or 25.0)
        #: Returns the stem being recorded right now, or None.
        self.live_name = live_name
        #: Returns the v2 RecordingWriter for that stem, or None.
        self.live_writer = live_writer
        #: Dataset names, as settings.storage.databases gives them to the v1 recorder.
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

    def _live(self, name: str):
        """The writer for `name` if it is the recording in progress, else None. Raises
        if it is in progress but cannot be read (no v2 writer)."""
        if name != self.live_name():
            return None
        writer = self.live_writer()
        if writer is None or writer.closed:
            raise RuntimeError(f"{name!r} is still being recorded; it can be read once "
                               "the recording stops")
        return writer

    @contextlib.contextmanager
    def _reading(self, name: str):
        """A backend for `name`, open for the duration of the block."""
        writer = self._live(name)
        if writer is not None:
            with writer.lock:
                writer.flush()
                yield backend(writer.h5f, self.ds)
            return
        with h5py.File(self.path(name), "r") as f:
            yield backend(f, self.ds)

    # Kept for callers that want the raw file (tests, scripts).
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
            base["recording"] = True
            writer = self.live_writer()
            if writer is None or writer.closed:
                return base
            try:
                with self._reading(name) as b:
                    return {**base, **self._counts(b)}
            except Exception as exc:
                return {**base, "error": f"{type(exc).__name__}: {exc}"}
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._summary_cache.get(name)
        if cached and cached[0] == key:
            return {**base, **cached[1]}
        try:
            with self._reading(name) as b:
                extra = self._counts(b)
        except Exception as exc:  # a crashed recording can be unreadable; list it anyway
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._summary_cache[name] = (key, extra)
        return {**base, **extra}

    def _counts(self, b) -> dict:
        attrs = b.attrs()
        out: dict = {"format_version": b.version}
        times: np.ndarray | None = None
        if b.has_frames():
            out["n_frames"] = b.n_frames()
            times = b.frame_times()
        if b.has_log():
            out["n_log_rows"] = b.n_log()
            if times is None or not times.size:
                times = b.log_times()
        if b.has_integration():
            out["n_integrations"] = b.n_integration_events()
        if b.has_detection():
            out["n_detections"] = b.n_detections()
        if b.version >= 2:
            out["n_events"] = len(b.events()["time"])
            out["complete"] = "closed" in attrs
            request = b.meta().get("request") or {}
            out["note"] = request.get("note")
            out["operator"] = request.get("operator")
        if times is not None and times.size and np.isfinite(times).any():
            out["start"], out["end"] = float(np.nanmin(times)), float(np.nanmax(times))
        energy = attrs.get("rheed_energy_kev")
        out["rheed_energy_recorded"] = energy is not None
        out["rheed_energy_kev"] = float(energy) if energy is not None else self.default_energy_kev
        return out

    def info(self, name: str) -> dict:
        summary = self.summary(name)
        if summary.get("error"):
            return summary
        if summary.get("recording") and summary.get("format_version") is None:
            return summary
        with self._reading(name) as b:
            info: dict = {"datasets": b.datasets()}
            if b.has_frames():
                frames = b.frame_dataset()
                info["frame_shape"] = list(frames.shape[1:])
                info["frame_dtype"] = str(frames.dtype)
                info["frame_times"] = [float(t) for t in b.frame_times()]
            if b.has_log():
                info["log_columns"] = b.log_columns()
            history = b.boxes()
            info["boxes"] = [{k: box[k] for k in ("bbox_id", "center_x", "center_y", "width", "height")}
                             for box in _first_boxes(history)]
            info["box_history"] = history
            info["meta"] = b.meta()
            info["attrs"] = {k: v for k, v in b.attrs().items()
                             if isinstance(v, (str, int, float, bool))}
            info["detection_classes"] = b.detection_classes()
        return {**summary, **info}

    # --- frames

    def frame(self, name: str, index: int) -> tuple[dict, np.ndarray]:
        with self._reading(name) as b:
            if not b.has_frames():
                raise ValueError(f"{name!r} has no frames")
            n = b.n_frames()
            if not 0 <= index < n:
                raise IndexError(f"frame {index} out of range: {name!r} has {n} frames")
            image = b.frame(index)
            header = b.frame_header(index)
        meta = {"name": name, "index": index, "n_frames": n,
                "dtype": str(image.dtype), "shape": list(image.shape)}
        if header:
            meta["time_stamp"] = header.get("time_stamp")
            meta["time"] = header.get("time")
        return meta, image

    def frame_scale(self, name: str) -> tuple[float, float]:
        """(low, high) raw values for black and white, fixed for the whole recording:
        the 0.5 and 99.9 percentiles over up to eight frames spread across it."""
        path = self.path(name)
        live = name == self.live_name()
        key = (name, path.stat().st_mtime_ns)
        with self._lock:
            if not live and key in self._scale_cache:
                return self._scale_cache[key]
        with self._reading(name) as b:
            n = b.n_frames()
            if n == 0:
                raise ValueError(f"{name!r} has no frames")
            picks = np.unique(np.linspace(0, n - 1, num=min(n, 8)).astype(int))
            sample = np.stack([b.frame(int(i)) for i in picks])
        low, high = (float(v) for v in np.percentile(sample, [0.5, 99.9]))
        if high <= low:
            high = low + 1.0
        if not live:
            with self._lock:
                self._scale_cache[key] = (low, high)
                while len(self._scale_cache) > 64:
                    self._scale_cache.popitem(last=False)
        return low, high

    # --- integrations

    def integration(self, name: str, bbox_ids, since, until, max_points: int) -> dict:
        with self._reading(name) as b:
            data = b.integration()
            boxes = _first_boxes(b.boxes(data))
        out = []
        if data and len(data["time"]):
            for box in boxes:
                bbox_id = box["bbox_id"]
                if bbox_ids is not None and bbox_id not in bbox_ids:
                    continue
                keep = (data["bbox_id"] == bbox_id) & _window(data["time"], since, until)
                n = int(keep.sum())
                k = stride_for(n, max_points)
                idx = np.flatnonzero(keep)[::k]
                out.append({**{f: box[f] for f in ("bbox_id", "center_x", "center_y", "width", "height")},
                            "n_points": n, "stride": k,
                            **{key: data[key][idx].astype(float).tolist()
                               for key in ("time", "mean", "max", "min")}})
        return {"name": name, "traces": out}

    # --- the embedded chamber log

    def log(self, name: str, columns, since, until, max_points: int) -> dict:
        wanted = list(columns) if columns else list(DEFAULT_LOG_COLUMNS)
        with self._reading(name) as b:
            if not b.has_log():
                raise ValueError(f"{name!r} has no chamber log")
            times = b.log_times()
            table = b.log_table([c for c in wanted if c != "time"])
        keep = _window(times, since, until)
        picked = np.flatnonzero(keep)
        k = stride_for(len(picked), max_points)
        picked = picked[::k]
        return {
            "name": name, "n_rows": int(keep.sum()), "stride": k,
            "time": times[picked].astype(float).tolist(),
            "columns": {c: ([_cell(v) for v in table[c][picked]] if c in table
                            else (times[picked].astype(float).tolist() if c == "time"
                                  else [None] * len(picked))) for c in wanted},
        }

    # --- v2: events and detections

    def events(self, name: str, kinds, since, until, limit: int) -> dict:
        with self._reading(name) as b:
            ev = b.events()
        rows = []
        for t, kind, message, detail in zip(ev["time"], ev["kind"], ev["message"], ev["detail"]):
            if kinds and kind not in kinds:
                continue
            if (since is not None and t < since) or (until is not None and t > until):
                continue
            rows.append({"time": t, "time_iso": iso(t), "kind": kind, "message": message,
                         "detail": detail})
        return {"name": name, "total": len(rows), "events": rows[-limit:] if limit else rows}

    def detections(self, name: str, since, until, max_points: int) -> dict:
        with self._reading(name) as b:
            if not b.has_detection():
                raise ValueError(f"{name!r} has no detections")
            table = b.detection_table()
            classes = b.detection_classes()
        times = table["time"]
        keep = _window(times, since, until)
        picked = np.flatnonzero(keep)
        k = stride_for(len(picked), max_points)
        picked = picked[::k]
        return {"name": name, "n_detections": int(keep.sum()), "stride": k,
                "index": picked.astype(int).tolist(),
                "time": times[picked].astype(float).tolist(),
                "n_boxes": table["n_boxes"][picked].astype(int).tolist(),
                "classes": classes,
                "classification": {c: [_cell(v) for v in table[f"p_{c}"][picked]]
                                   for c in classes if f"p_{c}" in table}}

    def detection(self, name: str, index: int) -> dict:
        with self._reading(name) as b:
            n = b.n_detections()
            if not 0 <= index < n:
                raise IndexError(f"detection {index} out of range: {name!r} has {n}")
            table = b.detection_table()
            boxes = b.detection_boxes(index)
            crop = b.crop(index)
        return {"name": name, "index": index, "n_detections": n,
                "time": _cell(table["time"][index]), "uuid": str(table["uuid"][index]),
                "crop": ({"x": crop[0], "y": crop[1], "width": crop[2], "height": crop[3]}
                         if crop else None),
                "classification": {c[2:]: _cell(v[index]) for c, v in table.items() if c.startswith("p_")},
                "boxes": boxes}


def _first_boxes(history: list[dict]) -> list[dict]:
    """One entry per bbox_id -- its first geometry -- in bbox_id order."""
    first: dict[int, dict] = {}
    for box in history:
        first.setdefault(box["bbox_id"], box)
    return [first[k] for k in sorted(first)]
