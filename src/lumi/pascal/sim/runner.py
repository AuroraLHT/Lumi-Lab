"""The two threads that make the model behave like a chamber.

`SimLogWriter` appends CSV rows the production `LogReader` tails; `SimMIBackend`
replaces `MIModeBackendSimulator` and actually executes the script it was handed.

Both are driven off the same `ChamberModel`, and only the writer integrates it -- the
MI thread sets setpoints and then waits for the model to get there. That keeps a single
owner of simulated time, so a log row can never disagree with what the MI thread
believes just happened.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import threading
import time
from pathlib import Path

from lumi.pascal.mi_mode import SCRIPT_PREFIX, MIModeServerConfig
from lumi.pascal.sim.columns import LOG_COLUMNS, render_row
from lumi.pascal.sim.model import ChamberModel, ChamberSimConfig
from lumi.pascal.sim.script import Instruction, ScriptError, parse_script

log = logging.getLogger(__name__)

# Ordinary motion should never take this long; if a wait does not clear, the script is
# aborted rather than pinning the MI thread forever.
DEFAULT_COMMAND_TIMEOUT = 3600.0


class ScriptExecutor:
    """Runs parsed instructions against a model.

    Thread-free and injectable so tests can drive a whole growth deterministically:
    pass `advance=model.tick` and simulated time steps forward with no sleeping at all.
    In the node, `advance` is None -- the executor sleeps and `SimLogWriter` is what
    ticks the model.
    """

    def __init__(
        self,
        model: ChamberModel,
        *,
        time_scale: float = 1.0,
        advance=None,
        should_stop=None,
        poll_interval: float = 0.05,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.model = model
        self.time_scale = max(time_scale, 1e-6)
        self._advance_fn = advance
        self._should_stop = should_stop or (lambda: False)
        self.poll_interval = poll_interval
        self.command_timeout = command_timeout

    # --- time ---------------------------------------------------------------

    def advance(self, sim_seconds: float) -> None:
        """Move `sim_seconds` of simulated time forward."""
        if sim_seconds <= 0:
            return
        if self._advance_fn is not None:
            self._advance_fn(sim_seconds)
            return
        deadline = time.monotonic() + sim_seconds / self.time_scale
        while not self._should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))

    def wait_until(self, predicate, timeout: float, what: str) -> None:
        waited = 0.0
        while not predicate():
            if self._should_stop():
                return
            if waited >= timeout:
                raise TimeoutError(f"{what} did not finish within {timeout:.0f}s of simulated time")
            self.advance(self.poll_interval)
            waited += self.poll_interval

    # --- execution ----------------------------------------------------------

    def run(self, instructions: list[Instruction]) -> None:
        for instruction in instructions:
            if self._should_stop():
                return
            self.execute(instruction)

    def execute(self, instruction: Instruction) -> None:  # noqa: C901 - a flat command table
        model, args, kind = self.model, instruction.args, instruction.kind

        if kind == "wait":
            self.advance(float(args["seconds"]))
        elif kind == "wait_for_continue":
            # Nothing can press Continue in a simulation; blocking here would hang
            # every script that used it.
            log.warning("[sim] 'Wait for Continue' has no operator to continue it -- skipped")

        # --- heater. `Temperature Set` holds the execution in RUNNING until the
        # substrate actually arrives, which is what the firmware does: a command without
        # an explicit (Nowait) completes when the controller confirms the physical goal,
        # not when it accepts the request. A 220->700 ramp at 20 degC/min is 24 minutes
        # of simulated time, and the execution stays RUNNING for all of it.
        #
        # This is the behaviour that makes `MiCommandRunner`'s default 30s deadline
        # wrong rather than merely tight -- see `experiment.mi_command_timeout`, which
        # start_simulation.sh sets to 0 (no deadline) for exactly this reason. An
        # earlier version of this simulator returned immediately to fit inside that 30s,
        # which made the simulation agree with the timeout instead of with the machine.
        elif kind == "temperature_control":
            model.set_temperature_control(args["mode"])
        elif kind == "temperature_ramp":
            model.set_temperature_ramp(float(args["rate"]), True)
        elif kind == "temperature_ramp_state":
            model.set_temperature_ramp(None, args["state"].upper() == "ON")
        elif kind == "temperature_set":
            model.set_temperature(float(args["temperature"]))
            if not instruction.nowait:
                self.wait_until(model.at_temperature, self.command_timeout, instruction.text)
        elif kind == "temperature_tolerance":
            model.set_temperature_tolerance(float(args["tolerance"]))
        elif kind == "set_heating_current":
            model.set_heating_current(float(args["current"]))
        elif kind == "set_minimum_current":
            model.set_current_limits(minimum=float(args["current"]))
        elif kind == "set_maximum_current":
            model.set_current_limits(maximum=float(args["current"]))
        elif kind == "heating_laser":
            model.set_heating_laser(args["state"].upper() == "ON")
        elif kind == "heating_laser_lock":
            model.set_heating_lock(args["state"].upper() == "LOCKED")
        elif kind in ("heating_laser_pointer", "heating_laser_threshold"):
            pass  # no logged consequence

        # --- targets and motion. These block until the axis arrives unless the script
        # said (Nowait) -- `model.motor_free` (all axes idle) is what the MI backend
        # waits on to call a move complete, so overlapping motions have to be visible.
        elif kind == "select_target":
            try:
                model.select_target(args["target"])
            except KeyError as exc:
                raise ScriptError(str(exc)) from None
            self._wait_for_motion(instruction)
        elif kind == "target_rotation_mode":
            model.set_target_rotation_mode(args["mode"])
        elif kind == "target_twist_mode":
            model.set_target_twist_mode(args["mode"])
        elif kind == "sample_rotation_mode":
            model.set_sample_rotation_mode(args["mode"])
        elif kind in ("set_mask_position", "move_mask"):
            model.move_mask(int(args["mask_id"]), float(args["distance"]))
            self._wait_for_motion(instruction)
        elif kind == "seek_mask_home":
            model.move_mask(int(args["mask_id"]), 0.0)
            self._wait_for_motion(instruction)
        elif kind == "seek_targets_home":
            model.select_target("Clear")
            self._wait_for_motion(instruction)
        elif kind == "set_rheed_gun_x":
            model.move_rheed_gun_x(float(args["position"]))
            self._wait_for_motion(instruction)
        elif kind == "set_sample_position":
            # The firmware's name for an *absolute* sample angle, 0-360 -- not a height
            # stage. (`TG-Z` is a separate logged column that no command here drives.)
            model.set_sample_angle(float(args["position"]))
            self._wait_for_motion(instruction)
        elif kind == "rotate_sample":
            # Relative, unlike `Set Sample Position`.
            model.rotate_sample_by(float(args["angle"]))
            self._wait_for_motion(instruction)
        elif kind == "mask_speed":
            pass  # the model uses one mask speed; profiles are not simulated

        # --- shutter and laser
        elif kind == "sample_shutter":
            model.set_shutter(args["state"].upper() == "ON")
            # Wait for the blade: `perform_deposition` opens the shutter and fires in
            # consecutive commands, and pulses landing on a half-open shutter is a real
            # failure mode, not one to paper over by pretending it is instant. `(Nowait)`
            # is honoured here like everywhere else -- the script asked not to wait.
            if not instruction.nowait:
                self.wait_until(lambda: model.shutter_idle, self.command_timeout, instruction.text)
        elif kind == "depo_laser_gate":
            model.set_gate(args["state"].upper() == "ON")
            # The gate travels like the sample shutter does.
            if not instruction.nowait:
                self.wait_until(lambda: model.gate_idle, self.command_timeout, instruction.text)
        elif kind == "trigger_laser":
            self._fire(instruction, int(args["num_pulse"]), float(args["frequency"]))
        elif kind == "combi":
            model.move_mask(1, float(args["mask1"]))
            model.move_mask(2, float(args["mask2"]))
            self._wait_for_motion(instruction)
            self._fire(instruction, int(args["num_pulse"]), float(args["frequency"]))

        # --- gas and pressure
        elif kind == "mfc_control":
            model.set_mfc_control(args["state"].lower() == "enable")
        elif kind == "mfc_flow":
            model.set_mfc_flow(int(args["mfc_id"]), float(args["flow"]))
        elif kind == "set_pressure":
            model.set_pressure(float(args["pressure"]))
        elif kind == "pressure_control":
            model.set_pressure_control(args["state"].upper() == "ON")
        elif kind == "select_control_mfc":
            model.select_control_mfc(args["mfc"])
        elif kind == "select_pressure_gauge":
            model.select_pressure_gauge(args["gauge"])
        elif kind == "valve":
            pass  # every mapped valve is held open, as in the recorded asset

        elif kind == "log_interval":
            model.set_log_interval(int(args["seconds"]))
        elif kind in ("beep", "data_logging_on", "data_logging_off", "select_coil_position", "noop"):
            if kind == "noop":
                log.info("[sim] not modelled, ignoring: %s", instruction.text)
        else:
            log.warning("[sim] unhandled instruction kind %r (%s)", kind, instruction.text)

    def _wait_for_motion(self, instruction: Instruction) -> None:
        if instruction.nowait:
            return
        self.wait_until(lambda: self.model.motor_free, self.command_timeout, instruction.text)

    def _fire(self, instruction: Instruction, num_pulse: int, frequency: float) -> None:
        expected = self.model.trigger_laser(num_pulse, frequency)
        if instruction.nowait:
            return
        # Generous but bounded: the pulse train is deterministic, so anything much past
        # its own duration means the model stopped being ticked.
        self.wait_until(lambda: self.model.laser_idle, expected * 2 + 60.0, instruction.text)


class SimLogWriter(threading.Thread):
    """Integrates the model and appends the chamber log.

    Writes into a *directory*, which is what `LogReader` watches -- the real controller
    starts a new file per session and the reader picks it up from a watchdog event. The
    file is flushed after every row: that flush is what generates the inotify event
    `FileModifyHandler` needs, so without it the reader never opens the file at all.
    """

    def __init__(
        self,
        model: ChamberModel,
        log_dir: str | Path,
        *,
        time_scale: float = 1.0,
        log_interval: float = 1.0,
        tick_interval: float = 0.05,
        name: str = "sim_log_writer",
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self.model = model
        self.log_dir = Path(log_dir)
        self.time_scale = max(time_scale, 1e-6)
        self.log_interval = log_interval
        self.tick_interval = tick_interval
        self.rows_written = 0
        self.log_file: Path | None = None
        self._stop_event = threading.Event()

    def _emit(self, fields: list[str]) -> None:
        """Write one whole line, then flush.

        Deliberately not `csv.writer.writerow`, which writes each field separately into
        the text buffer: a reader tailing this file can then read a line that stops
        mid-row, and `csv.DictReader` pads the missing columns with None -- which used
        to kill LogReader's thread on `ast.literal_eval(None)` and freeze the chamber
        log forever. Formatting the row first and issuing it as a single write keeps a
        partial line out of the file in the first place. (The reader is now also robust
        to one, but the two fixes address different halves of the same bug.)
        """
        buffer = io.StringIO()
        csv.writer(buffer).writerow(fields)
        self._handle.write(buffer.getvalue())
        self._handle.flush()

    def open_log(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"chamber_log_{stamp}.csv"
        self._handle = self.log_file.open("w", newline="")
        self._emit(list(LOG_COLUMNS))
        log.info("[sim] writing the chamber log to %s", self.log_file)
        return self.log_file

    def write_row(self, when: datetime.datetime | None = None) -> None:
        self._emit(render_row(self.model.snapshot(), when or datetime.datetime.now()))
        self.rows_written += 1

    def run(self) -> None:
        self.open_log()
        last_tick = time.monotonic()
        next_row = last_tick
        try:
            while not self._stop_event.wait(self.tick_interval):
                now = time.monotonic()
                self.model.tick((now - last_tick) * self.time_scale)
                last_tick = now
                if now >= next_row:
                    self.write_row()
                    # Advance from the schedule, not from `now`, so a slow tick does not
                    # make the log drift permanently late.
                    next_row = max(now, next_row + self.log_interval)
        finally:
            self._handle.close()

    def stop(self) -> None:
        self._stop_event.set()


class SimMIBackend(threading.Thread):
    """Executes MI scripts against the model.

    Same file protocol as `MIModeBackendSimulator`, which this replaces for `--src sim`:
    `MIModeServer` writes `script` plus the assist file, and the backend signals
    progress by renaming the assist file to `<State>_<assist>`, which the server's
    watchdog turns into execution state changes. The difference is that this one reads
    the script, runs it, and only reports Completed when the chamber has actually done
    it -- and reports Aborted when it cannot.
    """

    def __init__(
        self,
        model: ChamberModel,
        mi_config: MIModeServerConfig,
        *,
        time_scale: float = 1.0,
        advance=None,
        poll_interval: float = 0.05,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        name: str = "sim_mi_backend",
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self.model = model
        self.mi_config = mi_config
        self.mi_folder = Path(mi_config.mi_folder)
        self.assist_file = self.mi_folder / mi_config.assist_file_name
        self.time_scale = time_scale
        # None means "somebody else integrates the model" -- in the node that is
        # SimLogWriter, so that simulated time has exactly one owner. Pass
        # `advance=model.tick` to run the backend on its own, as the tests do.
        self.advance = advance
        self.command_timeout = command_timeout
        self.poll_interval = poll_interval
        self.scripts_run = 0
        self._stop_event = threading.Event()

    def _read_script(self) -> str:
        # The assist file holds the path of the script to run, which is how the real
        # backend is told which file to pick up.
        try:
            pointer = self.assist_file.read_text().strip()
        except OSError:
            pointer = ""
        candidate = Path(pointer) if pointer else self.mi_folder / SCRIPT_PREFIX
        if not candidate.exists():
            candidate = self.mi_folder / SCRIPT_PREFIX
        try:
            return candidate.read_text()
        except OSError:
            log.warning("[sim] no script file to execute at %s", candidate)
            return ""

    def _rename(self, current: Path, state: str) -> Path:
        target = self.mi_folder / f"{state}_{self.mi_config.assist_file_name}"
        current.rename(target)
        return target

    def run(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            if not self.assist_file.exists():
                continue
            self.execute_pending()

    def execute_pending(self) -> None:
        source = self._read_script()
        assist = self.assist_file
        # Load -> Loaded happen before anything runs, matching the state sequence the
        # old simulator produced and `MIState` expects.
        assist = self._rename(assist, "Load")
        try:
            instructions = parse_script(source)
        except ScriptError as exc:
            log.error("[sim] rejecting script: %s", exc)
            self._rename(assist, "Aborted")
            return

        assist = self._rename(assist, "Loaded")
        assist = self._rename(assist, "Running")

        executor = ScriptExecutor(
            self.model,
            time_scale=self.time_scale,
            advance=self.advance,
            should_stop=self._stop_event.is_set,
            poll_interval=self.poll_interval,
            command_timeout=self.command_timeout,
        )
        try:
            executor.run(instructions)
        except (ScriptError, TimeoutError) as exc:
            log.error("[sim] aborting script: %s", exc)
            self._rename(assist, "Aborted")
            return
        except Exception:
            log.exception("[sim] script raised")
            self._rename(assist, "Aborted")
            return

        self.scripts_run += 1
        self._rename(assist, "Completed")

    def stop(self) -> None:
        self._stop_event.set()


def build_chamber_sim(
    log_dir: str | Path,
    mi_config: MIModeServerConfig,
    *,
    sim_config: ChamberSimConfig | None = None,
    time_scale: float = 1.0,
    log_interval: float = 1.0,
    tick_interval: float = 0.05,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[ChamberModel, SimLogWriter, SimMIBackend]:
    """One model, the writer that integrates it, and the MI backend that drives it."""
    model = ChamberModel(sim_config)
    writer = SimLogWriter(
        model, log_dir, time_scale=time_scale, log_interval=log_interval, tick_interval=tick_interval
    )
    backend = SimMIBackend(model, mi_config, time_scale=time_scale, command_timeout=command_timeout)
    return model, writer, backend
