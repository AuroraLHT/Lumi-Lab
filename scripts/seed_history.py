#!/usr/bin/env python
"""Seed the simulator with a fake growth history, so a history UI has something to show.

    .venv/bin/python scripts/seed_history.py            # add the history
    .venv/bin/python scripts/seed_history.py --purge    # remove exactly what it added

What it writes, back-dated over the past three weeks and consistent with each other:

- growth.db: two projects, four substrates and their position samples, one chamber
  session per growth day with its full step journal, experiment + record rows, and
  measurements (in-situ RHEED metric, ex-situ XRD/AFM/transport).
- RHEED recordings: one HDF5 per deposition in the storage node's folder, laid out by
  the real `Recorder` (frame, frame_meta, log, integration*). Frames are the simcam's
  test frame with RHEED oscillations on the specular spot that damp faster on a
  worse growth; the integration boxes carry the same curve at 5 Hz.
- Chamber logs: one PASCAL-format CSV per session in the simulator's log folder.

The chamber log is not drawn by hand. Each session drives the simulator's ChamberModel
through the sequence its steps describe (warm-up, gas, target, preablation, deposition,
cool-down) and renders its rows, so the CSV, the step journal and the log embedded in
each recording tell the same story at the same timestamps.

Safe to run against a live simulation stack:
- growth.db is shared with the running experiment node; SQLite serialises the writes,
  and nothing here touches the node's current session or loaded substrate.
- The pascal sim tails its log folder by watchdog *modify* events, so a CSV written in
  place would hijack the live chamber stream. Each CSV is written outside the folder
  and renamed in, which raises no modify event.

Everything created is listed in a manifest (run/simulation/seed_history.json by
default). --purge reads it and deletes those rows and files and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import math
import os
import random
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from lumi.config import settings
from lumi.experiment.db import GrowthDB
from lumi.pascal.chamber_log import process_row
from lumi.pascal.sim import LOG_COLUMNS, ChamberModel, ChamberSimConfig, render_row
from lumi.storage.record import Recorder, RecorderConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = "seed_history"
MANIFEST_VERSION = 1

#: The sim chamber's carousel (PLDconfig_07102025.ini [TGsettings]).
TARGETS = {"B": "TbFeO3", "C": "La0.7Sr0.3MnO3", "D": "GdFeO3", "E": "SmFeO3"}
CENTER_MASK_POS = 97.2
MASK_BLOCK_POS = 75.0


# --- the history to write -------------------------------------------------------

@dataclass
class Growth:
    pixel: int
    target: str
    temperature: float
    pressure: float  # Torr
    rate: float  # Hz
    pulses: int
    laser_power: float
    #: 0..1 -- how clean the RHEED oscillations are, and what the metrics report.
    quality: float
    #: Pulses per unit cell, i.e. the oscillation period.
    period: float = 50.0
    #: Fraction of the pulses fired before the laser trips; None for a clean run.
    fail_at: float | None = None
    #: A later layer on the same sample (single-deposition bilayer): skip the pixel move.
    same_sample: bool = False

    @property
    def material(self) -> str:
        return TARGETS[self.target]


@dataclass
class SubstrateSpec:
    key: str
    name: str
    materials: str
    orientation: str
    width: float
    height: float
    positions: list[float]
    manufacture: str
    thickness: float = 0.5
    pixel_spacing: float = 1.0

    @property
    def is_pixel(self) -> bool:
        return len(self.positions) > 1


@dataclass
class SessionSpec:
    days_ago: int
    start: tuple[int, int]
    actor: str
    project: str
    substrate: str
    growths: list[Growth]
    register: bool = False
    align_mask: bool = False
    anneal: list[tuple[float, float]] = field(default_factory=list)  # (temperature, hold s)
    finish_substrate: bool = False


PROJECTS = {
    "rfo": ("RFeO3-BO", "Bayesian search over the growth window of orthoferrite films "
                        "(TbFeO3, GdFeO3, SmFeO3) on SrTiO3(001)."),
    "lsmo": ("LSMO-bilayers", "La0.7Sr0.3MnO3 bottom electrodes, some with a TbFeO3 cap. "
                              "Single-deposition substrates."),
}

SUBSTRATES = {
    "rfo1": SubstrateSpec("rfo1", "STO-RFO-01", "SrTiO3", "001", 10.0, 10.0,
                          [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0], "CrysTec"),
    "rfo2": SubstrateSpec("rfo2", "STO-RFO-02", "SrTiO3", "001", 10.0, 10.0,
                          [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0], "CrysTec"),
    "lsmo1": SubstrateSpec("lsmo1", "STO-LSMO-01", "SrTiO3", "001", 5.0, 5.0, [0.0], "MTI"),
    "lsmo2": SubstrateSpec("lsmo2", "YSZ-LSMO-02", "YSZ", "111", 5.0, 5.0, [0.0], "MTI"),
}

SESSIONS = [
    SessionSpec(19, (9, 40), "operator", "rfo", "rfo1", register=True, align_mask=True, growths=[
        Growth(0, "B", 650, 0.05, 2.0, 400, 1.2, quality=0.55, period=55),
        Growth(1, "B", 700, 0.05, 3.0, 400, 1.2, quality=0.80, period=50),
    ]),
    SessionSpec(16, (13, 20), "bo-agent", "rfo", "rfo1", growths=[
        Growth(2, "B", 700, 0.10, 3.0, 450, 1.3, quality=0.70, period=48),
        Growth(3, "D", 750, 0.10, 3.0, 450, 1.3, quality=0.85, period=45),
    ]),
    SessionSpec(13, (10, 10), "bo-agent", "rfo", "rfo1", growths=[
        Growth(4, "D", 720, 0.02, 5.0, 500, 1.4, quality=0.40, period=42, fail_at=0.55),
        Growth(5, "D", 720, 0.02, 5.0, 500, 1.4, quality=0.45, period=42),
    ]),
    SessionSpec(9, (14, 0), "operator", "lsmo", "lsmo1", register=True, growths=[
        Growth(0, "C", 680, 0.15, 5.0, 600, 1.5, quality=0.90, period=60),
        Growth(0, "B", 650, 0.05, 3.0, 300, 1.2, quality=0.60, period=50, same_sample=True),
    ], finish_substrate=True),
    SessionSpec(6, (10, 30), "operator", "lsmo", "lsmo2", register=True, growths=[
        Growth(0, "C", 700, 0.10, 5.0, 600, 1.5, quality=0.65, period=60),
    ], anneal=[(750, 300.0), (600, 120.0)], finish_substrate=True),
    SessionSpec(2, (11, 0), "bo-agent", "rfo", "rfo2", register=True, align_mask=True, growths=[
        Growth(0, "E", 700, 0.05, 3.0, 400, 1.3, quality=0.75, period=52),
        Growth(1, "E", 740, 0.08, 3.0, 400, 1.3, quality=0.88, period=50),
    ]),
]


# --- time -------------------------------------------------------------------------

def utc_text(when: dt.datetime) -> str:
    """A local wall-clock time -> the CURRENT_TIMESTAMP form growth.db stores (UTC).

    Times here are naive local, like the chamber's own: PASCAL logs local wall-clock
    time with no zone, and so does the recorder's copy of it."""
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- RHEED ----------------------------------------------------------------------

#: (x0, y0, x1, y1) on the 720x540 test frame: the specular spot and a side spot.
BBOXES = {0: (322, 132, 352, 162), 1: (253, 163, 283, 193)}


class FakeRheed:
    """The simcam test frame, modulated the way a growing film modulates RHEED.

    The pattern is `bg + (base - bg) * G`, where G is 1 everywhere before growth and,
    during it, oscillates on the specular spot (one period per unit cell, damping with
    how rough the growth is) while the diffuse background rises as the surface roughens.
    """

    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng
        self.base = np.load(PROJECT_ROOT / "src/lumi/rheed/assets/test_frame.npy").astype(np.float32)
        self.bg = float(np.percentile(self.base, 5))
        self.signal = self.base - self.bg
        h, w = self.base.shape
        yy, xx = np.mgrid[0:h, 0:w]

        def blob(box):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            return np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 14.0 ** 2)).astype(np.float32)

        self.w_spec, self.w_side = blob(BBOXES[0]), blob(BBOXES[1])
        # Each box's stats are linear in the three gains, so they can be integrated at
        # 5 Hz without rendering a frame: precompute each term's box mean/max/min pixel.
        self._box_terms = {}
        for bbox_id, (x0, y0, x1, y1) in BBOXES.items():
            s = self.signal[y0:y1, x0:x1]
            ws, wd = self.w_spec[y0:y1, x0:x1], self.w_side[y0:y1, x0:x1]
            hi, lo = np.unravel_index(np.argmax(s), s.shape), np.unravel_index(np.argmin(s), s.shape)
            self._box_terms[bbox_id] = {
                "mean": (s.mean(), (s * ws).mean(), (s * wd).mean()),
                "max": (s[hi], s[hi] * ws[hi], s[hi] * wd[hi]),
                "min": (s[lo], s[lo] * ws[lo], s[lo] * wd[lo]),
                "height": y1 - y0, "width": x1 - x0,
                "center_x": (x0 + x1) / 2, "center_y": (y0 + y1) / 2,
            }

    @staticmethod
    def gains(growth: Growth, deposited: float, since_end: float | None, brightness: float):
        """(diffuse, specular, side) gains after `deposited` pulses, `since_end` s after
        the laser stopped (None while still growing or before it started)."""
        q = growth.quality
        u = deposited / growth.period
        progress = min(1.0, deposited / growth.pulses)
        amplitude = 0.25 + 0.35 * q
        damping = 1.5 + 8.0 * q  # in unit cells
        rough = 0.55 * (1 - q) * progress
        dip = amplitude * (0.5 - 0.5 * math.cos(2 * math.pi * u)) * math.exp(-u / damping)
        spec = 1.0 - dip - rough
        if since_end is not None:
            # Surface recovery once the plume stops: the specular spot comes partly back.
            spec += (1.0 - spec) * 0.6 * q * (1 - math.exp(-since_end / 12.0))
        side = 1.0 - 0.45 * (1.0 - spec)
        diffuse = 1.0 + 0.6 * (1 - q) * progress
        return diffuse * brightness, spec * brightness, side * brightness

    def frame(self, gains) -> np.ndarray:
        diffuse, spec, side = gains
        g = diffuse + (spec - diffuse) * self.w_spec + (side - diffuse) * self.w_side
        img = self.bg + self.signal * g
        img += self.rng.normal(0.0, 6.0, img.shape).astype(np.float32)
        return np.clip(img, 0, 4095).astype(np.uint16)

    def integrate(self, gains) -> dict[int, dict]:
        diffuse, spec, side = gains
        out = {}
        for bbox_id, t in self._box_terms.items():
            def value(key, noise):
                base, ws, wd = t[key]
                v = self.bg + diffuse * base + (spec - diffuse) * ws + (side - diffuse) * wd
                return float(np.clip(v + self.rng.normal(0.0, noise), 0, 4095))
            out[bbox_id] = {
                "mean": value("mean", 1.5), "max": value("max", 8.0), "min": value("min", 3.0),
                "height": t["height"], "width": t["width"],
                "center_x": t["center_x"], "center_y": t["center_y"],
            }
        return out


class Recording:
    """One HDF5 file, created by the storage node's own Recorder so the layout is the
    real one, and filled through its save_* methods."""

    def __init__(self, root: Path, storage_name: str, log_columns: list[str]) -> None:
        cfg = settings.storage.hdf5_recorder
        self.recorder = Recorder(RecorderConfig(
            project_name=storage_name, root_folder=str(root),
            frame_dim=(540, 720), frame_meta_columns=["time_stamp", "time"],
            log_columns=log_columns,
            pattern_dim=None, detection_meta_columns=None,
            detector_classes=None, classifier_classes=None,
            initial_size=cfg.initial_size,
            save_frame=True, save_log=True, save_ai=False, save_integration=True,
            compression="lzf", compression_opts=None, scaleoffset=0,
        ))
        created = self.recorder.create_datasets()
        if not created["succ"]:
            raise RuntimeError(f"could not create datasets: {created['error_message']}")
        self.path = root / f"{storage_name}.hdf5"

    @staticmethod
    def _meta(when: dt.datetime) -> dict:
        return {"time_stamp": when.strftime("%Y-%m-%d %H:%M:%S.%f"), "time": when.timestamp()}

    def frame(self, when: dt.datetime, image: np.ndarray) -> None:
        self.recorder.save_frame(image, self._meta(when))

    def integration(self, when: dt.datetime, values: dict[int, dict]) -> None:
        self.recorder.save_integrations_buffered(values, self._meta(when))

    def log(self, values: dict) -> None:
        self.recorder.save_log(values)

    def close(self, when: dt.datetime) -> None:
        self.recorder.close_h5()
        os.utime(self.path, (when.timestamp(), when.timestamp()))


# --- the chamber ----------------------------------------------------------------

class Chamber:
    """ChamberModel on a back-dated clock. Every simulated second becomes a CSV row,
    and -- while a recording is open -- a log row, frames and integrations in it."""

    TICK = 0.2  # s: the integration rate (5 Hz); the log is written every 5th tick

    def __init__(self, start: dt.datetime, csv_path: Path, rheed: FakeRheed, frame_interval: float,
                 rng: random.Random) -> None:
        self.model = ChamberModel(ChamberSimConfig(seed=rng.randrange(1 << 30)))
        self.model.set_log_interval(1)
        self.model.set_sample_rotation_mode("ON")
        self.model.move_mask(1, CENTER_MASK_POS)
        self.model.tick(30.0)
        self.now = start
        self.rheed = rheed
        self.frame_interval = frame_interval
        self.rng = rng
        self.csv_path = csv_path
        self._csv_file = open(csv_path, "w", newline="")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(LOG_COLUMNS)
        self._ticks = 0
        self.log_columns: list[str] | None = None
        # The open recording and the growth it is watching.
        self.recording: Recording | None = None
        self.growth: Growth | None = None
        self.brightness = 1.0
        self._pulses_at_start: int | None = None
        self._laser_stopped_at: dt.datetime | None = None
        self._next_frame: dt.datetime | None = None
        self.row()  # the first row, so the column list is known before anything records

    # --- clock

    def row(self) -> dict:
        values = self.model.snapshot()
        text = render_row(values, self.now)
        self._csv.writerow(text)
        processed = process_row(dict(zip(LOG_COLUMNS, text)))
        # process_row stamps rows with *today's* date (it only ever sees live rows);
        # a back-dated row needs its own.
        stamp = self.now.replace(microsecond=0)
        processed["Time"] = processed["time_stamp"] = stamp.isoformat()
        processed["time"] = stamp.timestamp()
        if self.log_columns is None:
            self.log_columns = list(processed)
        return processed

    def advance(self, seconds: float) -> None:
        for _ in range(max(1, round(seconds / self.TICK))):
            self.model.tick(self.TICK)
            self.now += dt.timedelta(seconds=self.TICK)
            self._ticks += 1
            if self._ticks % 5 == 0:
                processed = self.row()
                if self.recording is not None:
                    self.recording.log(processed)
            if self.recording is not None:
                self._observe()

    def until(self, done, limit: float, step: float = 1.0) -> None:
        waited = 0.0
        while not done() and waited < limit:
            self.advance(step)
            waited += step

    def pause(self, lo: float, hi: float) -> None:
        """Someone reading a screen, clicking a button: dead time between ops."""
        self.advance(self.rng.uniform(lo, hi))

    # --- RHEED while recording

    def _observe(self) -> None:
        growth = self.growth
        deposited = 0.0
        since_end = None
        if self._pulses_at_start is not None:
            deposited = float(self.model.laser_pulses - self._pulses_at_start)
            if self._laser_stopped_at is not None:
                since_end = (self.now - self._laser_stopped_at).total_seconds()
        gains = self.rheed.gains(growth, deposited, since_end, self.brightness)
        self.recording.integration(self.now, self.rheed.integrate(gains))
        if self.now >= self._next_frame:
            self.recording.frame(self.now, self.rheed.frame(gains))
            self._next_frame = self.now + dt.timedelta(seconds=self.frame_interval)

    def start_recording(self, recording: Recording, growth: Growth, brightness: float) -> None:
        self.recording, self.growth, self.brightness = recording, growth, brightness
        self._pulses_at_start = None
        self._laser_stopped_at = None
        self._next_frame = self.now

    def mark_deposition(self, started: bool) -> None:
        if started:
            self._pulses_at_start = self.model.laser_pulses
        else:
            self._laser_stopped_at = self.now

    def stop_recording(self) -> Recording:
        recording, self.recording, self.growth = self.recording, None, None
        recording.close(self.now)
        return recording

    def close(self) -> None:
        self._csv_file.close()
        os.utime(self.csv_path, (self.now.timestamp(), self.now.timestamp()))


# --- the journal ----------------------------------------------------------------

class Seeder:
    def __init__(self, db: GrowthDB, args, rng: random.Random) -> None:
        self.db = db
        self.args = args
        self.rng = rng
        self.rheed = FakeRheed(np.random.default_rng(args.seed))
        self.manifest: dict[str, list] = {k: [] for k in (
            "project", "substrate", "sample", "chamber_session", "step",
            "experiment", "record", "measurement", "files")}
        self.project_ids: dict[str, int] = {}
        self.substrate_ids: dict[str, int] = {}
        self.samples: dict[tuple[str, int], int] = {}
        self.quality: dict[int, list[tuple[Growth, int, int, int, dt.datetime]]] = {}
        # Per session
        self.chamber: Chamber | None = None
        self.session_id: int | None = None
        self.last_step: int | None = None
        self.actor = "operator"
        self.sample_id: int | None = None

    # --- rows

    async def _backdate(self, table: str, pk: str, row_id: int, column: str, when: dt.datetime) -> None:
        await self.db.conn.execute(f"UPDATE {table} SET {column} = ? WHERE {pk} = ?", (utc_text(when), row_id))
        await self.db.conn.commit()

    async def step(self, kind: str, params: dict | None = None, *, run=None, result=None,
                   ok: bool = True, error: str | None = None, sample: bool = True):
        """One journal row, opened now, closed after `run` (a sync callable that drives
        the chamber, and may return the result) has advanced the clock."""
        started = self.chamber.now
        step_id = await self.db.add_step(
            kind, step_uuid=uuid.uuid4().hex, started_at=started.timestamp(),
            session_id=self.session_id, parent_step_id=self.last_step,
            sample_id=self.sample_id if sample else None,
            params=json.dumps(params) if params else None,
            actor=self.actor, source=SOURCE,
        )
        self.manifest["step"].append(step_id)
        self.last_step = step_id
        if run is not None:
            out = run()
            if out is not None:
                result = out
        else:
            self.chamber.advance(self.rng.uniform(0.3, 2.0))
        if result is None:
            result = {"ok": ok}
        await self.db.finish_step(step_id, ok=ok, ended_at=self.chamber.now.timestamp(),
                                  result=json.dumps(result), error=error)
        return step_id, result

    # --- a session

    async def run_session(self, spec: SessionSpec) -> None:
        today = dt.datetime.now()
        start = (today - dt.timedelta(days=spec.days_ago)).replace(
            hour=spec.start[0], minute=spec.start[1], second=self.rng.randrange(60), microsecond=0)
        log_name = f"chamber_log_{start:%Y%m%d_%H%M%S}.csv"
        staging = self.args.staging / log_name
        self.chamber = Chamber(start, staging, self.rheed, self.args.frame_interval, self.rng)
        self.actor = spec.actor
        self.sample_id = None
        self.last_step = None

        self.session_id = await self.db.start_chamber_session(f"experiment-{SOURCE}")
        self.manifest["chamber_session"].append(self.session_id)
        ch, m = self.chamber, self.chamber.model
        sub = SUBSTRATES[spec.substrate]
        print(f"session {start:%Y-%m-%d %H:%M} {spec.actor:9s} {sub.name}: "
              f"{len(spec.growths)} growth(s)")

        await self.step("start_mi_logging", {"interval_s": 1, "file_name": ""},
                        result={"file_name": log_name, "interval_s": 1}, sample=False)
        ch.pause(20, 90)

        project_name = PROJECTS[spec.project][0]
        if spec.project not in self.project_ids:
            pid = await self.db.add_project(*PROJECTS[spec.project])
            await self._backdate("project", "project_id", pid, "project_created_at", ch.now)
            self.project_ids[spec.project] = pid
            self.manifest["project"].append(pid)
            await self.step("register_project",
                            {"project_name": project_name, "description": PROJECTS[spec.project][1]},
                            result={"project_id": pid, "project_name": project_name}, sample=False)
            ch.pause(10, 40)

        if spec.register:
            await self._register_substrate(sub)
        else:
            await self.step("resume_substrate", {"substrate_id": self.substrate_ids[sub.key]},
                            result={"substrate_id": self.substrate_ids[sub.key], "substrate_name": sub.name})
        self.sample_id = self.samples[(sub.key, spec.growths[0].pixel)]
        ch.pause(10, 60)

        # Warm-up: gas in, heater on, ramp to the first growth temperature.
        first = spec.growths[0]
        await self.step("set_mfc_control", {"enabled": True}, run=lambda: m.set_mfc_control(True))
        await self.step("set_pressure_control", {"on": True}, run=lambda: m.set_pressure_control(True))
        await self._set_pressure(first.pressure)
        await self.step("initiate_heating_laser", run=lambda: self._heater_on())
        ch.pause(5, 20)
        await self._to_temperature(first.temperature)

        if spec.align_mask:
            await self._align_mask()

        for growth in spec.growths:
            await self._grow(spec, sub, growth, project_name)

        for temperature, hold in spec.anneal:
            await self._anneal(temperature, hold)

        if spec.finish_substrate:
            await self.step("finish_substrate")

        # Cool-down and shutdown.
        await self.step("cool_down", {"ramp_rate": 20.0}, run=lambda: self._ramp(160.0))
        await self.step("turn_off_heating_laser", run=lambda: m.set_heating_laser(False))
        await self.step("set_pressure_control", {"on": False}, run=lambda: self._gas_off())
        await self.step("set_mfc_control", {"enabled": False}, run=lambda: m.set_mfc_control(False))
        ch.pause(10, 30)
        await self.step("stop_mi_logging", sample=False)

        ch.close()
        await self.db.conn.execute(
            "UPDATE chamber_session SET started_at = ?, ended_at = ? WHERE session_id = ?",
            (utc_text(start), utc_text(ch.now), self.session_id))
        await self.db.conn.commit()
        final = self.args.log_dir / log_name
        os.replace(staging, final)  # a rename: no modify event for the live log reader
        self.manifest["files"].append(str(final))

    async def _register_substrate(self, sub: SubstrateSpec) -> None:
        ch = self.chamber
        sid = await self.db.add_substrate(
            materials=sub.materials, orientation=sub.orientation, width=sub.width,
            height=sub.height, thickness=sub.thickness, pixel_spacing=sub.pixel_spacing,
            positions=json.dumps(sub.positions), manufacture=sub.manufacture,
            manufacture_date=(ch.now - dt.timedelta(days=120)).strftime("%Y-%m-%d"),
            substrate_name=sub.name, created_at=utc_text(ch.now),
        )
        self.substrate_ids[sub.key] = sid
        self.manifest["substrate"].append(sid)
        row = await self.db.get_substrate(sid)
        root = await self.db.add_sample(sid, kind="substrate", sample_name=sub.name,
                                        state="active", sample_uuid=row["substrate_uuid"])
        created = [root]
        for index, position in enumerate(sub.positions):
            sample = await self.db.add_sample(sid, kind="position", pixel_index=index,
                                              position_mm=position, parent_sample_id=root,
                                              sample_name=f"{sub.name}-p{index}")
            self.samples[(sub.key, index)] = sample
            created.append(sample)
        for sample in created:
            await self._backdate("sample", "sample_id", sample, "created_at", ch.now)
        self.manifest["sample"].extend(created)

        positions = [{"index": i, "position": p, "mask_position": CENTER_MASK_POS + p,
                      "rheed_position": -p, "accessible": True} for i, p in enumerate(sub.positions)]
        await self.step("register_substrate", {
            "materials": sub.materials, "orientation": sub.orientation, "width": sub.width,
            "height": sub.height, "thickness": sub.thickness, "substrate_name": sub.name,
            "manufacturer": sub.manufacture, "manufacture_date": None,
            "pixel_spacing": sub.pixel_spacing, "positions": sub.positions,
        }, result={"substrate_id": sid, "substrate_uuid": row["substrate_uuid"],
                   "materials": sub.materials, "orientation": sub.orientation,
                   "width": sub.width, "height": sub.height, "thickness": sub.thickness,
                   "pixel_spacing": sub.pixel_spacing, "positions": positions,
                   "current_pixel_index": 0}, sample=False)

    # --- chamber actions (sync: they only move the clock)

    def _heater_on(self) -> None:
        m = self.chamber.model
        m.set_heating_lock(False)
        m.set_heating_laser(True)
        m.set_temperature_control("PID")
        m.set_temperature_ramp(20.0, True)
        self.chamber.advance(5)

    def _ramp(self, temperature: float) -> None:
        m = self.chamber.model
        m.set_temperature(temperature)
        self.chamber.until(m.at_temperature, limit=4 * 3600)
        self.chamber.advance(self.rng.uniform(30, 120))  # let it settle

    def _gas_off(self) -> None:
        m = self.chamber.model
        m.set_pressure_control(False)
        m.mfc_set = {1: 0.0, 2: 0.0}
        self.chamber.advance(60)

    def _move_mask(self, position: float) -> None:
        self.chamber.model.move_mask(1, position)
        self.chamber.until(lambda: self.chamber.model.motor_free, limit=60, step=0.2)

    def _fire(self, pulses: int, rate: float) -> None:
        m = self.chamber.model
        m.trigger_laser(pulses, rate)
        self.chamber.until(lambda: m.laser_idle, limit=pulses / rate + 60, step=0.2)

    # --- journalled ops

    async def _set_pressure(self, pressure: float) -> None:
        m = self.chamber.model

        def run():
            m.set_pressure(pressure)
            self.chamber.advance(self.rng.uniform(90, 150))  # the chamber takes ~2 min to settle
        await self.step("set_pressure", {"pressure": pressure}, run=run)

    async def _to_temperature(self, temperature: float) -> None:
        await self.step("to_temperature", {"temperature": temperature, "ramp_rate": 20.0},
                        run=lambda: self._ramp(temperature))

    async def _align_mask(self) -> None:
        ch, m = self.chamber, self.chamber.model
        await self.step("move_mask_to_position", {"position": CENTER_MASK_POS},
                        run=lambda: self._move_mask(CENTER_MASK_POS))
        previous = CENTER_MASK_POS
        center = previous + self.rng.uniform(-0.4, 0.4)

        def run():
            samples = []
            for i in range(17):
                position = previous - 4.0 + 0.5 * i
                self._move_mask(position)
                ch.advance(1.0)
                bump = 95.0 * math.exp(-((position - center) / 0.8) ** 2)
                samples.append({"position": position,
                                "reading": round(100.0 + bump + self.rng.gauss(0, 2.0), 2)})
            self._move_mask(center)
            return {"ok": True, "marker_id": "m1", "previous_center": previous, "center": center,
                    "applied": True, "contrast": 95.0, "baseline": 100.0, "polarity": 1,
                    "samples": samples}
        await self.step("auto_align_center_mask", {
            "half_window_mm": 4.0, "step_mm": 0.5, "backoff_mm": 5.0, "frames_per_point": 2,
            "min_contrast": 5.0, "apply": True, "center_mm": None}, run=run)
        ch.pause(10, 30)

    async def _anneal(self, temperature: float, hold: float) -> None:
        def run():
            self._ramp(temperature)
            self.chamber.advance(hold)
        await self.step("anneal", {"steps": [{"temperature": temperature, "ramp_rate": 20.0,
                                              "wait_time": hold}]}, run=run)

    async def _grow(self, spec: SessionSpec, sub: SubstrateSpec, g: Growth, project_name: str) -> None:
        ch, m = self.chamber, self.chamber.model
        self.sample_id = self.samples[(sub.key, g.pixel)]
        position = sub.positions[g.pixel]
        mask_position = CENTER_MASK_POS + position

        if not g.same_sample:
            await self.step("to_current_pixel", run=lambda: self._move_mask(mask_position),
                            result={"index": g.pixel, "mask_position": mask_position,
                                    "rheed_position": -position})
        if abs(m.temperature_target - g.temperature) > 0.5:
            await self._to_temperature(g.temperature)
        if abs(m.pressure_setpoint - g.pressure) > 1e-9:
            await self._set_pressure(g.pressure)

        def select():
            m.select_target(g.target)
            ch.until(lambda: m.motor_free, limit=60, step=0.2)
        await self.step("set_target", {"target_id": g.target, "rotation_mode": "AUTO",
                                       "twist_mode": "AUTO"}, run=select)
        ch.pause(5, 20)

        measured = round(g.laser_power + self.rng.uniform(-0.04, 0.04), 3)
        await self.step("begin_set_laser_power", {"laser_power": g.laser_power, "target_id": g.target,
                                                  "force": False})
        ch.pause(30, 90)  # someone reading the power meter
        await self.step("confirm_laser_power", {"measured_power": measured},
                        result={"measured_power": measured})

        def preablate():
            m.set_shutter(False)
            self._move_mask(MASK_BLOCK_POS)
            self._fire(600, 10.0)
            self._move_mask(mask_position)
        await self.step("perform_preablation", {"target_id": g.target, "num_pulse": 600, "frequency": 10.0,
                                                "is_dryrun": False, "move_mask_to_block_position": True},
                        run=preablate)

        gain = round(self.rng.uniform(80, 120))
        await self.step("begin_adjust_rheed_gain")
        await self.step("set_rheed_gain", {"gain": float(gain)})
        ch.pause(5, 20)
        await self.step("confirm_rheed_gain")

        record_uuid = str(uuid.uuid4())
        storage_name = f"{sub.name}-p{g.pixel}_{project_name}_{record_uuid}"
        recording = Recording(self.args.h5_root, storage_name, ch.log_columns)
        self.manifest["files"].append(str(recording.path))
        ch.start_recording(recording, g, brightness=gain / 100.0)
        await self.step("start_storage", {
            "project_name": project_name, "is_dryrun": False, "save_frame": True, "save_ai": False,
            "save_log": True, "save_integration": True, "force_rewrite": False,
        }, result={"ok": True, "record_uuid": record_uuid, "storage_name": storage_name,
                   "path": str(recording.path), "message": "recording"})
        ch.advance(self.rng.uniform(15, 25))  # a flat baseline before the plume

        fired = g.pulses if g.fail_at is None else int(g.pulses * g.fail_at)
        error = None
        if g.fail_at is not None:
            error = (f"laser interlock tripped after {fired}/{g.pulses} pulses "
                     f"(MissedPuls rose by {g.pulses - fired})")

        def deposit():
            m.set_shutter(True)
            ch.until(lambda: m.shutter_idle, limit=5, step=0.2)
            ch.mark_deposition(True)
            self._fire(fired, g.rate)
            if g.fail_at is not None:
                m.missed_pulses += g.pulses - fired
            ch.mark_deposition(False)
            m.set_shutter(False)
            ch.until(lambda: m.shutter_idle, limit=5, step=0.2)
        dep_step, _ = await self.step("perform_deposition", {
            "target_id": g.target, "num_pulse": g.pulses, "target_material": g.material,
            "laser_repetition_rate": g.rate, "is_dryrun": False,
            "temperature": g.temperature, "pressure": g.pressure,
        }, run=deposit, ok=g.fail_at is None, error=error)

        ch.advance(self.rng.uniform(25, 40))  # watch the surface recover
        await self.step("end_storage", {"is_dryrun": False},
                        run=lambda: None, result={"ok": True, "message": "stopped"})
        ch.stop_recording()

        experiment_uuid = str(uuid.uuid4())
        exp_id = await self.db.add_experiment(
            substrate_id=self.substrate_ids[sub.key], is_pixel=sub.is_pixel,
            pixel_location=g.pixel, experiment_uuid=experiment_uuid,
            project_id=self.project_ids[spec.project], temperature=g.temperature,
            pressure=g.pressure, laser_power=measured, laser_pulse_rate=g.rate,
            target_material=g.material, num_pulse=fired,
        )
        rec_id = await self.db.add_recording(exp_id, storage_name, record_uuid)
        await self._backdate("experiment", "experiment_id", exp_id, "experiment_created_at", ch.now)
        await self._backdate("record", "record_id", rec_id, "record_created_at", ch.now)
        self.manifest["experiment"].append(exp_id)
        self.manifest["record"].append(rec_id)
        await self.step("finish_experiment_record", {
            "substrate_id": self.substrate_ids[sub.key], "project_id": self.project_ids[spec.project],
            "experiment_uuid": experiment_uuid, "temperature": g.temperature, "pressure": g.pressure,
            "laser_power": measured, "laser_repetition_rate": g.rate, "target_material": g.material,
            "num_pulse": fired, "is_pixel": sub.is_pixel, "pixel_index": g.pixel,
            "storage_name": storage_name, "record_uuid": record_uuid,
        }, result={"experiment_id": exp_id})

        if g.fail_at is not None:
            await self.db.conn.execute(
                "UPDATE sample SET state = 'active', notes = ? WHERE sample_id = ?",
                (f"deposition aborted: {error}", self.sample_id))
            await self.db.conn.commit()
            ch.pause(60, 180)
            return

        self.quality.setdefault(self.sample_id, []).append((g, dep_step, exp_id, rec_id, ch.now))
        metric = await self._rheed_metric(g, dep_step, exp_id, rec_id)
        await self.step("add_measurement", {
            "sample_id": self.sample_id, "kind": "rheed_metric", "value": metric["value"],
            "detail": metric["detail"], "source": "rheed oscillation fit",
            "step_id": dep_step, "record_id": rec_id,
        }, result={"measurement_id": metric["id"]})

        last_layer = g is spec.growths[-1] or not spec.growths[spec.growths.index(g) + 1].same_sample
        if last_layer and sub.is_pixel:
            await self.step("finish_current_pixel", {"material": g.material, "thickness": None})
        if last_layer:
            await self.db.set_sample_state(self.sample_id, "grown")
        ch.pause(20, 90)

    # --- measurements

    async def _measure(self, kind: str, value: float, detail: dict, source: str, when: dt.datetime,
                       *, step_id=None, experiment_id=None, record_id=None, sample_id=None) -> int:
        mid = await self.db.add_measurement(
            sample_id or self.sample_id, kind, value=value, detail=json.dumps(detail), source=source,
            step_id=step_id, experiment_id=experiment_id, record_id=record_id)
        await self._backdate("measurement", "measurement_id", mid, "created_at", when)
        self.manifest["measurement"].append(mid)
        return mid

    async def _rheed_metric(self, g: Growth, step_id: int, exp_id: int, rec_id: int) -> dict:
        q = g.quality
        detail = {
            "metric": round(q + self.rng.gauss(0, 0.03), 4),
            "period_pulses": round(g.period + self.rng.gauss(0, 1.0), 2),
            "oscillations_seen": int(1.5 + 8.0 * q),
            "amplitude": round(0.25 + 0.35 * q, 3),
            "recovery": round(0.6 * q, 3),
            "unit_cells": round(g.pulses / g.period, 1),
        }
        mid = await self._measure("rheed_metric", detail["metric"], detail, "rheed oscillation fit",
                                  self.chamber.now, step_id=step_id, experiment_id=exp_id,
                                  record_id=rec_id)
        return {"id": mid, "value": detail["metric"], "detail": detail}

    async def ex_situ(self) -> None:
        """XRD and AFM on most grown samples, transport on the electrodes -- measured a
        day or two after growth, as they would be."""
        for sample_id, layers in self.quality.items():
            g, _, exp_id, _, grown_at = layers[-1]
            q = sum(layer[0].quality for layer in layers) / len(layers)
            when = grown_at + dt.timedelta(days=1, hours=self.rng.uniform(1, 6))
            if self.rng.random() < 0.85:
                fwhm = round(0.03 + 0.25 * (1 - q) + abs(self.rng.gauss(0, 0.01)), 4)
                thickness = round(sum(layer[0].pulses / layer[0].period for layer in layers) * 0.39, 1)
                await self._measure("xrd", fwhm, {
                    "rocking_curve_fwhm_deg": fwhm,
                    "peak_2theta_deg": round(46.5 + self.rng.gauss(0, 0.15), 3),
                    "thickness_nm": thickness, "laue_fringes": q > 0.6,
                }, "XRD (Bruker D8)", when, experiment_id=exp_id, sample_id=sample_id)
            if self.rng.random() < 0.7:
                rms = round(0.15 + 1.6 * (1 - q) + abs(self.rng.gauss(0, 0.05)), 3)
                await self._measure("afm", rms, {
                    "rms_nm": rms, "scan_um": 5, "step_terraces": q > 0.7,
                }, "AFM (Asylum MFP-3D)", when + dt.timedelta(hours=3),
                    experiment_id=exp_id, sample_id=sample_id)
            if layers[0][0].material == "La0.7Sr0.3MnO3":
                rho = round((1.2 + 3.0 * (1 - q)) * 1e-4, 7)
                await self._measure("transport", rho, {
                    "resistivity_ohm_cm": rho, "tc_k": round(330 + 30 * q + self.rng.gauss(0, 3)),
                    "geometry": "van der Pauw",
                }, "PPMS", when + dt.timedelta(days=1), experiment_id=exp_id, sample_id=sample_id)


# --- entry points ---------------------------------------------------------------

async def seed(args) -> None:
    if args.manifest.exists():
        sys.exit(f"{args.manifest} exists: this history is already seeded. "
                 f"Run with --purge first to replace it.")
    for folder in (args.h5_root, args.log_dir, args.staging):
        folder.mkdir(parents=True, exist_ok=True)

    db = GrowthDB(str(args.db))
    await db.connect()
    await db.create_database()
    seeder = Seeder(db, args, random.Random(args.seed))
    try:
        for spec in SESSIONS:
            await seeder.run_session(spec)
        await seeder.ex_situ()
    finally:
        # Written even on failure, so a half-finished seed can still be purged.
        args.manifest.write_text(json.dumps(
            {"version": MANIFEST_VERSION, "db": str(args.db), **seeder.manifest}, indent=1))
        await db.close()
        try:
            args.staging.rmdir()
        except OSError:
            pass

    n = seeder.manifest
    print(f"\nwrote {len(n['chamber_session'])} sessions, {len(n['step'])} steps, "
          f"{len(n['sample'])} samples, {len(n['record'])} recordings, "
          f"{len(n['measurement'])} measurements")
    print(f"manifest: {args.manifest}")


async def purge(args) -> None:
    if not args.manifest.exists():
        sys.exit(f"no manifest at {args.manifest}: nothing seeded to purge")
    manifest = json.loads(args.manifest.read_text())
    db = GrowthDB(manifest.get("db", str(args.db)))
    await db.connect()
    # Children first: every foreign key points up this list.
    order = [("measurement", "measurement_id"), ("step", "step_id"), ("record", "record_id"),
             ("experiment", "experiment_id"), ("sample", "sample_id"),
             ("substrate", "substrate_id"), ("project", "project_id"),
             ("chamber_session", "session_id")]
    for table, pk in order:
        ids = manifest.get(table, [])
        if ids:
            marks = ",".join("?" * len(ids))
            await db.conn.execute(f"DELETE FROM {table} WHERE {pk} IN ({marks})", ids)
        print(f"deleted {len(ids):4d} {table}")
    await db.conn.commit()
    await db.close()
    removed = 0
    for path in manifest.get("files", []):
        try:
            Path(path).unlink()
            removed += 1
        except FileNotFoundError:
            pass
    print(f"removed {removed} files")
    args.manifest.unlink()


def main() -> None:
    sim = PROJECT_ROOT / "run/simulation"
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--purge", action="store_true", help="remove everything a previous run added")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / settings.experiment.growth_db_path)
    parser.add_argument("--h5-root", type=Path, default=sim / "database",
                        help="where the storage node writes HDF5 (start_simulation.sh's --root)")
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / settings.pascal.sim.log_dir)
    parser.add_argument("--manifest", type=Path, default=sim / "seed_history.json")
    parser.add_argument("--frame-interval", type=float, default=3.0,
                        help="seconds between recorded RHEED frames (each is 0.75 MB raw)")
    parser.add_argument("--seed", type=int, default=20260926)
    args = parser.parse_args()
    args.staging = sim / ".seed_staging"
    asyncio.run(purge(args) if args.purge else seed(args))


if __name__ == "__main__":
    main()
