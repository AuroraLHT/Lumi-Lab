"""Watching pld-service: the latest state, its events, and the growth-log CSV.

pld-service writes only its own JSONL service log. Everything in Lumi that reads the
chamber -- the experiment node's readings and interlocks, storage's HDF5 log table, the
monitor's charts -- reads PLD Controller's growth-log CSV through `LogReader`. So with
the new driver this node writes that CSV itself, from the service's state, in PLD
Controller's exact columns and number formats (`lumi.pascal.sim.columns`), and the
production `LogReader` tails it unchanged, as it tails the simulator's.

The column mapping is the REA side's (`src/rea/lumi_adapter/growth_log.py`), which is
what the legacy adapter writes. Its caveats, from the REA session (2026-11-05):

- Columns the chamber has no reading for are 0: FocusLns, MirrorPs, ATN, BTFvalve,
  PwrMeter, Laser moni/set, MFC2-5, TG Spin Speed, TG-Z.
- Status words carry only the bits the service knows; every other bit is 0, and so are
  DepoLaserStat, Pump Stat, Valve Stat2, Other Stat and the A*/W* words. (So
  `get_pump_status` reads every pump as off under this driver.)
- `Back Pres Main` is TCG[0]; the TCG order in the gauge reply is not confirmed yet.
- `Mask2` carries the RHEED gun X position.
- NaN readings (pyrometer below ~140 C, IG1 warming up) are written as 0, as PLD
  Controller shows them.
- `LaserHz` is not a service reading: it is the rate of the train this node started,
  while that train is firing, and 0 otherwise (the recorded PLD Controller logs idle it
  at 0 too).
"""

from __future__ import annotations

import collections
import csv
import datetime
import io
import logging
import math
import threading
import time
from pathlib import Path

import grpc

from lumi.pascal.pld.client import PldClient
from lumi.pascal.sim.columns import LOG_COLUMNS, render_row

log = logging.getLogger(__name__)

#: PLDconfig.ini TG1..TG7.
TARGET_NUMBERS = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "Clear": 7}
#: `Valve Stat1` bits, as `lumi.pascal.chamber_log.PARSE_DICT` decodes them. Bits 3-4
#: (MV2/FV2, RHEED) are left 0: the REA mapping does not name them.
VALVE_STAT1 = {"MV10": 0, "MV11": 1, "FV1": 2, "MV3": 5, "FV3": 6, "RV3": 7}

_RECONNECT_S = 2.0


def _num(value) -> float:
    value = float(value)
    return value if math.isfinite(value) else 0.0


def _word(flags: dict[int, bool]) -> int:
    return sum(1 << bit for bit, on in flags.items() if on)


def values_from_state(s, laser_rate: float = 0.0) -> dict:
    """A `pld.v1.ChamberState` as one log row's values, keyed by LOG_COLUMNS."""
    h, p, m, laser, g = s.heater, s.pressures, s.motion, s.laser, s.gas
    temperature = _num(h.temperature)
    temp_set = _num(h.temperature_setpoint) if h.pid_on else 0.0
    values = {c: 0 for c in LOG_COLUMNS[1:]}
    values.update({
        "TG No.": TARGET_NUMBERS.get(m.target, 0),
        "TG Rotate": _num(m.target_angle),
        "Mask1": _num(m.mask_position),
        "Mask2": _num(m.rheed_gun_x),
        "Substrate": _num(m.sample_angle),
        "LaserHz": laser_rate if laser.status == "firing" else 0.0,
        "LaserPuls": laser.pulses_fired,
        "MissedPuls": laser.missed_shots,
        "HT set": _num(h.current_set),
        "HT moni": _num(h.current_monitor),
        "Delta HT curr": _num(h.current_set) - _num(h.current_monitor),
        "HT Temp set": temp_set,
        "HT Temp moni": temperature,
        "Delta Temp": (temp_set - temperature) if h.pid_on else 0.0,
        "MFC1 set": _num(g.mfc1_setpoint),
        "MFC1 moni": _num(g.mfc1_flow),
        "Delta MFC1": _num(g.mfc1_setpoint) - _num(g.mfc1_flow),
        "Prc Pres Main": _num(p.cdg10),
        "Prc Pres Main2": _num(p.cdg11),
        "Vac Pres Main": _num(p.ig1),
        "Back Pres Main": _num(p.tcg[0]) if len(p.tcg) else 0.0,
        "Vac Pres L/L": _num(p.ig3),
        "Back Pres L/L": _num(p.tcg31),
        "Heat Stat": _word({0: h.power_on, 1: h.pid_on, 2: h.ramp_c_per_min > 0, 3: h.started}),
        "Valve Stat1": _word({bit: s.valves.get(name, False) for name, bit in VALVE_STAT1.items()}),
        "Shut Stat": _word({0: s.sample_shutter_away}),
        "Motor Stat": _word({0: m.motor_free, 1: m.target_spinning}),
    })
    return values


class GrowthLog:
    """One CSV per `start()` in the log folder, flushed after every whole row.

    One write per row, like `SimLogWriter._emit`: a reader tailing the file must never
    see half a row, and the flush is what wakes `LogReader`'s watchdog.
    """

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)
        self.path: Path | None = None
        self._file = None
        self._lock = threading.Lock()

    def start(self, name: str | None = None) -> Path:
        with self._lock:
            self._close()
            self.folder.mkdir(parents=True, exist_ok=True)
            stem = Path(name).name if name else f"chamber_log_{datetime.datetime.now():%Y%m%d_%H%M%S}"
            self.path = self.folder / (stem if stem.endswith(".csv") else stem + ".csv")
            self._file = self.path.open("a", newline="")
            if self._file.tell() == 0:
                self._write(list(LOG_COLUMNS))
            log.info("writing the growth log to %s", self.path)
            return self.path

    def stop(self) -> None:
        with self._lock:
            self._close()

    @property
    def active(self) -> bool:
        return self._file is not None

    def write(self, state, when: datetime.datetime | None = None, laser_rate: float = 0.0) -> None:
        with self._lock:
            if self._file is None:
                return
            if when is None:
                when = datetime.datetime.fromtimestamp(state.time) if state.time else datetime.datetime.now()
            self._write(render_row(values_from_state(state, laser_rate), when))

    def _write(self, fields: list[str]) -> None:
        buffer = io.StringIO()
        csv.writer(buffer).writerow(fields)
        self._file.write(buffer.getvalue())
        self._file.flush()

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class PldStateMonitor(threading.Thread):
    """Follows WatchState: keeps the latest snapshot, feeds the bus stream, and writes a
    growth-log row every `interval_s`. Reconnects when the service goes away, and says
    so -- `connected` is False until it is back."""

    def __init__(self, client: PldClient, growth_log: GrowthLog, *, interval_s: float = 1.0,
                 rate_hz: float = 2.0, name: str = "pld_state_monitor") -> None:
        super().__init__(name=name, daemon=True)
        self.client = client
        self.growth_log = growth_log
        self.interval_s = float(interval_s)
        self.rate_hz = rate_hz
        #: The rate of the train this node last started; see LaserHz in the module doc.
        self.laser_rate = 0.0
        self.latest = None
        self.latest_at = 0.0
        self.last_error: str | None = None
        self._new = threading.Event()
        self._stop_event = threading.Event()
        self._stream = None
        self._next_row = 0.0

    @property
    def connected(self) -> bool:
        return self.latest is not None and bool(self.latest.connected) and time.monotonic() - self.latest_at < 10.0

    def take_new(self):
        """The latest snapshot if one arrived since the last call, else None."""
        if not self._new.is_set():
            return None
        self._new.clear()
        return self.latest

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._stream = self.client.watch(self.rate_hz)
                for state in self._stream:
                    self._on_state(state)
                    if self._stop_event.is_set():
                        break
            except grpc.RpcError as e:
                if self._stop_event.is_set():
                    break
                message = f"{e.code().name}: {e.details()}" if hasattr(e, "code") else str(e)
                if message != self.last_error:
                    log.warning("pld-service state stream at %s lost (%s); retrying", self.client.address, message)
                self.last_error = message
            except Exception as e:  # noqa: BLE001 - a bad snapshot must not kill the thread
                log.exception("pld state monitor failed")
                self.last_error = str(e)
            self._stop_event.wait(_RECONNECT_S)

    def _on_state(self, state) -> None:
        if self.last_error is not None:
            log.info("pld-service state stream at %s is back", self.client.address)
            self.last_error = None
        self.latest = state
        self.latest_at = time.monotonic()
        self._new.set()
        now = time.monotonic()
        if now >= self._next_row:
            try:
                self.growth_log.write(state, laser_rate=self.laser_rate)
            except Exception:
                log.exception("could not write the growth log row")
            self._next_row = max(now, self._next_row + self.interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            self._stream.cancel()
        self.growth_log.stop()


class PldEventMonitor(threading.Thread):
    """Follows WatchEvents into a bounded history, and the node log."""

    def __init__(self, client: PldClient, maxlen: int = 1000, name: str = "pld_event_monitor") -> None:
        super().__init__(name=name, daemon=True)
        self.client = client
        self.events: collections.deque = collections.deque(maxlen=maxlen)
        self._stop_event = threading.Event()
        self._stream = None

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._stream = self.client.events()
                for event in self._stream:
                    self.events.append(event)
                    level = logging.WARNING if event.kind in ("interlock", "error") else logging.INFO
                    log.log(level, "pld-service %s: %s", event.kind, event.message)
            except grpc.RpcError:
                if self._stop_event.is_set():
                    break
            except Exception:  # noqa: BLE001
                log.exception("pld event monitor failed")
            self._stop_event.wait(_RECONNECT_S)

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            self._stream.cancel()
