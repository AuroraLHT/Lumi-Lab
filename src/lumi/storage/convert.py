"""Rewrite a v1 recording as v2, and check the copy against the original.

    python -m lumi.storage.convert OLD.hdf5 [NEW.hdf5] [--no-verify] [--overwrite]

The original is only read, never changed or removed; NEW defaults to OLD's name with
`.v2` before the suffix. Verification reads both files back through
lumi.storage.reader and compares every frame, log cell, integration row, detection
box, mask and pattern; any difference is an error and the new file is deleted.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import h5py
import numpy as np

from lumi.storage.reader import V1, V2
from lumi.storage.v2 import DetectionSchema, LogSchema, RecordingWriter

log = logging.getLogger(__name__)


class VerifyError(AssertionError):
    pass


def default_target(src: Path) -> Path:
    return src.with_name(f"{src.stem}.v2{src.suffix}")


def convert_v1(src: str | Path, dst: str | Path | None = None, *, verify: bool = True,
               overwrite: bool = False, progress=None) -> Path:
    src = Path(src)
    dst = Path(dst) if dst is not None else default_target(src)
    if src.resolve() == dst.resolve():
        raise ValueError("the converted copy must not replace the original")
    progress = progress or (lambda *_: None)
    with h5py.File(src, "r") as f:
        b = V1(f)
        if f.attrs.get("format") is not None:
            raise ValueError(f"{src} is not a v1 recording (format={f.attrs.get('format')!r})")
        writer = RecordingWriter(dst, overwrite=overwrite, **_layout(b, src))
        try:
            _copy(b, writer, progress)
        except BaseException:
            writer.close()
            dst.unlink(missing_ok=True)
            raise
        writer.close()
    if verify:
        try:
            check(src, dst, progress)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
    return dst


def _layout(b: V1, src: Path) -> dict:
    frame_shape = tuple(b.frame_dataset().shape[1:3]) if b.has_frames() else None
    log_schema = None
    if b.has_log():
        table = b.log_table()
        columns = [c for c in b.log_columns() if c != "time"]
        log_schema = LogSchema(columns, {c for c in columns if table[c].dtype == object})
    detection = None
    if b.has_detection():
        pattern = b._get("pattern")
        detection = DetectionSchema(
            classes=b.detection_classes(),
            detector_classes=[str(c) for c in b._get("detection").attrs.get("class_name", [])]
            if b._get("detection") is not None else [],
            pattern_shape=tuple(pattern.shape[1:3]) if pattern is not None else frame_shape,
        )
    attrs = b.attrs()
    return {
        "frame_shape": frame_shape, "log": log_schema, "integration": b.has_integration(),
        "detection": detection,
        "meta": {"converted_from": {"path": str(src), "format_version": 1, "attrs": attrs,
                                    "converted": time.time()}},
        "attrs": {k: v for k, v in attrs.items() if isinstance(v, (int, float, str, bool))},
    }


def _copy(b: V1, w: RecordingWriter, progress) -> None:
    if b.has_frames():
        times = b.frame_times()
        ds = b.frame_dataset()
        n = b.n_frames()
        for start in range(0, n, 64):
            block = ds[start:min(n, start + 64)]
            for j, image in enumerate(block):
                w.add_frame(image, float(times[start + j]) if start + j < len(times) else np.nan)
            w.flush()
            progress("frames", min(n, start + 64), n)

    if b.has_log():
        table = b.log_table()
        times = b.log_times()
        columns = [c for c in b.log_columns() if c != "time"]
        for i in range(b.n_log()):
            w.add_log(float(times[i]), {c: _py(table[c][i]) for c in columns})
        w.flush()
        progress("log", b.n_log(), b.n_log())

    if b.has_integration():
        data = b.integration()
        t = data["time"]
        root = b._get("integration_root")
        n_events = b.n_integration_events()
        spans = root[:n_events] if root is not None and n_events else np.empty((0, 2), int)
        geometry = ("center_x", "center_y", "width", "height")
        for start, end in spans:
            rows = range(int(start), int(end) + 1)
            if not len(rows) or rows[-1] >= len(t):
                continue
            w.add_integration(float(t[rows[0]]), {
                int(data["bbox_id"][r]): {**{k: float(data[k][r]) for k in ("mean", "max", "min")},
                                          **{k: float(data[k][r]) for k in geometry if k in data}}
                for r in rows})
        w.flush()
        progress("integration", n_events, n_events)

    if b.has_detection():
        table = b.detection_table()
        classes = b.detection_classes()
        tracking, num_tracking = b._get("tracking"), b._get("num_tracking")
        n = b.n_detections()
        for d in range(n):
            boxes = [{"bbox_id": x["bbox_id"], "x": x["x0"], "y": x["y0"], "width": x["x1"] - x["x0"],
                      "height": x["y1"] - x["y0"], "score": x["score"], "label": x["label"]}
                     for x in b.detection_boxes(d)]
            tracks = {}
            if tracking is not None and num_tracking is not None:
                k = int(num_tracking[d])
                tracks = {i: int(v) for i, v in enumerate(tracking[d, :k])} if k else {}
            w.add_detection(float(table["time"][d]), str(table["uuid"][d]), boxes,
                            {c: float(table[f"p_{c}"][d]) for c in classes},
                            b.pattern(d), b.detection_masks(d), tracks, None)
            if d % 64 == 63:
                w.flush()
                progress("detections", d + 1, n)
        w.flush()
        progress("detections", n, n)


def _py(v):
    if v is None:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and np.isnan(v):
        return None
    return v


def check(src: str | Path, dst: str | Path, progress=None) -> None:
    """Raise VerifyError unless `dst` holds everything `src` does."""
    progress = progress or (lambda *_: None)
    with h5py.File(src, "r") as fs, h5py.File(dst, "r") as fd:
        a, b = V1(fs), V2(fd)

        def same(what, x, y):
            x, y = np.asarray(x), np.asarray(y)
            if x.dtype.kind in "fc" or y.dtype.kind in "fc":
                ok = x.shape == y.shape and np.array_equal(x.astype(np.float64), y.astype(np.float64),
                                                           equal_nan=True)
            else:
                ok = x.shape == y.shape and np.array_equal(x, y)
            if not ok:
                raise VerifyError(f"{what} differs between {src} and {dst}")

        if a.has_frames():
            n = a.n_frames()
            same("frame count", n, b.n_frames())
            same("frame times", a.frame_times(), b.frame_times())
            for start in range(0, n, 64):
                stop = min(n, start + 64)
                same(f"frames {start}-{stop}", a.frame_dataset()[start:stop], b.frame_dataset()[start:stop])
                progress("verify frames", stop, n)
        if a.has_log():
            same("log times", a.log_times(), b.log_times())
            ta, tb = a.log_table(), b.log_table()
            for c in a.log_columns():
                if c == "time":
                    continue
                if ta[c].dtype == object:
                    if [None if v in (None, "") else str(v) for v in ta[c]] != list(tb[c]):
                        raise VerifyError(f"log column {c!r} differs")
                else:
                    same(f"log column {c!r}", ta[c], tb[c])
        if a.has_integration():
            ia, ib = a.integration(), b.integration()
            n = len(ib["time"])
            for k in ("time", "bbox_id", "mean", "max", "min"):
                same(f"integration {k}", ia[k][:n], ib[k])
        if a.has_detection():
            n = a.n_detections()
            same("detection count", n, b.n_detections())
            da, db = a.detection_table(), b.detection_table()
            same("detection times", da["time"], db["time"])
            for d in range(n):
                ba, bb = a.detection_boxes(d), b.detection_boxes(d)
                for k in ("x0", "y0", "x1", "y1", "label", "score"):
                    same(f"detection {d} boxes {k}", [x[k] for x in ba], [x[k] for x in bb])
                ma, mb = a.detection_masks(d), b.detection_masks(d)
                for i, m in ma.items():
                    same(f"detection {d} mask {i}", m.astype(bool), mb[i])
                pa, pb = a.pattern(d), b.pattern(d)
                if pa is not None:
                    same(f"detection {d} pattern", pa.astype(np.float32), pb)
                if d % 64 == 63:
                    progress("verify detections", d + 1, n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lumi.storage.convert", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path, nargs="?")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    last = [0.0]

    def progress(what, done, total):
        if time.monotonic() - last[0] > 2 or done == total:
            last[0] = time.monotonic()
            print(f"  {what}: {done}/{total}", file=sys.stderr)

    t0 = time.monotonic()
    out = convert_v1(args.src, args.dst, verify=not args.no_verify, overwrite=args.overwrite,
                     progress=progress)
    size_a, size_b = args.src.stat().st_size, out.stat().st_size
    print(f"{out}: {size_b / 1e9:.2f} GB from {size_a / 1e9:.2f} GB "
          f"({100 * (1 - size_b / size_a):.0f}% smaller), {time.monotonic() - t0:.0f} s"
          + ("" if args.no_verify else ", verified"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
