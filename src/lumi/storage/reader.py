"""Reading recordings back, v1 or v2, through one interface.

    from lumi.storage import open_recording
    with open_recording("run/simulation/database/STO-..._1234.hdf5") as rec:
        rec.log()            # DataFrame, one row per log entry, typed columns
        rec.integration()    # DataFrame, long: time, bbox_id, mean, max, min
        rec.frames[10]       # one frame, read on demand
        rec.meta["camera"]   # what was recorded with (v2 only)

`Recording` wraps a *backend*: `V1` for the text-celled layout record.py writes and
`V2` for lumi.storage.v2. RecordingArchive (the History API) uses the same backends,
so a notebook and the browser can never disagree about what a file holds.

Everything is read with array slices. The v1 backend parses each text column once,
vectorised, instead of a float() per cell -- the 12 s walk of a 15-minute file's
integration in RecordReader is 0.1 s here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import h5py
import hdf5plugin  # noqa: F401 -- registers the v2 filters with h5py; frames will not open without it
import numpy as np

from lumi.storage import v2 as fmt


def _floats(values) -> np.ndarray:
    """Text cells -> float64, NaN where a cell is empty or not a number. Vectorised."""
    arr = np.asarray(values, dtype=object)
    if arr.size == 0:
        return np.empty(arr.shape, dtype=float)
    try:
        return arr.astype(np.float64)
    except (TypeError, ValueError):
        pass
    import pandas as pd
    flat = pd.to_numeric(pd.Series(arr.ravel()).replace("", np.nan), errors="coerce")
    return flat.to_numpy(dtype=np.float64).reshape(arr.shape)


def as_value(text) -> float | str | None:
    """A stored text cell -> a number where it is one, else its text."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if text is None or text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return str(text)


def local_stamp(t: float | None) -> str | None:
    """The `time_stamp` form the cameras write: local time, str(datetime)."""
    if t is None or not np.isfinite(t):
        return None
    return str(datetime.fromtimestamp(float(t)))


def format_version(f: h5py.File) -> int:
    return int(f.attrs.get("format_version", 1)) if f.attrs.get("format") == fmt.FORMAT else 1


def backend(f: h5py.File, ds_names=None):
    return V2(f) if format_version(f) == 2 else V1(f, ds_names)


# --- v1 ------------------------------------------------------------------------------


class V1:
    """record.py's layout: pre-allocated datasets with `size` attrs, text metadata."""

    version = 1

    def __init__(self, f: h5py.File, ds_names=None) -> None:
        if ds_names is None:
            from lumi.config import settings
            ds_names = settings.storage.databases
        self.f, self.ds = f, ds_names

    def _get(self, key: str):
        name = getattr(self.ds, key, key)
        return self.f[name] if name in self.f else None

    @staticmethod
    def _size(ds) -> int:
        return int(ds.attrs.get("size", ds.shape[0]))

    def _texts(self, ds, n: int) -> np.ndarray:
        return ds.asstr()[:n] if n else np.empty((0,) + ds.shape[1:], dtype=object)

    def _column(self, ds, name: str, n: int) -> np.ndarray:
        columns = [str(c) for c in ds.attrs.get("columns", [])]
        if name not in columns or not n:
            return np.empty(0, dtype=object)
        return ds.asstr()[:n, columns.index(name)]

    def attrs(self) -> dict:
        return {k: _plain(v) for k, v in self.f.attrs.items()}

    def meta(self) -> dict[str, Any]:
        return {}

    def datasets(self) -> dict[str, int]:
        return {k: self._size(self.f[k]) for k in self.f if isinstance(self.f[k], h5py.Dataset)}

    # frames
    def has_frames(self) -> bool:
        return self._get("frame") is not None

    def n_frames(self) -> int:
        ds = self._get("frame")
        return self._size(ds) if ds is not None else 0

    def frame_dataset(self):
        return self._get("frame")

    def frame(self, i: int) -> np.ndarray:
        return self._get("frame")[i]

    def frame_times(self) -> np.ndarray:
        meta = self._get("frame_meta")
        if meta is None:
            return np.empty(0)
        n = min(self.n_frames(), self._size(meta))
        return _floats(self._column(meta, "time", n))

    def frame_header(self, i: int) -> dict:
        meta = self._get("frame_meta")
        if meta is None or i >= self._size(meta):
            return {}
        columns = [str(c) for c in meta.attrs.get("columns", ["time_stamp", "time"])]
        values = dict(zip(columns, meta.asstr()[i]))
        return {"time": as_value(values.get("time")), "time_stamp": values.get("time_stamp")}

    # log
    def has_log(self) -> bool:
        return self._get("log") is not None

    def n_log(self) -> int:
        ds = self._get("log")
        return self._size(ds) if ds is not None else 0

    def log_columns(self) -> list[str]:
        ds = self._get("log")
        return [str(c) for c in ds.attrs.get("columns", [])] if ds is not None else []

    def log_times(self) -> np.ndarray:
        ds = self._get("log")
        if ds is None:
            return np.empty(0)
        return _floats(self._column(ds, "time", self._size(ds)))

    def log_table(self, columns: list[str] | None = None) -> dict[str, np.ndarray]:
        """{column: values}: float64 where the whole column parses, else object text."""
        ds = self._get("log")
        if ds is None:
            return {}
        stored = self.log_columns()
        rows = self._texts(ds, self._size(ds))
        out = {}
        for c in (columns if columns is not None else stored):
            if c not in stored:
                continue
            raw = rows[:, stored.index(c)]
            nums = _floats(raw)
            filled = np.array([v != "" for v in raw], dtype=bool) if len(raw) else np.zeros(0, bool)
            out[c] = nums if not np.any(np.isnan(nums) & filled) else np.array(
                [None if v == "" else v for v in raw], dtype=object)
        return out

    # integration
    def has_integration(self) -> bool:
        return self._get("integration") is not None

    def n_integration_events(self) -> int:
        ds = self._get("integration_root")
        return self._size(ds) if ds is not None else 0

    def integration(self) -> dict[str, np.ndarray]:
        data_ds, meta_ds = self._get("integration"), self._get("integration_meta")
        if data_ds is None or meta_ds is None:
            return {}
        n = min(self._size(data_ds), self._size(meta_ds))
        columns = [str(c) for c in data_ds.attrs["columns"]]
        data = data_ds[:n]
        col = {c: data[:, i] for i, c in enumerate(columns)}
        return {"time": _floats(self._column(meta_ds, "time", n)),
                "bbox_id": col["bbox_id"].astype(np.int32),
                **{k: col[k] for k in ("mean", "max", "min")},
                **{k: col[k] for k in ("center_x", "center_y", "width", "height") if k in col}}

    def boxes(self, data: dict | None = None) -> list[dict]:
        """One entry per box geometry: v1 repeats the geometry on every row, so a new
        entry starts wherever a box's geometry changes."""
        data = self.integration() if data is None else data
        if not data or not len(data["time"]):
            return []
        ids, t = data["bbox_id"], data["time"]
        keys = ("center_x", "center_y", "width", "height")
        g = np.stack([np.asarray(data[k], dtype=np.float64) if k in data else np.full(len(t), np.nan)
                      for k in keys], axis=1)
        out = []
        for b in np.unique(ids):
            rows = np.flatnonzero(ids == b)
            gb = g[rows]
            change = np.ones(len(rows), dtype=bool)
            same = (gb[1:] == gb[:-1]) | (np.isnan(gb[1:]) & np.isnan(gb[:-1]))
            change[1:] = ~same.all(axis=1)
            starts = rows[change]
            for j, s in enumerate(starts):
                nxt = starts[j + 1] if j + 1 < len(starts) else None
                out.append({"bbox_id": int(b), **{k: float(g[s, i]) for i, k in enumerate(keys)},
                            "t_from": float(t[s]), "t_to": float(t[nxt]) if nxt is not None else None})
        return out

    # detection
    def has_detection(self) -> bool:
        return self._get("detection_meta") is not None

    def n_detections(self) -> int:
        ds = self._get("detection_meta")
        return self._size(ds) if ds is not None else 0

    def detection_table(self) -> dict[str, np.ndarray]:
        meta = self._get("detection_meta")
        if meta is None:
            return {}
        n = self.n_detections()
        out = {"time": _floats(self._column(meta, "time", n)),
               "uuid": np.asarray(self._column(meta, "uuid", n), dtype=object)}
        num = self._get("num_detection")
        out["n_boxes"] = num[:n].astype(np.int32) if num is not None else np.zeros(n, np.int32)
        cls = self._get("classification")
        if cls is not None:
            names = [str(c) for c in cls.attrs.get("class_name", [])]
            values = cls[:n]
            for j, c in enumerate(names):
                out[f"p_{c}"] = values[:, j]
        return out

    def detection_classes(self) -> list[str]:
        cls = self._get("classification")
        return [str(c) for c in cls.attrs.get("class_name", [])] if cls is not None else []

    def detection_boxes(self, d: int) -> list[dict]:
        det, num = self._get("detection"), self._get("num_detection")
        if det is None or num is None:
            return []
        k = int(num[d])
        rows = det[d, :k]
        return [{"bbox_id": i, "x0": float(r[0]), "y0": float(r[1]), "x1": float(r[2]),
                 "y1": float(r[3]), "label": int(r[4]), "score": float(r[5]), "track": -1}
                for i, r in enumerate(rows)]

    def detection_masks(self, d: int) -> dict[int, np.ndarray]:
        seg, num = self._get("instance_segmentation"), self._get("num_detection")
        if seg is None or num is None:
            return {}
        k = int(num[d])
        return {i: m for i, m in enumerate(seg[d, :k])} if k else {}

    def pattern(self, d: int) -> np.ndarray | None:
        ds = self._get("pattern")
        return ds[d] if ds is not None and d < self._size(ds) else None

    def crop(self, d: int) -> tuple[int, int, int, int] | None:
        return None

    # events
    def events(self) -> dict[str, list]:
        return {"time": [], "kind": [], "message": [], "detail": []}


# --- v2 ------------------------------------------------------------------------------


class V2:
    """lumi.storage.v2's layout."""

    version = 2

    def __init__(self, f: h5py.File) -> None:
        self.f = f

    def refresh(self) -> None:
        """See rows appended since the file was opened (a live, SWMR-opened file)."""
        def visit(_name, node):
            if isinstance(node, h5py.Dataset):
                node.refresh()
        self.f.visititems(visit)

    def _n(self, path: str) -> int:
        return int(self.f[path].shape[0]) if path in self.f else 0

    def attrs(self) -> dict:
        return {k: _plain(v) for k, v in self.f.attrs.items()}

    def meta(self) -> dict[str, Any]:
        g = self.f.get("meta")
        if g is None:
            return {}
        out = {}
        for k in g:
            raw = g[k][()]
            try:
                out[k] = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (ValueError, AttributeError):
                out[k] = None
        return out

    def datasets(self) -> dict[str, int]:
        out = {}

        def visit(name, node):
            if fmt.is_text(node):
                out[name] = fmt.text_count(node)
            elif isinstance(node, h5py.Dataset) and node.ndim and not _inside_text(name, self.f):
                out[name] = int(node.shape[0])
        self.f.visititems(visit)
        return out

    # frames
    def has_frames(self) -> bool:
        return "frame/data" in self.f

    def _frame_n(self) -> int:
        return min(self._n("frame/data"), self._n("frame/time"))

    def n_frames(self) -> int:
        return self._frame_n() if self.has_frames() else 0

    def frame_dataset(self):
        return self.f["frame/data"] if self.has_frames() else None

    def frame(self, i: int) -> np.ndarray:
        return self.f["frame/data"][i]

    def frame_times(self) -> np.ndarray:
        return self.f["frame/time"][:self._frame_n()] if self.has_frames() else np.empty(0)

    def frame_header(self, i: int) -> dict:
        t = float(self.f["frame/time"][i])
        return {"time": t, "time_stamp": local_stamp(t), "seq": int(self.f["frame/seq"][i])}

    # log
    def has_log(self) -> bool:
        return "log" in self.f

    def _log_n(self) -> int:
        return self._n("log/time")

    def n_log(self) -> int:
        return self._log_n() if self.has_log() else 0

    def log_columns(self) -> list[str]:
        if not self.has_log():
            return []
        return ["time", *json.loads(self.f["log"].attrs["columns"])]

    def log_times(self) -> np.ndarray:
        return self.f["log/time"][:self._log_n()] if self.has_log() else np.empty(0)

    def log_table(self, columns: list[str] | None = None) -> dict[str, np.ndarray]:
        if not self.has_log():
            return {}
        g = self.f["log"]
        n = self._log_n()
        stored = self.log_columns()
        out = {}
        for c in (columns if columns is not None else stored):
            if c not in stored:
                continue
            if c == "time":
                out[c] = g["time"][:n]
                continue
            node = g[fmt.escape(c)]
            if fmt.is_text(node):
                texts = fmt.read_text(node, 0, n)
                out[c] = np.array([t if t != "" else None for t in texts], dtype=object)
            else:
                out[c] = node[:n]
        return out

    def log_extra(self) -> list[dict]:
        if not self.has_log():
            return []
        return [json.loads(t) if t else {} for t in fmt.read_text(self.f["log/_extra"], 0, self._log_n())]

    # integration
    def has_integration(self) -> bool:
        return "integration" in self.f

    def integration(self) -> dict[str, np.ndarray]:
        if not self.has_integration():
            return {}
        n = min(self._n(f"integration/{k}") for k in ("time", "bbox_id", "mean", "max", "min"))
        return {k: self.f[f"integration/{k}"][:n] for k in ("time", "bbox_id", "mean", "max", "min")}

    def n_integration_events(self) -> int:
        if not self.has_integration():
            return 0
        if "n_integrations" in self.f.attrs:
            return int(self.f.attrs["n_integrations"])
        return int(np.unique(self.f["integration/time"][:]).size)

    def boxes(self, data: dict | None = None) -> list[dict]:
        if not self.has_integration():
            return []
        rows = self.f["integration/boxes"][:]
        return [{"bbox_id": int(r["bbox_id"]), "center_x": float(r["center_x"]),
                 "center_y": float(r["center_y"]), "width": float(r["width"]),
                 "height": float(r["height"]), "t_from": float(r["t_from"]),
                 "t_to": None if np.isnan(r["t_to"]) else float(r["t_to"])} for r in rows]

    # detection
    def has_detection(self) -> bool:
        return "detection" in self.f

    def n_detections(self) -> int:
        if not self.has_detection():
            return 0
        return min(self._n(f"detection/{k}") for k in ("time", "uuid", "n_boxes", "classification",
                                                         "pattern_denom"))

    def detection_classes(self) -> list[str]:
        if not self.has_detection():
            return []
        return [str(c) for c in self.f["detection/classification"].attrs.get("classes", [])]

    def detection_table(self) -> dict[str, np.ndarray]:
        if not self.has_detection():
            return {}
        n = self.n_detections()
        g = self.f["detection"]
        out = {"time": g["time"][:n],
               "uuid": np.array([u.decode("ascii", "replace") for u in g["uuid"][:n]], dtype=object),
               "n_boxes": g["n_boxes"][:n]}
        crop = g["crop"][:n]
        for j, k in enumerate(("crop_x", "crop_y", "crop_width", "crop_height")):
            out[k] = crop[:, j]
        values = g["classification"][:n]
        for j, c in enumerate(self.detection_classes()):
            out[f"p_{c}"] = values[:, j]
        return out

    def _box_rows(self, d: int) -> np.ndarray:
        ds = self.f["detection/boxes"]
        det = ds["det"]
        lo, hi = np.searchsorted(det, d, "left"), np.searchsorted(det, d, "right")
        return ds[lo:hi]

    def detection_boxes(self, d: int) -> list[dict]:
        return [{"bbox_id": int(r["bbox_id"]), "x0": float(r["x0"]), "y0": float(r["y0"]),
                 "x1": float(r["x1"]), "y1": float(r["y1"]), "label": int(r["label"]),
                 "score": float(r["score"]), "track": int(r["track"])} for r in self._box_rows(d)]

    def detection_masks(self, d: int) -> dict[int, np.ndarray]:
        shape = self._pattern_full_shape(d)
        bits = self.f["detection/mask_bits"]
        out = {}
        for r in self._box_rows(d):
            h, w = int(r["mask_h"]), int(r["mask_w"])
            if r["mask_offset"] < 0:
                out[int(r["bbox_id"])] = np.zeros(shape, dtype=bool)
                continue
            nbytes = -(-h * w // 8)
            off = int(r["mask_offset"])
            out[int(r["bbox_id"])] = fmt.unpack_mask(bits[off:off + nbytes], int(r["mask_y0"]),
                                                     int(r["mask_x0"]), h, w, shape)
        return out

    def _pattern_full_shape(self, d: int) -> tuple[int, int]:
        h, w = (int(v) for v in self.f["detection/pattern_shape"][d])
        if h and w:
            return h, w
        crop = self.f["detection/crop"][d]
        return (int(crop[3]), int(crop[2])) if crop[2] > 0 else (0, 0)

    def pattern(self, d: int) -> np.ndarray | None:
        g = self.f["detection"]
        if "pattern" not in g:
            return None
        h, w = (int(v) for v in g["pattern_shape"][d])
        if not (h and w):
            return None
        denom = float(g["pattern_denom"][d])
        if np.isfinite(denom):
            return fmt.decode_pattern(g["pattern"][d, :h, :w], denom)
        slots = np.flatnonzero(g["pattern_f32_det"][:] == d)
        return g["pattern_f32"][int(slots[0]), :h, :w] if slots.size else None

    def crop(self, d: int) -> tuple[int, int, int, int] | None:
        c = self.f["detection/crop"][d]
        return None if c[2] < 0 else tuple(int(v) for v in c)

    # events
    def events(self) -> dict[str, list]:
        if "events" not in self.f:
            return {"time": [], "kind": [], "message": [], "detail": []}
        g = self.f["events"]
        n = min(self._n("events/time"), *(fmt.text_count(g[k]) for k in ("kind", "message", "detail")))
        return {"time": [float(t) for t in g["time"][:n]],
                "kind": fmt.read_text(g["kind"], 0, n),
                "message": fmt.read_text(g["message"], 0, n),
                "detail": [json.loads(t) if t else {} for t in fmt.read_text(g["detail"], 0, n)]}


def _inside_text(name: str, f: h5py.File) -> bool:
    parent = name.rsplit("/", 1)[0] if "/" in name else ""
    return bool(parent) and fmt.is_text(f[parent])


def _plain(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


# --- the notebook-facing reader --------------------------------------------------------


class Frames:
    """A recording's frames, read one at a time: len(), [i], [a:b], iteration."""

    def __init__(self, b) -> None:
        self._b = b

    def __len__(self) -> int:
        return self._b.n_frames()

    def __getitem__(self, i):
        n = len(self)
        if isinstance(i, slice):
            return np.stack([self._b.frame(j) for j in range(*i.indices(n))]) if n else np.empty((0,))
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError(f"frame {i} out of range ({n} frames)")
        return self._b.frame(i)

    def __iter__(self):
        for i in range(len(self)):
            yield self._b.frame(i)

    @property
    def times(self) -> np.ndarray:
        return self._b.frame_times()


class Recording:
    """One recording file, v1 or v2. Use as a context manager, or call close()."""

    def __init__(self, path: str | Path, *, swmr: bool = False) -> None:
        self.path = Path(path)
        self.f = h5py.File(self.path, "r", libver="latest", swmr=swmr) if swmr else h5py.File(self.path, "r")
        self.b = backend(self.f)
        self.frames = Frames(self.b)

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def version(self) -> int:
        return self.b.version

    def refresh(self) -> None:
        """For a file opened with live=True: pick up what the writer appended since."""
        if self.version == 2 and self.f.swmr_mode:
            self.b.refresh()

    @property
    def attrs(self) -> dict:
        return self.b.attrs()

    @property
    def meta(self) -> dict[str, Any]:
        return self.b.meta()

    def log(self, columns: list[str] | None = None):
        import pandas as pd
        table = self.b.log_table(columns)
        if "time" in table:
            table = {"time": table.pop("time"), **table}
        return pd.DataFrame(table)

    def integration(self, bbox_id: int | None = None):
        import pandas as pd
        df = pd.DataFrame({k: v for k, v in self.b.integration().items()
                           if k in ("time", "bbox_id", "mean", "max", "min")})
        return df[df["bbox_id"] == bbox_id].reset_index(drop=True) if bbox_id is not None and len(df) else df

    def boxes(self):
        import pandas as pd
        return pd.DataFrame(self.b.boxes())

    def events(self):
        import pandas as pd
        return pd.DataFrame(self.b.events())

    def detections(self):
        import pandas as pd
        return pd.DataFrame(self.b.detection_table())

    def detection(self, d: int) -> dict:
        """One detection in full: boxes, masks (full pattern size), pattern, crop."""
        return {"boxes": self.b.detection_boxes(d), "masks": self.b.detection_masks(d),
                "pattern": self.b.pattern(d), "crop": self.b.crop(d)}


def open_recording(path: str | Path, *, live: bool = False) -> Recording:
    """Open a recording for reading. `live=True` opens a v2 file that is still being
    written (SWMR); call `rec.refresh()` to see rows appended since."""
    return Recording(path, swmr=live)
