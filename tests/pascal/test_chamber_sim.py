"""T0 tests for the state-model chamber simulator (`lumi.pascal.sim`).

No broker, no hardware. The integration test at the bottom does use real threads and
real files, because the whole point of `--src sim` is that the production `LogReader`
tails a real CSV -- stubbing that out would test the half that was never in doubt.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import time
from pathlib import Path

import pytest

import lumi.pascal.command as pcmd
from lumi.pascal.chamber_log import PARSE_DICT, process_row
from lumi.pascal.log_reader import LogReader, LogReaderConfig
from lumi.pascal.mi_mode import MIModeServerConfig
from lumi.pascal.sim.columns import (
    LOG_COLUMNS,
    format_scientific,
    format_time,
    render_row,
)
from lumi.pascal.sim.model import ChamberModel, ChamberSimConfig
from lumi.pascal.sim.runner import ScriptExecutor, SimLogWriter, SimMIBackend
from lumi.pascal.sim.script import ScriptError, parse_line, parse_script
from lumi.path import PROJECT_ROOT

ASSET = PROJECT_ROOT / "src" / "lumi" / "pascal" / "assets" / "chamber_log_test.csv"


@pytest.fixture
def model() -> ChamberModel:
    # No pyrometer noise: these assert on exact setpoints, and the noise is a display
    # nicety rather than anything the control code depends on.
    return ChamberModel(ChamberSimConfig(temperature_noise=0.0))


def run(model: ChamberModel, script: str) -> ScriptExecutor:
    """Execute a script with simulated time driven directly, so no test sleeps."""
    executor = ScriptExecutor(model, advance=model.tick, poll_interval=0.05)
    executor.run(parse_script(script))
    return executor


def text(*commands) -> str:
    return "".join(c.to_text() for c in commands)


# --- the log has to look exactly like PASCAL's ------------------------------


def test_columns_match_the_recorded_asset():
    # Storage sizes its HDF5 log table from the column list the chamber advertises, so
    # a column added here without one in the recording is a silently mis-shaped record.
    with ASSET.open() as handle:
        header = next(csv.reader(handle))
    assert list(LOG_COLUMNS) == header


def test_a_rendered_row_parses_the_way_a_recorded_one_does(model):
    row = dict(zip(LOG_COLUMNS, render_row(model.snapshot(), datetime.datetime(2026, 8, 6, 14, 51, 59))))
    parsed = process_row(dict(row))

    # process_row expands each status word into named booleans; every one of them has
    # to be there or ExperimentManager's `values["Motor free"]` lookups raise KeyError.
    for field_map in PARSE_DICT.values():
        for name in field_map.values():
            assert name in parsed, f"{name} missing from the parsed row"
    assert isinstance(parsed["time"], float)
    assert parsed["time_stamp"]


def test_the_time_column_is_the_controllers_12_hour_form():
    assert format_time(datetime.datetime(2026, 8, 6, 14, 51, 59)) == "2:51:59 PM"
    assert format_time(datetime.datetime(2026, 8, 6, 0, 3, 4)) == "12:03:04 AM"
    # Round-trips through the parser process_row uses.
    datetime.datetime.strptime(format_time(datetime.datetime.now()), "%I:%M:%S %p")


def test_pressures_use_the_controllers_exponent_form():
    # `ExperimentManager.get_current_pressure` compares this column as a string, so the
    # exact text matters: "0.0" or "0.00E+00" would take the wrong branch.
    assert format_scientific(0.0) == "0.00E+0"
    assert format_scientific(1.11e-3) == "1.11E-3"
    assert format_scientific(1.04, 3) == "1.040E+0"
    assert format_scientific(6.7e-9) == "6.70E-9"


# --- the interlock the recorded log can never satisfy -----------------------


def test_motor_free_is_set_at_rest_and_cleared_while_moving(model):
    # The recorded asset holds Motor Stat at 0x0000 for all 5944 rows, so bit 0
    # ("Motor free") is never set and ExperimentManager.move_mask_to_position() raises
    # unconditionally when replaying it. That is the behaviour this mode exists to fix.
    assert model.motor_free
    assert process_row(dict(zip(LOG_COLUMNS, render_row(model.snapshot(), datetime.datetime.now()))))["Motor free"]

    model.move_mask(1, 75.0)
    assert not model.motor_free
    model.tick(100.0)
    assert model.motor_free
    assert model.mask1.position == pytest.approx(75.0)


# --- the script decoder mirrors the encoder ---------------------------------


def test_every_command_the_experiment_manager_emits_is_understood():
    # If any of these ever parses as "noop" the simulator silently stops modelling a
    # step of a real growth, which is worse than failing.
    commands = [
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL),
        pcmd.TemperatureRamp(20.0, state=pcmd.PascalState("ON")),
        pcmd.TemperatureRamp(20.0, state=pcmd.PascalState("OFF")),
        pcmd.TemperatureSet(750.0, nowait=False),
        pcmd.SelectTarget("C", nowait=False),
        pcmd.SelectTarget(pcmd.Targets.Clear, nowait=False),
        pcmd.TargetRotationMode(mode="AUTO"),
        pcmd.TargetTwistMode(mode="AUTO"),
        pcmd.SampleShutter(pcmd.PascalState("ON")),
        pcmd.TriggerLaser(num_pulse=3000, frequency=10.0, sync=False, nowait=False),
        pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=75.0, sync=False, nowait=False),
        pcmd.SetRHEEDGunX(1.5),
        pcmd.SetHeatingCurrent(current=8.5),
        pcmd.HeatingLaserLock(locked=False, nowait=False),
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.HeatingLaserThreshold(state=pcmd.PascalState("ON")),
        pcmd.SetMFC1Flow(5.0),
        pcmd.SetMFC2Flow(5.0),
        pcmd.SetPressure(1.0e-2),
        pcmd.PressureControl(state=pcmd.PascalState("ON")),
        pcmd.Wait(30),
    ]
    for command in commands:
        instruction = parse_line(command.to_text())
        assert instruction is not None and instruction.kind != "noop", command.to_text()


def test_the_sync_and_nowait_suffixes_are_stripped():
    instruction = parse_line(
        pcmd.TriggerLaser(num_pulse=10, frequency=5.0, sync=True, nowait=True).to_text()
    )
    assert instruction.kind == "trigger_laser"
    assert instruction.sync and instruction.nowait
    assert instruction.args == {"num_pulse": 10, "frequency": 5.0}


def test_comments_and_blank_lines_are_dropped():
    assert parse_line(pcmd.Comment("annealing now").to_text()) is None
    assert parse_line("   ") is None


def test_loops_are_unrolled():
    script = "Loop 3 (0)\nSample Shutter ON\nSample Shutter OFF\nLoop End\n"
    assert [i.kind for i in parse_script(script)] == ["sample_shutter"] * 6


def test_a_runaway_loop_is_rejected_at_parse_time():
    with pytest.raises(ScriptError, match="more than"):
        parse_script("Loop 10000000 (0)\nBeep\nLoop End\n")


def test_an_unclosed_loop_is_rejected():
    with pytest.raises(ScriptError, match="no 'Loop End'"):
        parse_script("Loop 2 (0)\nBeep\n")


def test_an_unknown_line_is_kept_as_a_noop_rather_than_failing():
    # The real controller understands more than lumi.pascal.command encodes; aborting a
    # growth over a line the simulator has not learned would be the worse lie.
    instruction = parse_line("Tilt Stage 3.00")
    assert instruction.kind == "noop" and instruction.text == "Tilt Stage 3.00"


# --- physics that the control code actually reads back ----------------------


def test_trigger_laser_delivers_exactly_the_requested_pulses(model):
    run(model, text(pcmd.TriggerLaser(num_pulse=600, frequency=10.0, sync=False, nowait=False)))
    assert model.laser_pulses == 600
    assert model.laser_idle
    # 600 pulses at 10 Hz is a minute of chamber time, not an instant.
    assert model.sim_time == pytest.approx(60.0, abs=1.0)
    # The gate closes when the train finishes.
    assert model.snapshot()["DepoLaserStat"] == 0x0001


def test_the_gate_bit_is_open_only_while_pulses_are_being_delivered(model):
    model.trigger_laser(100, 10.0)
    model.tick(1.0)
    assert model.snapshot()["DepoLaserStat"] & 0x0004
    model.tick(20.0)
    assert not model.snapshot()["DepoLaserStat"] & 0x0004


def test_missed_pulses_can_be_injected(model):
    # perform_preablation fails the growth if MissedPuls moves between two log reads.
    faulty = ChamberModel(ChamberSimConfig(temperature_noise=0.0, missed_pulse_rate=0.5))
    faulty.trigger_laser(200, 100.0)
    faulty.tick(5.0)
    assert faulty.missed_pulses > 0
    model.trigger_laser(200, 100.0)
    model.tick(5.0)
    assert model.missed_pulses == 0


def test_temperature_ramps_at_the_commanded_rate(model):
    run(model, text(
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        pcmd.TemperatureRamp(60.0, state=pcmd.PascalState("ON")),  # 1 degC/s
        # (Nowait), so the script keeps running while the ramp is still climbing --
        # without it this command would not return until the substrate reached 400.
        pcmd.TemperatureSet(400.0, nowait=True),
        pcmd.Wait(60),
    ))
    # 60s at 60 degC/min from the 160 floor: the setpoint is 220, not the target.
    assert model.temperature_setpoint == pytest.approx(220.0, abs=1.0)
    assert model.temperature_target == 400.0
    # And the substrate is following it, lagging behind as a real one does.
    assert 160.0 < model.logged_temperature < model.temperature_setpoint


def test_temperature_set_holds_the_execution_until_the_substrate_arrives(model):
    """The firmware's actual contract: a command without an explicit (Nowait) stays
    RUNNING until the controller confirms the physical goal, so `Temperature Set 400`
    completes when the substrate is at 400 -- not when the setpoint is accepted.

    This is what makes a bounded MI deadline wrong rather than merely tight: the wait is
    as long as the ramp. It also used to be the other way round in this simulator, which
    made the simulation agree with `MiCommandRunner`'s 30s default instead of with the
    machine.
    """
    run(model, text(
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        pcmd.TemperatureRamp(600.0, state=pcmd.PascalState("ON")),  # 10 degC/s
        pcmd.TemperatureSet(400.0, nowait=False),
    ))
    assert model.at_temperature(), "the command returned before the substrate arrived"
    assert model.logged_temperature == pytest.approx(400.0, abs=model.temperature_tolerance)
    # 220 -> 400 at 10 degC/s is ~18s of ramp, plus the substrate's lag behind it.
    assert model.sim_time > 18.0


def test_the_nowait_suffix_is_what_returns_before_the_ramp_finishes(model):
    """The other half of the same contract -- and the only way a script can carry on
    while the chamber is still moving."""
    run(model, text(
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        pcmd.TemperatureRamp(20.0, state=pcmd.PascalState("ON")),
        pcmd.TemperatureSet(750.0, nowait=True),
    ))
    assert model.sim_time < 1.0
    assert model.temperature_target == 750.0
    assert not model.at_temperature()


def test_the_heater_follows_the_measured_calibration(model):
    # 6.5 A minimum on the diode; linear 7 A -> 220 degC, 30 A -> 1000 degC.
    assert model.temperature_for_current(7.0) == pytest.approx(220.0)
    assert model.temperature_for_current(18.5) == pytest.approx(610.0, abs=0.5)
    assert model.temperature_for_current(30.0) == pytest.approx(1000.0)
    assert model.current_for_temperature(220.0) == pytest.approx(7.0)
    assert model.current_for_temperature(1000.0) == pytest.approx(30.0)

    # Under the diode's lasing minimum nothing is delivered, so the pyrometer reads its
    # floor rather than a temperature off the line.
    assert model.temperature_for_current(6.0) == pytest.approx(160.0)
    # The coldest the diode can actually hold, at ld_min itself.
    assert model.temperature_for_current(6.5) == pytest.approx(203.0, abs=0.5)


def test_asking_for_less_than_the_diode_can_hold_turns_it_off(model):
    """The anchor is 220 degC, so a lower setpoint is below the calibrated range.

    It must map to a current under ld_min -- switching the diode off and letting the
    substrate coast to the pyrometer floor -- rather than clamping at ld_min, which
    would hold ~203 degC while `HT Temp set` read 160 and leave every "have we arrived?"
    poll waiting on a setpoint the log would never reach.
    """
    wanted = model.current_for_temperature(160.0)
    assert wanted < model.config.ld_min
    assert model.temperature_for_current(wanted) == pytest.approx(160.0)  # round-trips


def test_manual_current_warms_the_substrate_past_the_pid_engage_threshold(model):
    # _warm_up_to_pid_limit refuses to continue unless the pyrometer passes 220 degC.
    # On the measured calibration that needs about 8.65 A.
    run(model, text(
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL),
        pcmd.SetHeatingCurrent(current=9.0),
        pcmd.Wait(300),
    ))
    assert model.logged_temperature > 220.0


def test_the_configured_warm_up_target_clears_the_pid_engage_threshold(model):
    """The cold path a growth actually takes, pinned at both ends.

    `_warm_up_to_pid_limit` ramps the heating current to PLDconfig's
    `[PIDsettings] LDmin = 8.5`, then raises "temperature stalled" unless the pyrometer
    has passed `experiment.bounds.temperature_pid_engage_threshold = 220`. On the
    measured calibration 8.5 A is ~271 degC, comfortably clear.

    This once asserted the opposite: with the earlier 7 A -> 160 degC anchor the ramp
    reached only ~215 degC and every to_temperature() from a cold chamber raised. If a
    re-measure moves the anchor back down, this fails rather than the failure resurfacing
    four minutes into a growth.
    """
    assert model.temperature_for_current(8.5) == pytest.approx(270.9, abs=0.5)
    assert model.temperature_for_current(8.5) > 220.0
    # And the margin, so a small re-measure does not silently land on the boundary.
    assert model.current_for_temperature(220.0) == pytest.approx(7.0, abs=0.05)


def test_a_cold_chamber_reads_the_pyrometer_floor(model):
    # Why the recorded asset is a flat 160: PLDconfig's Pyro_min, not a stuck sensor.
    model.tick(600.0)
    assert model.logged_temperature == pytest.approx(160.0)


def test_the_gas_line_follows_the_measured_calibration(model):
    # Linear 0.5 sccm -> 1 mTorr, 30 sccm -> 200 mTorr, in Torr.
    assert model.pressure_for_flow(0.5) == pytest.approx(1.0e-3)
    assert model.pressure_for_flow(30.0) == pytest.approx(200.0e-3)
    assert model.pressure_for_flow(15.25) == pytest.approx(100.5e-3, rel=1e-3)
    # Under the calibrated range it decays to base pressure rather than going negative.
    assert 0 < model.pressure_for_flow(0.1) < 1.0e-3
    assert model.pressure_for_flow(0.0) == pytest.approx(model.config.base_pressure)
    # And the inverse the pressure controller uses agrees with it.
    assert model.flow_for_pressure(model.pressure_for_flow(12.0)) == pytest.approx(12.0)


def test_pressure_control_settles_at_the_requested_pressure(model):
    run(model, text(
        pcmd.PressureControl(state=pcmd.PascalState("ON")),
        pcmd.SetPressure(20.0e-3),
        pcmd.Wait(120),
    ))
    values = model.snapshot()
    assert values["Vac Pres Main"] == pytest.approx(20.0e-3, rel=0.02)
    # 20 mTorr is a shade under 3.3 sccm on the calibration.
    assert values["MFC1 moni"] == pytest.approx(3.32, abs=0.05)


def test_pressure_follows_the_gas_flow(model):
    base = model.snapshot()["Vac Pres Main"]
    # 300s: the chamber's time constant is ~26s (constant pump speed), so a settled
    # reading needs several of them.
    run(model, text(pcmd.SetMFC1Flow(5.0), pcmd.SetMFCControl(enable=True), pcmd.Wait(300)))
    values = model.snapshot()
    assert values["Vac Pres Main"] > base * 1000
    # 5 sccm on the calibration: 1 mTorr + 4.5 * (199/29.5) mTorr.
    assert values["Vac Pres Main"] == pytest.approx(31.4e-3, rel=0.02)
    assert values["MFC1 moni"] == pytest.approx(5.0, abs=0.1)
    # The process gauge never reads below its own zero offset, which is why the
    # recorded asset shows 1.11E-3 next to an ion gauge at 6.70E-9.
    assert values["Prc Pres Main"] > values["Vac Pres Main"]


def test_selecting_a_target_moves_the_carousel_to_its_configured_angle(model):
    run(model, text(pcmd.SelectTarget("C", nowait=False)))
    assert model.target_number == 3
    # PLDconfig_07102025.ini [TGsettings] TG3angle.
    assert model.rotation.position == pytest.approx(154.5)
    assert model.motor_free
    # Rotating a third of the way round is not instantaneous.
    assert model.sim_time > 1.0


def test_selecting_a_target_that_does_not_exist_fails_the_script(model):
    with pytest.raises(ScriptError):
        run(model, "Select Target Z\n")


def test_the_carousel_takes_the_short_way_round(model):
    # F is at 308.5 and Clear at 0.0: 51.5 degrees apart across the wrap, not 308.5.
    run(model, text(pcmd.SelectTarget("F", nowait=False)))
    start = model.sim_time
    run(model, text(pcmd.SelectTarget(pcmd.Targets.Clear, nowait=False)))
    travelled = (model.sim_time - start) * model.config.target_rotation_speed
    assert travelled == pytest.approx(51.5, abs=2.0)


def test_the_target_number_changes_on_arrival_not_on_command(model):
    run(model, text(pcmd.SelectTarget("A", nowait=False)))
    model.select_target("D")
    # Mid-rotation the log still reports the target actually under the plume.
    assert model.target_number == 1
    model.tick(0.5)
    assert model.target_number == 1 and not model.motor_free
    model.tick(60.0)
    assert model.target_number == 4 and model.motor_free


def test_the_rep_rate_column_is_zero_while_the_laser_is_idle(model):
    # The recorded asset idles LaserHz at 0 rather than holding the last rate.
    assert model.snapshot()["LaserHz"] == 0
    model.trigger_laser(100, 10.0)
    model.tick(1.0)
    assert model.snapshot()["LaserHz"] == 10.0
    model.tick(30.0)
    assert model.snapshot()["LaserHz"] == 0


def test_a_mask_past_its_travel_stop_parks_and_raises_the_limit_alarm(model):
    # PLDconfig [MotorSettings] Mask1setmax = 160. ExperimentManager validates this
    # client-side, but a raw MI script does not go through the manager.
    run(model, "Set Mask Position M1=500.00\n")
    assert model.mask1.position == pytest.approx(160.0)
    assert model.snapshot()["A Motor1"] & (1 << 10)  # "Limit sensor in Mask 1"
    parsed = process_row(dict(zip(LOG_COLUMNS, render_row(model.snapshot(), datetime.datetime.now()))))
    assert parsed["Limit sensor in Mask 1"] is True

    # It latches until the axis is commanded somewhere legal again.
    run(model, "Seek Mask Home M1\n")
    assert not model.snapshot()["A Motor1"] & (1 << 10)


def test_the_shutter_takes_time_to_travel_and_the_command_waits_for_it(model):
    model.set_shutter(True)
    assert not model.shutter_open and not model.shutter_idle
    model.tick(model.config.shutter_travel_time + 0.01)
    assert model.shutter_open and model.shutter_idle

    # And a script does not move on until the blade has finished.
    fresh = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    run(fresh, text(pcmd.SampleShutter(pcmd.PascalState("ON"))))
    assert fresh.shutter_open
    assert fresh.sim_time >= fresh.config.shutter_travel_time


# --- the whole loop, with real threads and real files -----------------------


def test_the_production_log_reader_tails_what_the_simulator_writes(tmp_path: Path):
    """The `--src sim` wiring end to end: SimLogWriter -> CSV -> watchdog -> LogReader.

    This is the path `--src test` never exercises, and it is the same one `--src path`
    uses against the real controller.
    """
    model = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    log_dir = tmp_path / "chamber_log"
    log_dir.mkdir()

    writer = SimLogWriter(model, log_dir, time_scale=50.0, log_interval=0.1, tick_interval=0.02)
    reader = LogReader(
        config=LogReaderConfig(queue_size=10, idle_time=0.01, log_path=str(log_dir)),
        name="test_sim_log_reader",
        daemon=True,
    )
    model.set_shutter(True)
    model.trigger_laser(10_000, 100.0)

    writer.start()
    reader.start()
    try:
        deadline = time.monotonic() + 20.0
        row = None
        while time.monotonic() < deadline:
            row, _ = reader.get_log()
            if row is not None and float(row["Laser moni"]) > 0:
                break
            time.sleep(0.05)
        assert row is not None, "the log reader never picked up the simulator's file"
        assert float(row["Laser moni"]) > 0, "the tailed row does not show the laser firing"
        assert row["Sample Shutter"] is True
        assert row["Motor free"] is True
        assert writer.log_file is not None and writer.log_file.exists()
    finally:
        reader.stop()
        writer.stop()
        writer.join(timeout=5)
        reader.join(timeout=5)


def test_the_mi_backend_runs_the_script_it_was_handed(tmp_path: Path):
    """`MIModeBackendSimulator` renames the assist file without ever reading the
    script. This one executes it, so Completed means the chamber actually did it."""
    model = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    mi_folder = tmp_path / "mi_mode"
    mi_folder.mkdir()
    config = MIModeServerConfig(mi_folder=str(mi_folder), assist_file_name="assist.txt")

    (mi_folder / "script").write_text(text(
        pcmd.SelectTarget("B", nowait=False),
        pcmd.SampleShutter(pcmd.PascalState("ON")),
        pcmd.TriggerLaser(num_pulse=50, frequency=50.0, sync=False, nowait=False),
    ))
    (mi_folder / "assist.txt").write_text(str(mi_folder / "script") + "\r\n")

    # `advance=model.tick` because SimLogWriter -- which owns simulated time in the
    # node -- is not running here.
    backend = SimMIBackend(model, config, advance=model.tick, poll_interval=0.05)
    backend.execute_pending()

    assert (mi_folder / "Completed_assist.txt").exists()
    assert model.target_number == 2
    assert model.shutter_open
    assert model.laser_pulses == 50


def test_a_script_the_chamber_cannot_run_is_reported_aborted(tmp_path: Path):
    model = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    mi_folder = tmp_path / "mi_mode"
    mi_folder.mkdir()
    config = MIModeServerConfig(mi_folder=str(mi_folder), assist_file_name="assist.txt")

    (mi_folder / "script").write_text("Loop 2 (0)\nBeep\n")  # no Loop End
    (mi_folder / "assist.txt").write_text(str(mi_folder / "script") + "\r\n")

    SimMIBackend(model, config, advance=model.tick, poll_interval=0.05).execute_pending()
    assert (mi_folder / "Aborted_assist.txt").exists()
    assert not (mi_folder / "Completed_assist.txt").exists()


# --- the log reader must survive a torn row -------------------------------------
#
# Regression tests for the failure that froze a running chamber for 1h46m: the reader
# caught a partially written line, DictReader padded the missing columns with None,
# process_row's ast.literal_eval(None) raised, and the exception killed the thread.
# get_log() then served the same row forever -- `Motor free` included, so
# is_motor_free() answered "busy" until the node was restarted -- while the
# capability's state still reported is_running with no error.


def test_a_partial_row_does_not_kill_the_reader(tmp_path: Path):
    log_dir = tmp_path / "chamber_log"
    log_dir.mkdir()
    log_file = log_dir / "chamber_log_torn.csv"

    model = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    good = ",".join(render_row(model.snapshot(), datetime.datetime(2026, 8, 12, 12, 0, 0)))
    # A line that stops mid-row, exactly as a reader sees a flush in progress.
    torn = ",".join(render_row(model.snapshot(), datetime.datetime(2026, 8, 12, 12, 0, 1))[:8])

    reader = LogReader(
        config=LogReaderConfig(queue_size=10, idle_time=0.01, log_path=str(log_dir)),
        name="test_torn_row_reader", daemon=True,
    )
    def append(line: str) -> None:
        with log_file.open("a", newline="") as handle:
            handle.write(line + "\n")

    def wait_for(predicate, what: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    reader.start()
    try:
        # The file is created *after* the reader starts: it opens a file when the
        # directory watcher reports one, and `open_reader(jump_to_end=True)` seeks past
        # whatever is already there -- so every row this test cares about is appended
        # once the reader is tailing.
        log_file.write_text(",".join(LOG_COLUMNS) + "\n")
        # Keep appending until one lands: the watcher opens the file on the first
        # modify event and seeks to the end, so a row written in the gap between the
        # write and the open is skipped, and which side of that gap the first row falls
        # on is a race.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and reader.get_log()[0] is None:
            append(good)
            time.sleep(0.1)
        assert reader.get_log()[0] is not None, "the reader never picked up the file"

        append(torn)
        wait_for(lambda: reader._bad_rows > 0, "the torn row to be skipped")

        assert reader.is_alive(), "a torn row killed the reader thread"
        assert reader._bad_rows >= 1, "the torn row should have been counted as skipped"

        # And the good row before it is still being served.
        row, _ = reader.get_log()
        assert row is not None
        assert row["Motor free"] is True

        # The reader keeps up after the bad row: append a fresh one and see it arrive.
        # This is the assertion that would have caught the original bug -- the thread
        # was dead, so nothing after the torn row ever appeared.
        model.set_shutter(True)
        model.tick(2.0)  # past the shutter's pneumatic travel, or the bit is still 0
        append(",".join(render_row(model.snapshot(), datetime.datetime(2026, 8, 12, 12, 0, 2))))
        wait_for(lambda: reader.get_log()[0]["Sample Shutter"] is True,
                 "the reader to keep advancing after a torn row")
    finally:
        reader.stop()


def test_the_log_op_refuses_to_serve_a_dead_readers_last_row(tmp_path: Path):
    """A frozen chamber must not look like a healthy one. The op raises rather than
    handing out a stale row that a caller has no way to recognise as stale."""
    from lumi.contracts.payloads.common import Empty
    from lumi.pascal.handlers import ChamberLogHandler

    class DeadReader:
        config = LogReaderConfig(queue_size=10, idle_time=0.01, log_path=str(tmp_path))

        def is_alive(self) -> bool:
            return False

        def get_log(self):  # would happily answer -- that is the whole problem
            return ({"Motor free": False}, {})

    handler = ChamberLogHandler(DeadReader())
    with pytest.raises(RuntimeError, match="reader thread is not running"):
        asyncio.run(handler.log(Empty()))


def test_every_written_row_is_a_whole_line(tmp_path: Path):
    """The writer's half of the fix: a row reaches the file in one write, so a reader
    can never observe half of it."""
    model = ChamberModel(ChamberSimConfig(temperature_noise=0.0))
    log_dir = tmp_path / "chamber_log"
    writer = SimLogWriter(model, log_dir, log_interval=0.05, tick_interval=0.01)
    path = writer.open_log()

    seen: list[int] = []
    for _ in range(5):
        writer.write_row()
        # After every row the file parses as complete CSV -- no trailing partial line.
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert all(len(r) == len(LOG_COLUMNS) for r in rows), "a row reached disk incomplete"
        assert all(None not in r.values() for r in rows), "a row reached disk with missing columns"
        seen.append(len(rows))

    assert seen == [1, 2, 3, 4, 5]


# --- measured axis speeds ---------------------------------------------------------
#
# 10 deg/s on the rotary axes, 10 mm/s on the masks, ~1s for the shutter. These are the
# numbers a script's timing is built on, so they are asserted as durations rather than
# as config values -- a speed that stops being applied to an axis fails here.


def test_the_masks_move_at_the_measured_speed(model):
    run(model, text(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=50.0, sync=False, nowait=False)))
    # 50 mm at 10 mm/s, and the command blocks for the whole traverse.
    assert model.sim_time == pytest.approx(5.0, abs=0.3)
    assert model.mask1.position == pytest.approx(50.0)


def test_the_carousel_moves_at_the_measured_speed(model):
    # A -> C is two slots. The angles come from PLDconfig, so measure the move rather
    # than assuming a spacing: whatever it is, it should take angle/10 seconds.
    start = model.rotation.position
    run(model, text(pcmd.SelectTarget("C", nowait=False)))
    travelled = abs(model.rotation.position - start)
    assert model.sim_time == pytest.approx(travelled / 10.0, abs=0.3)


def test_rotating_the_sample_takes_time_and_holds_the_motor_interlock(model):
    """`Rotate Sample` used to be instantaneous, which made it the one motion that
    could not be caught in progress by `Motor free`. It is *relative* -- 90 here means
    90 degrees on from wherever the sample is, not to the 90 degree mark."""
    start = model.sample_rot.position
    run(model, text(pcmd.RotateSample(angle=90.0, sync=False, nowait=True)))
    assert not model.motor_free, "an in-progress sample rotation should hold the interlock"

    model.tick(9.0)  # 90 degrees at 10 deg/s
    assert model.motor_free
    assert model.sample_rot.position == pytest.approx((start + 90.0) % 360.0, abs=1.0)


def test_a_spinning_sample_does_not_hold_the_motor_interlock(model):
    """Free spin is not a move: `Sample Rotation ON` advances the angle forever, and if
    that counted as busy every subsequent mask move would raise 'motor is not free'."""
    run(model, text(pcmd.SampleRotationMode(mode="ON")))
    before = model.sample_rot.position
    model.tick(5.0)
    assert model.sample_rot.position != before, "the sample should be spinning"
    assert model.motor_free


def test_set_sample_position_is_absolute_and_rotate_sample_is_relative(model):
    """Two commands, two meanings, and the firmware names do not say which is which.
    `Set Sample Position` is an absolute angle (0-360); `Rotate Sample` turns by one."""
    run(model, text(pcmd.SetSamplePosition(position=30.0, sync=False, nowait=False)))
    assert model.sample_rot.position == pytest.approx(30.0, abs=0.5)

    run(model, text(pcmd.RotateSample(angle=45.0, sync=False, nowait=False)))
    assert model.sample_rot.position == pytest.approx(75.0, abs=0.5)

    # And it wraps rather than running off the end of the circle.
    run(model, text(pcmd.SetSamplePosition(position=350.0, sync=False, nowait=False)))
    run(model, text(pcmd.RotateSample(angle=20.0, sync=False, nowait=False)))
    assert model.sample_rot.position == pytest.approx(10.0, abs=0.5)


def test_the_rheed_gun_moves_at_its_own_slower_speed(model):
    # 1 mm/s, not the masks' 10 mm/s. Its whole travel is +/-3 mm, so a full sweep is
    # seconds -- long enough that `Motor free` catches it.
    run(model, text(pcmd.SetRHEEDGunX(3.0)))
    assert model.sim_time == pytest.approx(3.0, abs=0.3)


def test_the_deposition_gate_takes_time_to_travel(model):
    """Reported as travelling like the sample shutter. A script that opens the gate and
    fires immediately should not be flattered by an instantaneous gate."""
    model.set_gate(True)
    assert not model.gate_idle
    assert not model.gate_open, "the gate should not be open before it has travelled"
    model.tick(model.config.gate_travel_time + 0.01)
    assert model.gate_idle and model.gate_open


def test_pumping_to_a_process_pressure_takes_about_two_minutes(model):
    """Constant pump speed: 1e-7 -> 1e-1 Torr settles in roughly 120s.

    Asserted as a duration rather than as `pressure_tau`, because the time constant is
    a means to this and a re-measure should be free to change it.
    """
    # ~15.2 sccm is 1e-1 Torr on the gas calibration.
    flow = model.flow_for_pressure(1.0e-1)
    model.set_mfc_flow(1, flow)
    model.set_mfc_control(True)

    step, elapsed = 1.0, 0.0
    while elapsed < 600.0 and model.true_pressure < 0.99e-1:
        model.tick(step)
        elapsed += step

    assert elapsed == pytest.approx(120.0, abs=25.0), (
        f"1e-7 -> 1e-1 Torr took {elapsed:.0f}s, expected ~120s"
    )
