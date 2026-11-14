"""Recording format v2: one HDF5 file per recording, typed columns, written in SWMR mode.

v1 (record.py) stored every number as text and parsed it back one cell at a time,
kept no compression on frames, a float32 copy of each detection pattern and a
full-frame bool per detected instance. A 15-minute growth was 5.5 GB and walking its
integration took 12 s. v2 keeps the same data, losslessly, at about half the size, and
reads back with array slices.

Layout (every dataset's length is the true count; nothing is pre-allocated)::

    /                     attrs: format="lumi-recording", format_version=2, created,
                          rheed_energy_kev, provenance (lumi_version, contract_hash,
                          host, h5py, hdf5); on a clean close also closed, end and the
                          counts. A file without `closed` was interrupted.
    /meta/<source>        JSON text, a snapshot taken at start: request, experiment,
                          chamber, camera, integrator, detection
    /events/{time, kind, message, detail}
                          what happened during the recording; detail is JSON
    /frame/data           uint16 [N, H, W], bitshuffle-zstd, one frame per chunk
    /frame/{time, seq}    f8, i8 [N]; seq is the camera's frame counter (-1 unknown)
    /log/time             f8 [R]
    /log/<column>         f8 [R] (NaN = no value), or text; attrs: name, kind
    /log/_extra           text [R]: JSON of any values outside the schema, "" if none
    /integration/{time, bbox_id, mean, max, min}
                          long format, one row per box per event
    /integration/boxes    one row per box geometry: bbox_id, center_x, center_y,
                          width, height, t_from, t_to (NaN = still current)
    /detection/{time, uuid, n_boxes, crop}
                          one row per detection; crop is x, y, width, height on the frame
    /detection/classification   f4 [D, C]; attrs: classes
    /detection/boxes      one row per box: det, bbox_id, x0, y0, x1, y1 (pattern
                          coordinates, i.e. inside the crop), label, score, track, and
                          where its mask is: mask_offset, mask_y0, mask_x0, mask_h, mask_w
    /detection/mask_bits  uint8: each mask cropped to its own extent and np.packbits'd
    /detection/pattern    uint16 [D, H, W] k, with pattern = float32(k / pattern_denom[d]);
                          pattern_shape[d] is the part that is used (the crop can change)
    /detection/pattern_f32, pattern_f32_det
                          the rare pattern that is not k / denom, kept as float32

Text columns are a uint8 blob plus end offsets (`<name>/blob`, `<name>/end`), not
HDF5 variable-length strings: those live in the global heap, which SWMR does not
protect, and a reader of a live file then fails with "address of object past end of
allocation".

A writer creates every dataset up front (SWMR cannot add objects later) and then only
appends. Reading needs `import hdf5plugin` before h5py opens the file; lumi.storage.reader
does that.
"""

from __future__ import annotations

import json
import math
import platform
import threading
import time as _time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping

import h5py
import hdf5plugin
import numpy as np

FORMAT = "lumi-recording"
FORMAT_VERSION = 2

#: Rows per chunk for the 1-D columns. Big enough that a long recording is a few
#: hundred chunks, small enough that a live reader sees rows within a second.
COLUMN_CHUNK = 4096
INTEGRATION_CHUNK = 65536

BOX_DTYPE = np.dtype([
    ("bbox_id", "<i4"), ("center_x", "<f4"), ("center_y", "<f4"),
    ("width", "<f4"), ("height", "<f4"), ("t_from", "<f8"), ("t_to", "<f8"),
])

DETECTED_BOX_DTYPE = np.dtype([
    ("det", "<i8"), ("bbox_id", "<i4"),
    ("x0", "<f4"), ("y0", "<f4"), ("x1", "<f4"), ("y1", "<f4"),
    ("label", "<i2"), ("score", "<f4"), ("track", "<i4"),
    ("mask_offset", "<i8"), ("mask_y0", "<i4"), ("mask_x0", "<i4"),
    ("mask_h", "<i4"), ("mask_w", "<i4"),
])


def frame_filter():
    return hdf5plugin.Bitshuffle(cname="zstd")


def table_filter():
    return hdf5plugin.Blosc2(cname="zstd", clevel=5, filters=hdf5plugin.Blosc2.SHUFFLE)


def pattern_filter():
    return hdf5plugin.Blosc2(cname="zstd", clevel=5, filters=hdf5plugin.Blosc2.BITSHUFFLE)


def escape(name: str) -> str:
    """A log column name -> an HDF5 link name. '/' separates groups and a leading '.'
    or '_' is reserved here, so those are %-escaped; everything else is kept, so
    h5ls shows the PASCAL names."""
    out = name.replace("%", "%25").replace("/", "%2F")
    if out.startswith((".", "_")):
        out = f"%{ord(out[0]):02X}{out[1:]}"
    return out or "%00"


def provenance() -> dict[str, str]:
    """Which software wrote the file. Never fails: a missing piece is left out."""
    out = {"host": platform.node(), "h5py": h5py.version.version,
           "hdf5": h5py.version.hdf5_version, "hdf5plugin": hdf5plugin.version}
    try:
        from importlib.metadata import version
        out["lumi_version"] = version("lumi")
    except Exception:
        pass
    try:
        from lumi.contracts import contract_hash
        out["contract_hash"] = contract_hash()
    except Exception:
        pass
    commit = _git_commit()
    if commit:
        out["lumi_commit"] = commit
    return out


def _git_commit() -> str | None:
    """The Lumi-Lab commit this runs from, '+dirty' if the tree has changes; None
    outside a git checkout."""
    import subprocess
    here = Path(__file__).resolve().parent
    try:
        sha = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    return f"{sha}+dirty" if dirty else sha or None


# --- pattern encoding ---------------------------------------------------------------


def encode_pattern(pattern: np.ndarray) -> tuple[np.ndarray, float] | None:
    """The detector's pattern -> (k, denom) with k uint16 and float32(k / denom) equal
    to float32(pattern) on every pixel -- float32 is what v1 stored. None if no such
    pair exists.

    The detector min-max scales each frame: (frame - min) / (max - min + 1e-5), so every
    value is an integer count over one shared denominator. The denominator is found
    from the smallest step between distinct values, then checked on every pixel.
    """
    p = np.asarray(pattern)
    if p.ndim != 2 or p.size == 0 or not np.all(np.isfinite(p)):
        return None
    p32 = p.astype(np.float32)
    values = np.unique(p32)
    if values.size == 1:
        if values[0] == 0:
            return np.zeros(p.shape, np.uint16), 1.0
        if values[0] == 1:
            return np.ones(p.shape, np.uint16), 1.0
        return None
    if values[0] < 0:
        return None
    # The smallest step between distinct values is m / denom for a small m (often 1),
    # but float32 rounding moves it, so nearby denominators are tried too -- first on
    # the distinct values, which is cheap, then on every pixel.
    v64 = values.astype(np.float64)
    step = float(np.min(np.diff(v64)))
    candidates: list[float] = []
    for m in range(1, 9):
        centre = round(m / step)
        for d in (centre, centre - 1, centre + 1, centre - 2, centre + 2):
            if 0 < d <= 65535:
                candidates += [d + 1e-5, float(d)]
    # A plain ratio of integers (a pattern scaled some other way) is worth one try too.
    candidates.append(float(Fraction(float(values[1])).limit_denominator(65535).denominator))
    sample = values if values.size <= 512 else values[np.linspace(0, values.size - 1, 512).astype(int)]
    s64 = sample.astype(np.float64)
    for denom in dict.fromkeys(candidates):
        k = np.rint(s64 * denom)
        if k.max() > 65535 or not np.array_equal((k / denom).astype(np.float32), sample):
            continue
        k = np.rint(p.astype(np.float64) * denom)
        if np.array_equal((k / denom).astype(np.float32), p32):
            return k.astype(np.uint16), float(denom)
    return None


def decode_pattern(k: np.ndarray, denom: float) -> np.ndarray:
    return (k.astype(np.float64) / denom).astype(np.float32)


def pack_mask(mask: np.ndarray) -> tuple[bytes, int, int, int, int]:
    """A full-size bool mask -> (packed bits of its occupied rectangle, y0, x0, h, w).
    Lossless: everything outside the rectangle is False."""
    m = np.asarray(mask).astype(bool)
    if m.ndim != 2 or not m.any():
        return b"", 0, 0, 0, 0
    rows = np.flatnonzero(m.any(axis=1))
    cols = np.flatnonzero(m.any(axis=0))
    y0, y1, x0, x1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    return np.packbits(m[y0:y1, x0:x1].ravel()).tobytes(), y0, x0, y1 - y0, x1 - x0


def unpack_mask(bits: np.ndarray, y0: int, x0: int, h: int, w: int,
                shape: tuple[int, int]) -> np.ndarray:
    out = np.zeros(shape, dtype=bool)
    if h and w:
        out[y0:y0 + h, x0:x0 + w] = np.unpackbits(bits, count=h * w).reshape(h, w).astype(bool)
    return out


# --- appendable columns ---------------------------------------------------------------


class Column:
    """A 1-D (or [N, ...]) dataset that grows by appends, buffered until flush()."""

    def __init__(self, group: h5py.Group, name: str, dtype, *, shape=(), chunk=COLUMN_CHUNK,
                 compression=None, attrs: Mapping[str, Any] | None = None) -> None:
        kwargs = dict(compression) if isinstance(compression, Mapping) else (
            dict(compression) if compression is not None else {})
        self.ds = group.create_dataset(
            name, shape=(0, *shape), maxshape=(None, *shape), dtype=dtype,
            chunks=(chunk, *shape), **kwargs,
        )
        for k, v in (attrs or {}).items():
            self.ds.attrs[k] = v
        self.dtype = np.dtype(dtype)
        self._pending: list = []
        self.count = 0

    def append(self, value) -> None:
        self._pending.append(value)
        self.count += 1

    def flush(self) -> None:
        if not self._pending:
            return
        rows = np.asarray(self._pending, dtype=self.dtype)
        self._pending = []
        n = self.ds.shape[0]
        self.ds.resize(n + len(rows), axis=0)
        self.ds[n:] = rows
        self.ds.flush()


class TextColumn:
    """Strings as a uint8 blob plus each string's end offset -- SWMR-safe text."""

    def __init__(self, group: h5py.Group, name: str, *, attrs: Mapping[str, Any] | None = None) -> None:
        self.group = group.create_group(name)
        self.group.attrs["kind"] = "text"
        for k, v in (attrs or {}).items():
            self.group.attrs[k] = v
        self.blob = Column(self.group, "blob", np.uint8, chunk=65536, compression=table_filter())
        self.end = Column(self.group, "end", np.int64, compression=table_filter())
        self._size = 0
        self.count = 0

    def append(self, text: str | None) -> None:
        data = (text or "").encode("utf-8")
        self.blob._pending.extend(data)
        self._size += len(data)
        self.end.append(self._size)
        self.count += 1

    def flush(self) -> None:
        # The blob first: a live reader that sees an end offset must find its bytes.
        self.blob.flush()
        self.end.flush()


def read_text(group: h5py.Group, start: int = 0, stop: int | None = None) -> list[str]:
    """Rows [start, stop) of a TextColumn, as str."""
    end = group["end"]
    n = end.shape[0] if stop is None else min(stop, end.shape[0])
    if n <= start:
        return []
    ends = end[start:n]
    first = int(end[start - 1]) if start > 0 else 0
    blob = group["blob"][first:int(ends[-1])].tobytes()
    out, at = [], 0
    for e in (ends - first):
        out.append(blob[at:int(e)].decode("utf-8", "replace"))
        at = int(e)
    return out


def text_count(group: h5py.Group) -> int:
    return int(group["end"].shape[0])


def is_text(node) -> bool:
    return isinstance(node, h5py.Group) and node.attrs.get("kind") == "text"


# --- the writer ----------------------------------------------------------------------


@dataclass
class LogSchema:
    """The log columns, in order, and which of them hold text (status words, stamps)."""

    columns: list[str]
    text: set[str] = field(default_factory=set)

    @classmethod
    def from_sample(cls, columns: Iterable[str], sample: Mapping[str, Any] | None) -> "LogSchema":
        sample = sample or {}
        cols = [c for c in columns if c != "time"]
        return cls(cols, {c for c in cols if isinstance(sample.get(c), str) and _not_number(sample[c])})


def _not_number(text: str) -> bool:
    try:
        float(text)
        return False
    except ValueError:
        return True


@dataclass
class DetectionSchema:
    classes: list[str]
    detector_classes: list[str] = field(default_factory=list)
    #: The largest pattern this recording can hold: the camera's frame size.
    pattern_shape: tuple[int, int] | None = None


class RecordingWriter:
    """Writes one v2 recording. Thread-safe: every method takes the writer's lock."""

    def __init__(self, path: str | Path, *, frame_shape: tuple[int, int] | None = None,
                 log: LogSchema | None = None, integration: bool = False,
                 detection: DetectionSchema | None = None,
                 meta: Mapping[str, Any] | None = None, attrs: Mapping[str, Any] | None = None,
                 overwrite: bool = False) -> None:
        self.path = Path(path)
        if self.path.exists() and not overwrite:
            raise FileExistsError(f"recording {self.path} already exists")
        self.lock = threading.RLock()
        self.h5f = h5py.File(self.path, "w", libver="latest")
        self.closed = False
        self.start: float | None = None
        self.end: float | None = None
        try:
            self._create(frame_shape, log, integration, detection, meta or {}, attrs or {})
        except Exception:
            self.h5f.close()
            raise
        self.h5f.swmr_mode = True

    # --- layout

    def _create(self, frame_shape, log, integration, detection, meta, attrs) -> None:
        f = self.h5f
        f.attrs["format"] = FORMAT
        f.attrs["format_version"] = FORMAT_VERSION
        f.attrs["created"] = _time.time()
        for k, v in {**provenance(), **attrs}.items():
            if v is not None:
                f.attrs[k] = v

        g = f.create_group("meta")
        for source, snapshot in meta.items():
            if snapshot is not None:
                text = json.dumps(snapshot, default=str, sort_keys=True)
                g.create_dataset(source, data=np.bytes_(text.encode("utf-8")))

        ev = f.create_group("events")
        self._events = {
            "time": Column(ev, "time", np.float64, compression=table_filter()),
            "kind": TextColumn(ev, "kind"),
            "message": TextColumn(ev, "message"),
            "detail": TextColumn(ev, "detail"),
        }
        self._columns: list = list(self._events.values())

        self._frame = None
        if frame_shape is not None:
            h, w = (int(v) for v in frame_shape[:2])
            fg = f.create_group("frame")
            self._frame = {
                "data": fg.create_dataset("data", shape=(0, h, w), maxshape=(None, h, w),
                                          dtype=np.uint16, chunks=(1, h, w), **frame_filter()),
                "time": Column(fg, "time", np.float64, compression=table_filter()),
                "seq": Column(fg, "seq", np.int64, compression=table_filter()),
            }
            self._columns += [self._frame["time"], self._frame["seq"]]
        self.n_frames = 0

        self._log = None
        self.log_schema = log
        if log is not None:
            lg = f.create_group("log")
            lg.attrs["columns"] = json.dumps(log.columns)
            self._log = {"time": Column(lg, "time", np.float64, compression=table_filter())}
            for c in log.columns:
                if c in log.text:
                    self._log[c] = TextColumn(lg, escape(c), attrs={"name": c})
                else:
                    self._log[c] = Column(lg, escape(c), np.float64, compression=table_filter(),
                                          attrs={"name": c, "kind": "number"})
            self._log["_extra"] = TextColumn(lg, "_extra")
            self._columns += list(self._log.values())
        self.n_log = 0

        self._integration = None
        if integration:
            ig = f.create_group("integration")
            self._integration = {
                "time": Column(ig, "time", np.float64, chunk=INTEGRATION_CHUNK, compression=table_filter()),
                "bbox_id": Column(ig, "bbox_id", np.int32, chunk=INTEGRATION_CHUNK, compression=table_filter()),
                "mean": Column(ig, "mean", np.float32, chunk=INTEGRATION_CHUNK, compression=table_filter()),
                "max": Column(ig, "max", np.float32, chunk=INTEGRATION_CHUNK, compression=table_filter()),
                "min": Column(ig, "min", np.float32, chunk=INTEGRATION_CHUNK, compression=table_filter()),
            }
            self._boxes = ig.create_dataset("boxes", shape=(0,), maxshape=(None,), dtype=BOX_DTYPE,
                                            chunks=(256,))
            self._box_rows: dict[int, tuple[int, tuple]] = {}  # bbox_id -> (row, geometry)
            self._columns += list(self._integration.values())
        self.n_integration_events = 0

        self._det = None
        if detection is not None:
            dg = f.create_group("detection")
            n_cls = max(1, len(detection.classes))
            shape = tuple(int(v) for v in (detection.pattern_shape or (0, 0)))
            self._det = {
                "time": Column(dg, "time", np.float64, compression=table_filter()),
                "uuid": Column(dg, "uuid", "S36", compression=table_filter()),
                "n_boxes": Column(dg, "n_boxes", np.int32, compression=table_filter()),
                "crop": Column(dg, "crop", np.int32, shape=(4,), compression=table_filter(),
                               attrs={"columns": ["x", "y", "width", "height"]}),
                "classification": Column(dg, "classification", np.float32, shape=(n_cls,),
                                         compression=table_filter(),
                                         attrs={"classes": list(detection.classes) or ["?"]}),
                "boxes": Column(dg, "boxes", DETECTED_BOX_DTYPE, chunk=4096,
                                compression=table_filter(),
                                attrs={"classes": list(detection.detector_classes)}),
                "mask_bits": Column(dg, "mask_bits", np.uint8, chunk=65536, compression=table_filter()),
                "pattern_denom": Column(dg, "pattern_denom", np.float64, compression=table_filter()),
                "pattern_shape": Column(dg, "pattern_shape", np.int32, shape=(2,),
                                        compression=table_filter()),
                "pattern_f32_det": Column(dg, "pattern_f32_det", np.int64, compression=table_filter()),
            }
            self._det_classes = list(detection.classes)
            self._pattern_shape = shape
            if shape[0] and shape[1]:
                self._pattern = dg.create_dataset(
                    "pattern", shape=(0, *shape), maxshape=(None, *shape), dtype=np.uint16,
                    chunks=(1, *shape), **pattern_filter())
                self._pattern_f32 = dg.create_dataset(
                    "pattern_f32", shape=(0, *shape), maxshape=(None, *shape), dtype=np.float32,
                    chunks=(1, *shape), **pattern_filter())
            else:
                self._pattern = self._pattern_f32 = None
            self._mask_size = 0
            self._columns += list(self._det.values())
        self.n_detections = 0
        self.n_events = 0

    # --- appends

    def _seen(self, t: float | None) -> None:
        if t is None or not math.isfinite(t):
            return
        self.start = t if self.start is None else min(self.start, t)
        self.end = t if self.end is None else max(self.end, t)

    def add_frame(self, image: np.ndarray, time: float, seq: int | None = None) -> None:
        if self._frame is None:
            raise RuntimeError("this recording does not save frames")
        image = np.asarray(image)
        if image.ndim == 3:
            image = image[..., 0]
        with self.lock:
            ds = self._frame["data"]
            n = ds.shape[0]
            ds.resize(n + 1, axis=0)
            ds[n] = image
            self._frame["time"].append(float(time))
            self._frame["seq"].append(-1 if seq is None else int(seq))
            self.n_frames += 1
            self._seen(float(time))

    def add_log(self, time: float, values: Mapping[str, Any]) -> None:
        if self._log is None:
            raise RuntimeError("this recording does not save the log")
        schema = self.log_schema
        with self.lock:
            self._log["time"].append(float(time))
            for c in schema.columns:
                v = values.get(c)
                if c in schema.text:
                    self._log[c].append("" if v is None else str(v))
                else:
                    self._log[c].append(_number(v))
            known = set(schema.columns) | {"time"}
            extra = {k: v for k, v in values.items() if k not in known}
            # A value that would not fit its numeric column is kept here as well.
            extra.update({c: values[c] for c in schema.columns
                          if c not in schema.text and values.get(c) is not None
                          and not _is_float(values[c])})
            self._log["_extra"].append(json.dumps(extra, default=str) if extra else "")
            self.n_log += 1
            self._seen(float(time))

    def add_integration(self, time: float, results: Mapping[int, Mapping[str, Any]]) -> None:
        if self._integration is None:
            raise RuntimeError("this recording does not save integrations")
        t = float(time)
        with self.lock:
            for bbox_id, r in results.items():
                bbox_id = int(bbox_id)
                self._integration["time"].append(t)
                self._integration["bbox_id"].append(bbox_id)
                for k in ("mean", "max", "min"):
                    self._integration[k].append(float(r.get(k, np.nan)))
                geometry = tuple(float(r.get(k, np.nan)) for k in ("center_x", "center_y", "width", "height"))
                self._box(bbox_id, geometry, t)
            self.n_integration_events += 1
            self._seen(t)

    def _box(self, bbox_id: int, geometry: tuple, t: float) -> None:
        current = self._box_rows.get(bbox_id)
        if current is not None and _same(current[1], geometry):
            return
        n = self._boxes.shape[0]
        if current is not None:
            row = self._boxes[current[0]]
            row["t_to"] = t
            self._boxes[current[0]] = row
        self._boxes.resize(n + 1, axis=0)
        self._boxes[n] = np.array((bbox_id, *geometry, t, np.nan), dtype=BOX_DTYPE)
        self._boxes.flush()
        self._box_rows[bbox_id] = (n, geometry)

    def add_detection(self, time: float, uuid: str, boxes: list[Mapping[str, Any]],
                      classification: Mapping[str, float], pattern: np.ndarray | None = None,
                      masks: Mapping[int, np.ndarray] | None = None,
                      tracks: Mapping[int, int] | None = None,
                      crop: tuple[int, int, int, int] | None = None) -> None:
        """One detection. `boxes` are dicts with bbox_id, x, y, width, height, score,
        label, in pattern coordinates; `masks` maps a bbox_id to its full-size mask."""
        if self._det is None:
            raise RuntimeError("this recording does not save detections")
        masks = masks or {}
        tracks = tracks or {}
        with self.lock:
            d = self.n_detections
            det = self._det
            det["time"].append(float(time))
            det["uuid"].append(str(uuid or "")[:36].encode("ascii", "replace"))
            det["n_boxes"].append(len(boxes))
            det["crop"].append(tuple(int(v) for v in crop) if crop else (-1, -1, -1, -1))
            det["classification"].append([float(classification.get(c, np.nan))
                                          for c in (self._det_classes or ["?"])])
            for b in boxes:
                bbox_id = int(b["bbox_id"])
                bits, y0, x0, h, w = pack_mask(masks[bbox_id]) if bbox_id in masks else (b"", 0, 0, 0, 0)
                offset = self._mask_size if bits else -1
                det["mask_bits"]._pending.extend(bits)
                self._mask_size += len(bits)
                x, y = float(b["x"]), float(b["y"])
                det["boxes"].append((d, bbox_id, x, y, x + float(b["width"]), y + float(b["height"]),
                                     _label(b.get("label")), float(b["score"]),
                                     int(tracks.get(bbox_id, -1)), offset, y0, x0, h, w))
            self._add_pattern(d, pattern)
            self.n_detections += 1
            self._seen(float(time))

    def _add_pattern(self, d: int, pattern: np.ndarray | None) -> None:
        det = self._det
        shape = self._pattern_shape
        if self._pattern is None:
            det["pattern_denom"].append(np.nan)
            det["pattern_shape"].append((0, 0))
            return
        n = self._pattern.shape[0]
        self._pattern.resize(n + 1, axis=0)
        if pattern is None:
            det["pattern_denom"].append(np.nan)
            det["pattern_shape"].append((0, 0))
            return
        p = np.asarray(pattern)
        h, w = min(p.shape[0], shape[0]), min(p.shape[1], shape[1])
        p = p[:h, :w]
        encoded = encode_pattern(p)
        det["pattern_shape"].append((h, w))
        if encoded is not None:
            k, denom = encoded
            buf = np.zeros(shape, np.uint16)
            buf[:h, :w] = k
            self._pattern[n] = buf
            det["pattern_denom"].append(denom)
        else:
            det["pattern_denom"].append(np.nan)
            m = self._pattern_f32.shape[0]
            self._pattern_f32.resize(m + 1, axis=0)
            buf = np.zeros(shape, np.float32)
            buf[:h, :w] = p
            self._pattern_f32[m] = buf
            det["pattern_f32_det"].append(d)

    def add_event(self, time: float, kind: str, message: str = "",
                  detail: Mapping[str, Any] | None = None) -> None:
        with self.lock:
            self._events["time"].append(float(time))
            self._events["kind"].append(kind)
            self._events["message"].append(message)
            self._events["detail"].append(json.dumps(detail, default=str) if detail else "")
            self.n_events += 1

    # --- flushing and closing

    def flush(self) -> None:
        """Push buffered rows to the file, so a live reader (and a crash) sees them."""
        with self.lock:
            if self.closed:
                return
            for col in self._columns:
                col.flush()
            if self._frame is not None:
                self._frame["data"].flush()
            if self._det is not None and self._pattern is not None:
                self._pattern.flush()
                self._pattern_f32.flush()

    def counts(self) -> dict[str, int]:
        return {"n_frames": self.n_frames, "n_log_rows": self.n_log,
                "n_integrations": self.n_integration_events, "n_detections": self.n_detections,
                "n_events": self.n_events}

    def close(self) -> None:
        """Flush, close, then stamp the end and the counts -- attributes cannot change
        while the file is in SWMR mode, so this reopens it briefly."""
        with self.lock:
            if self.closed:
                return
            self.flush()
            self.h5f.close()
            self.closed = True
            with h5py.File(self.path, "r+", libver="latest") as f:
                f.attrs["closed"] = _time.time()
                if self.start is not None:
                    f.attrs["start"] = self.start
                    f.attrs["end"] = self.end
                for k, v in self.counts().items():
                    f.attrs[k] = v


def _number(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def _is_float(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _label(label) -> int:
    try:
        return int(float(label))
    except (TypeError, ValueError):
        return -1


def _same(a: tuple, b: tuple) -> bool:
    return all((x == y) or (math.isnan(x) and math.isnan(y)) for x, y in zip(a, b))
