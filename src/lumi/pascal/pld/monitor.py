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
import threading
import time
from pathlib import Path

import grpc

from lumi.pascal.pld.client import PldClient
from lumi.pascal.pld.columns import values_from_state  # noqa: F401  (re-exported)
from lumi.pascal.sim.columns import LOG_COLUMNS, render_row

log = logging.getLogger(__name__)

_RECONNECT_S = 2.0

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
