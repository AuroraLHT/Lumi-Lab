"""ExperimentHandler unit tests, mocked sources.

T0: no broker, no hardware. `sources` are lightweight stand-ins for the generated
clients (just the methods manager.py actually calls), not real CapabilityClients --
the same tier as tests/contracts, exercising the ported driving logic and the
pending-confirmation/current-task state machine without a live chamber.
"""

from __future__ import annotations

import asyncio
import time

import pydantic
import pytest

from lumi.contracts.payloads.camera import CameraConfig
from lumi.contracts.payloads.chamber import AllConfigs, ConfigSection, LogBatch, LogEntry
from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.experiment import (
    Anneal,
    AnnealStep,
    BeginSetLaserPower,
    ConfirmLaserPower,
    MoveTo,
    FinishCurrentPixel,
    AddMeasurement,
    ExperimentId,
    ListExperiments,
    ListMeasurements,
    MeasurementId,
    SubstrateId,
    ListQuery,
    ListSamples,
    ListSteps,
    ListSubstrates,
    ProjectId,
    RegisterProject,
    RegisterSubstrate,
    RetireExperiment,
    RetireMeasurement,
    RetireProject,
    UpdateExperiment,
    UpdateMeasurement,
    UpdateProject,
    UpdateSample,
    ReopenPosition,
    ResumeSubstrate,
    RetireSubstrate,
    UpdateSubstrate,
    CheckLogging,
    SampleAngle,
    SetMfcControl,
    SetMfcFlow,
    SetPressure,
    SetPressureControl,
    SetTarget,
    StartMiLogging,
    ToTemperature,
)
from lumi.contracts.payloads.storage import StorageStatus
from lumi.experiment.db import GrowthDB
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration

LOG_VALUES = {
    "HT Temp moni": "160.0",
    "HT set": "160.0",
    "Vac Pres Main": "1.00E-4",
    "Prc Pres Main": "0",
    "Prc Pres Main2": "0",
    "Mask1": "100.0",
    "MissedPuls": "0",
    "MV10 (Main)": "FALSE", "MV11 (Main bypass)": "FALSE", "FV1 (Main)": "FALSE",
    "MV2 (RHEED)": "FALSE", "FV2 (RHEED)": "FALSE", "MV3 (L/L)": "FALSE",
    "FV3 (L/L)": "FALSE", "RV3 (L/L)": "FALSE",
    "DP1 (Main)": "TRUE", "DP2 (2nd RHEED)": "TRUE", "DP3 (L/L)": "TRUE",
    "TMP1 (Main)": "TRUE", "TMP2 (RHEED)": "TRUE", "TMP3 (L/L)": "TRUE", "TMP4 (2nd RHEED)": "TRUE",
    "MFC1 set": "1.0", "MFC1 moni": "1.0",
    # `Heat Stat` bit 3 -- the heater's own ON/OFF monitor. to_temperature refuses to
    # ramp without it, so the default fixture has the laser already running.
    "ON/OFF monitor in PS": "TRUE",
    # `Motor Stat` bit 0 "Motor free" -- the holding lock *released*. A healthy powered
    # chamber holds it clear, so the default fixture is FALSE and a commanded move goes
    # straight through; a move only waits while this is TRUE.
    "Motor free": "FALSE",
}


class FakeChamberLog:
    def __init__(self, values: dict) -> None:
        self.values = dict(values)
        # 0.0 => every log() returns the same timestamp (a frozen log). A positive
        # value advances the clock by that much per call, standing in for PASCAL
        # writing a fresh row. Like production, the clock rides in `values` and the
        # top-level LogEntry.time / .time_stamp stay 0.0 / "".
        self.tick = 0.0
        self._t = 1000.0

    async def log(self) -> LogBatch:
        self._t += self.tick
        values = dict(self.values)
        values["time"] = self._t
        values["time_stamp"] = f"2026-09-10T00:00:{self._t:09.3f}"
        return LogBatch(entries=[LogEntry(time=0.0, time_stamp="", values=values)])


class FakeChamberConfig:
    def __init__(self, tg_values: dict, all_configs: dict | None = None) -> None:
        self.tg_values = tg_values
        self.all_configs = all_configs or {}

    async def get_configs_by_section(self, req):
        return ConfigSection(section=req.section, values=self.tg_values)

    async def get_all_config(self) -> AllConfigs:
        return AllConfigs(configs=self.all_configs)


class FakeMi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, cmd, timeout: float = 30.0):
        self.calls.append(cmd.to_text() if hasattr(cmd, "to_text") else str(cmd))
        return None


class FakeRheedCamera:
    async def get_camera_config(self) -> CameraConfig:
        return CameraConfig(gain=100)

    async def update_camera_config(self, req: CameraConfig) -> Ack:
        return Ack()


class FakeStorage:
    async def start_recording(self, req) -> StorageStatus:
        return StorageStatus(ok=True, message="", project_name=req.project_name, path="/tmp/fake.hdf5")

    async def stop_recording(self) -> StorageStatus:
        return StorageStatus(ok=True, message="")


@pytest.fixture
async def handler(tmp_path):
    db = GrowthDB(str(tmp_path / "growth.db"))
    await db.connect()
    await db.create_database()

    sources = {
        "chamber_mi": FakeMi(),
        "chamber_log": FakeChamberLog(LOG_VALUES),
        "chamber_config": FakeChamberConfig({"TG1name": "SRO"}, {"PIDsettings": {"LDmin": "220"}}),
        "rheed_camera": FakeRheedCamera(),
        "storage": FakeStorage(),
    }
    pld_config = PLDChamberConfiguration(
        center_mask_pos=100.0, center_rheed_pos=0.0, plumb_center=0.0,
        target_to_mask_distance=60.16, mask_to_sample_distance=0.0,
        rheed_limit=(-3.0, 3.0), mask_block_position=75.0,
    )
    bounds = ExperimentBounds(
        mask_travel_max=160.0, temperature_min=160.0, temperature_max=1000.0,
        temperature_pid_engage_threshold=220.0, warm_up_step=0.1,
        warm_up_current_ramp_rate=0.015, warm_up_wait_interval=0.01, warm_up_max_waittime=1.0,
        motor_ready_timeout=0.5,
    )
    h = ExperimentHandler(
        sources=sources, growth_db=db, pld_config=pld_config, bounds=bounds,
        target_mapper={"A": "TG1name"},
    )
    h.build_manager()
    yield h
    await db.close()


# --- readout / state -----------------------------------------------------------


async def test_readout_is_idle_with_no_substrate(handler):
    r = handler.readout()
    assert r.mode == "idle"
    assert r.pending_confirmation is None
    assert r.current_task is None
    assert r.deps_available == {"chamber": False, "rheed": False, "storage": False}


async def test_register_substrate_switches_mode_to_pixel(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    assert len(info.positions) == 3  # -2, 0, 2 given width=10, spacing=2
    assert handler.readout().mode == "pixel"


async def test_register_substrate_single_position_is_mode_single(handler):
    await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, positions=[0.0],
    ))
    assert handler.readout().mode == "single"


# --- chamber reads: the domain-parsing logic ported from manager.py ------------


async def test_is_motor_free_reads_the_log(handler):
    # FALSE in the default fixture: holding lock engaged, motor under command authority.
    assert (await handler.is_motor_free(Empty())).free is False
    handler.sources["chamber_log"].values["Motor free"] = "TRUE"
    assert (await handler.is_motor_free(Empty())).free is True


async def test_get_current_pressure_prefers_vac_gauge(handler):
    result = await handler.get_current_pressure(Empty())
    assert result.pressure == pytest.approx(1e-4)
    assert result.gauge == "Vac Pres Main"


async def test_get_current_pressure_falls_back_to_process_gauge(handler):
    # "Prc Pres Main" == 1 is a selector flag here, not a reading -- the real value
    # then lives in "Prc Pres Main2". Preserved as-is from the original manager.py.
    handler.sources["chamber_log"].values["Vac Pres Main"] = "0.00E+0"
    handler.sources["chamber_log"].values["Prc Pres Main"] = "1"
    handler.sources["chamber_log"].values["Prc Pres Main2"] = "5.5"
    result = await handler.get_current_pressure(Empty())
    assert result.gauge == "Prc Pres Main2"
    assert result.pressure == pytest.approx(5.5)


# --- hardware control bounds ---------------------------------------------------


async def test_move_mask_to_position_rejects_out_of_bounds(handler):
    with pytest.raises(ValueError):
        await handler.move_mask_to_position(MoveTo(position=200))


async def test_move_mask_to_position_refuses_while_the_holding_lock_is_released(handler):
    # "Motor free" TRUE == electromagnet lock released, axis back-driveable by hand
    # (a power cut is the usual cause). A commanded move has nothing to drive, so it
    # waits for the lock to re-engage and raises once bounds.motor_ready_timeout is up.
    handler.sources["chamber_log"].values["Motor free"] = "TRUE"
    with pytest.raises(RuntimeError, match="holding lock released"):
        await handler.move_mask_to_position(MoveTo(position=50))


async def test_set_target_refuses_while_the_holding_lock_is_released(handler):
    # The holding lock frees the target motor too, so SelectTarget / the spin-mode
    # commands are gated exactly like a mask move -- this is the early-stage carousel
    # move that used to go out unguarded.
    handler.sources["chamber_log"].values["Motor free"] = "TRUE"
    with pytest.raises(RuntimeError, match="holding lock released"):
        await handler.set_target(SetTarget(target_id="A"))
    assert handler.sources["chamber_mi"].calls == []


async def test_begin_check_mask_center_refuses_while_the_holding_lock_is_released(handler):
    # The gated calibration ops drive the mask/carousel too; they front their moves
    # with the same interlock.
    handler.sources["chamber_log"].values["Motor free"] = "TRUE"
    with pytest.raises(RuntimeError, match="holding lock released"):
        await handler.begin_check_mask_center(Empty())
    assert handler.sources["chamber_mi"].calls == []


async def test_rotate_sample_to_is_absolute_and_rotate_sample_by_is_relative(handler):
    await handler.rotate_sample_to(SampleAngle(angle=30.0))
    await handler.rotate_sample_by(SampleAngle(angle=-45.0))
    assert handler.sources["chamber_mi"].calls == [
        "Set Sample Position 30.00\n",
        "Rotate Sample -45.00\n",
    ]


async def test_sample_rotation_refuses_while_the_holding_lock_is_released(handler):
    # The holding lock frees the sample-rotation motor too.
    handler.sources["chamber_log"].values["Motor free"] = "TRUE"
    with pytest.raises(RuntimeError, match="holding lock released"):
        await handler.rotate_sample_to(SampleAngle(angle=30.0))
    with pytest.raises(RuntimeError, match="holding lock released"):
        await handler.rotate_sample_by(SampleAngle(angle=10.0))
    assert handler.sources["chamber_mi"].calls == []


# --- MI-mode logging ---------------------------------------------------------


async def test_start_mi_logging_sets_the_interval_then_turns_logging_on(handler):
    status = await handler.start_mi_logging(StartMiLogging(interval_s=1, file_name="run42.csv"))
    assert status.file_name == "run42.csv"
    assert status.interval_s == 1
    assert handler.sources["chamber_mi"].calls == [
        "Log Interval 1\n",
        "Data Logging File=run42.csv\n",
    ]


async def test_start_mi_logging_names_the_file_when_none_is_given(handler):
    status = await handler.start_mi_logging(StartMiLogging())
    assert status.file_name.startswith("chamber_log_") and status.file_name.endswith(".csv")
    assert handler.sources["chamber_mi"].calls == [
        "Log Interval 1\n",
        f"Data Logging File={status.file_name}\n",
    ]


async def test_start_mi_logging_rejects_a_sub_second_interval(handler):
    with pytest.raises(ValueError):
        await handler.start_mi_logging(StartMiLogging(interval_s=0))
    assert handler.sources["chamber_mi"].calls == []


async def test_stop_mi_logging_turns_the_logger_off(handler):
    await handler.stop_mi_logging(Empty())
    assert handler.sources["chamber_mi"].calls == ["Data Logging OFF\n"]


def test_start_mi_logging_rejects_an_unknown_field_instead_of_dropping_it():
    # A mistyped `filename` (for `file_name`) used to be silently ignored, so the op
    # ran with the auto-generated name and no error. The strict model catches it.
    with pytest.raises(pydantic.ValidationError, match="filename"):
        StartMiLogging(interval_s=1, filename="Auto_MI_20260910.csv")
    assert StartMiLogging(file_name="Auto_MI_20260910.csv").file_name == "Auto_MI_20260910.csv"


async def test_check_logging_alive_is_true_when_the_newest_row_advances(handler):
    # The clock rides in `values` and top-level LogEntry.time stays 0.0, exactly as
    # the production log reader delivers it -- the check must not compare .time.
    handler.sources["chamber_log"].tick = 1.0  # every log() read returns a fresh timestamp
    result = await handler.check_logging_alive(CheckLogging(timeout_s=2.0))
    assert result.alive is True
    assert result.waited_s < 2.0
    assert result.last_stamp  # echoes what it saw, for diagnosing a false negative


async def test_check_logging_alive_is_false_when_the_timestamp_is_frozen(handler):
    # tick stays 0.0: the fake serves the same row forever, like a dead log reader.
    result = await handler.check_logging_alive(CheckLogging(timeout_s=0.2))
    assert result.alive is False
    assert result.waited_s >= 0.2


# --- gas: setpoints and their gates are separate ops ---------------------------


async def test_set_mfc_flow_only_sets_the_setpoint(handler):
    # The gate is a separate op on purpose. Asserting the *absence* of `MFC Control`
    # here is the point: an op that quietly opened the gas line would pass a test that
    # only checked the flow command.
    await handler.set_mfc_flow(SetMfcFlow(mfc_id="1", flow=5.0))
    assert handler.sources["chamber_mi"].calls == ["MFC1 Flow Set= 5.00\n"]


async def test_set_mfc_control_opens_and_closes_the_master_gate(handler):
    await handler.set_mfc_control(SetMfcControl(enabled=True))
    await handler.set_mfc_control(SetMfcControl(enabled=False))
    assert handler.sources["chamber_mi"].calls == ["MFC Control Enable\n", "MFC Control Disable\n"]


async def test_set_pressure_and_pressure_control_are_separate(handler):
    await handler.set_pressure(SetPressure(pressure=20.0e-3))
    await handler.set_pressure_control(SetPressureControl(on=True))
    await handler.set_pressure_control(SetPressureControl(on=False))
    assert handler.sources["chamber_mi"].calls == [
        "Set Pressure= 2.00E-2\n", "Pressure Control ON\n", "Pressure Control OFF\n",
    ]


# --- pending-confirmation gate --------------------------------------------------


async def test_laser_power_gate_requires_matching_kind_to_resolve(handler):
    await handler.begin_set_laser_power(BeginSetLaserPower(laser_power=1.8))
    pending = handler.readout().pending_confirmation
    assert pending is not None and pending.kind == "laser_power"

    with pytest.raises(ValueError):
        await handler.confirm_rheed_gain(Empty())  # wrong gate

    result = await handler.confirm_laser_power(ConfirmLaserPower(measured_power=1.75))
    assert result.measured_power == 1.75
    assert handler.readout().pending_confirmation is None
    assert handler.readout().laser_power_real == 1.75


async def test_laser_power_gate_short_circuits_once_already_set(handler):
    await handler.begin_set_laser_power(BeginSetLaserPower(laser_power=1.8))
    await handler.confirm_laser_power(ConfirmLaserPower(measured_power=1.75))

    # Same value, not forced: begin should NOT open a new gate.
    await handler.begin_set_laser_power(BeginSetLaserPower(laser_power=1.8))
    assert handler.readout().pending_confirmation is None


# --- long-running task tracking --------------------------------------------------


async def test_long_running_task_tracked_and_cleared(handler):
    ack = await handler.to_temperature(ToTemperature(temperature=200, ramp_rate=20))
    assert handler.readout().current_task is not None
    assert handler.readout().current_task.id == ack.task_id

    for _ in range(200):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            assert event.task_result["ok"] is True
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("task never reported completion")

    assert handler.readout().current_task is None


async def test_to_temperature_refuses_with_the_heating_laser_off(handler):
    # PID with the diode off drives nothing: the setpoint climbs, the current stays at
    # zero and the pyrometer sits at Pyro_min. Worse, TemperatureSet goes out
    # nowait=False, so the MI command blocks until it times out rather than erroring.
    handler.sources["chamber_log"].values["ON/OFF monitor in PS"] = "FALSE"
    with pytest.raises(RuntimeError, match="initiate_heating_laser"):
        await handler.to_temperature(ToTemperature(temperature=700, ramp_rate=20))

    # Raised by the op, not reported on the update channel: a caller that only has ops
    # (the MCP server) cannot read current_task, so a refusal buried in the task would
    # look to it like a ramp that started fine.
    assert handler.readout().current_task is None
    # And nothing reached the chamber -- no setpoint left behind to act on later.
    assert handler.sources["chamber_mi"].calls == []


async def test_anneal_refuses_with_the_heating_laser_off(handler):
    handler.sources["chamber_log"].values["ON/OFF monitor in PS"] = "FALSE"
    with pytest.raises(RuntimeError, match="initiate_heating_laser"):
        await handler.anneal(Anneal(steps=[AnnealStep(temperature=700, ramp_rate=20, wait_time=0)]))
    assert handler.readout().current_task is None


async def test_to_temperature_below_the_pid_threshold_skips_the_heating_laser_check(handler):
    # A room-temperature growth runs with the diode deliberately off. A setpoint below
    # temperature_pid_engage_threshold has no ramp to perform, so it must not refuse --
    # and it must not leave a setpoint on the controller either.
    handler.sources["chamber_log"].values["ON/OFF monitor in PS"] = "FALSE"
    ack = await handler.to_temperature(ToTemperature(temperature=25, ramp_rate=20))

    for _ in range(200):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            assert event.task_result["ok"] is True
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("task never reported completion")

    assert handler.readout().current_task is None
    assert handler.sources["chamber_mi"].calls == []
    assert ack.task_id


async def test_to_temperature_below_the_pid_threshold_cools_down_when_still_hot(handler):
    # The sub-threshold branch keys off where the chamber *is*, not only what was
    # asked for. From 700C, asking for the pyrometer floor means "come down" -- it has
    # to go out as a real setpoint, not return a success that leaves the chamber hot.
    handler.sources["chamber_log"].values["HT Temp moni"] = "700.0"
    ack = await handler.to_temperature(ToTemperature(temperature=160, ramp_rate=20))

    for _ in range(200):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            assert event.task_result["ok"] is True
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("task never reported completion")

    # Ramp + setpoint, the way cool_down does it. No `Temperature Control PID`: the
    # controller cannot hold a sub-threshold setpoint, the substrate coasts to it.
    assert handler.sources["chamber_mi"].calls == [
        "Temperature Ramp 20.0\n",
        "Temperature Set 160.0\n",
    ]
    assert ack.task_id


# --- resuming a substrate ------------------------------------------------------


async def test_resume_substrate_restores_spent_positions(handler):
    """A resumed campaign must not hand back a pixel that has already been grown on.

    resume_substrate used to rebuild the Substrate with current_position_id = 0 and an
    empty accessed set, so restarting the node mid-campaign silently rewound to the
    first position and deposited on top of it.
    """
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    assert len(info.positions) == 3

    # Two growths happened before the restart.
    for pixel in (0, 1):
        await handler.growth_db.add_experiment(
            substrate_id=info.substrate_id, is_pixel=True, pixel_location=pixel,
        )

    handler.manager.substrates.clear()  # as if the node had restarted
    resumed = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))

    assert resumed.current_pixel_index == 2
    assert handler.manager.current_substrate.accessible_positions == [info.positions[2].position]


async def test_resume_substrate_with_no_history_starts_at_zero(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    handler.manager.substrates.clear()
    resumed = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))
    assert resumed.current_pixel_index == 0
    assert len(handler.manager.current_substrate.accessible_positions) == 3


async def test_register_substrate_materialises_one_sample_per_position(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
        substrate_name="STO-42",
    ))
    rows = await handler.growth_db.get_samples_for_substrate(info.substrate_id)
    kinds = [r[4] for r in rows]
    assert kinds.count("substrate") == 1      # the root
    assert kinds.count("position") == 3       # one per growable position
    names = sorted(r[7] for r in rows if r[4] == "position")
    assert names == ["STO-42-p0", "STO-42-p1", "STO-42-p2"]


async def test_finishing_a_pixel_marks_its_sample_grown(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.finish_current_pixel(FinishCurrentPixel())

    grown = await handler.growth_db.find_sample(info.substrate_id, 0)
    still_planned = await handler.growth_db.find_sample(info.substrate_id, 1)
    assert grown[8] == "grown"
    assert still_planned[8] == "planned"


async def test_deposition_records_the_material_not_just_the_slot(handler):
    """The carousel's slot-to-material map is chamber config and changes when targets
    are swapped, so a step that recorded only `target_id: "A"` names a different
    material after the next swap. The name is resolved at journal time instead."""
    from lumi.contracts.payloads.experiment import PerformDeposition

    captured: list[dict] = []

    class Recorder:
        async def begin(self, kind, *, params=None, **_):
            captured.append(params or {})
            return 0

        async def end(self, *a, **k):
            pass

    handler.journal = Recorder()
    await handler.perform_deposition(PerformDeposition(
        target_id="A", num_pulse=500, laser_repetition_rate=2.78, is_dryrun=True,
    ))

    assert captured[0]["target_material"] == "SRO"   # FakeChamberConfig's TG1name
    assert captured[0]["target_id"] == "A"
    assert captured[0]["is_dryrun"] is True


async def test_start_storage_names_the_recording_after_the_sample(handler):
    """A record UUID alone doesn't say what was grown. The recording is named after
    the sample actually loaded -- which already carries its position, if it has one
    -- so a stray .hdf5 in storage can be traced back without opening it."""
    from lumi.contracts.payloads.experiment import StartStorage

    await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
        substrate_name="STO-42",
    ))

    result = await handler.start_storage(StartStorage(project_name="demo"))

    assert result.storage_name.startswith("STO-42-p0_demo_")
    assert result.storage_name.endswith(result.record_uuid)


async def test_start_storage_falls_back_to_project_name_with_no_sample_loaded(handler):
    from lumi.contracts.payloads.experiment import StartStorage

    result = await handler.start_storage(StartStorage(project_name="demo"))

    assert result.storage_name == f"demo_{result.record_uuid}"


# --- bookkeeping corrections ---------------------------------------------------
# Registration was write-once: a substrate typed in with the wrong size, or a position
# finished by accident, had no way back, and the only way to keep depositing was to
# register a second, fictional substrate. These pin the undo paths.


async def test_update_substrate_fixes_a_mistyped_size_and_rebuilds_positions(handler):
    """The reported case: registered, then noticed the size was wrong.

    width drives _compute_positions, so correcting it has to re-derive the positions
    and their sample rows -- leaving a 3-position substrate that now says width=20
    would be worse than not allowing the edit at all.
    """
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
        substrate_name="STO-42",
    ))
    assert len(info.positions) == 3

    fixed = await handler.update_substrate(UpdateSubstrate(width=20.0))

    # width=20, spacing=2 reaches +-4mm before the fixture's rheed_limit of +-3 bites,
    # so the RHEED drop is re-applied too rather than only on first registration.
    assert fixed.width == 20.0
    assert [p.position for p in fixed.positions] == [-2.0, 0.0, 2.0]
    rows = await handler.growth_db.get_samples_for_substrate(info.substrate_id)
    assert [r[4] for r in rows].count("position") == 3
    assert [r[4] for r in rows].count("substrate") == 1  # the root survives the rebuild

    # ...and it is persisted, not just patched in memory.
    handler.manager.substrates.clear()
    resumed = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))
    assert resumed.width == 20.0


async def test_update_substrate_edits_description_without_touching_positions(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    before = [p.position for p in info.positions]

    updated = await handler.update_substrate(UpdateSubstrate(
        substrate_id=info.substrate_id, materials="LaAlO3", substrate_name="LAO-1",
    ))

    assert updated.materials == "LaAlO3"
    assert updated.substrate_name == "LAO-1"
    assert updated.orientation == "(001)"          # untouched fields keep their value
    assert [p.position for p in updated.positions] == before


async def test_update_substrate_refuses_geometry_once_a_growth_is_recorded(handler):
    """Renumbering pixels under an experiment row would relocate a growth that already
    happened. Descriptive fields stay editable -- the refusal is about geometry only."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.growth_db.add_experiment(
        substrate_id=info.substrate_id, is_pixel=True, pixel_location=0,
    )

    with pytest.raises(ValueError, match="cannot be changed"):
        await handler.update_substrate(UpdateSubstrate(width=20.0))

    renamed = await handler.update_substrate(UpdateSubstrate(substrate_name="STO-7"))
    assert renamed.substrate_name == "STO-7"


async def test_reopen_position_undoes_an_accidental_finish(handler):
    """The other reported case: finished a substrate by mistake and had to register a
    fake one to carry on. Nothing was deposited, so the position goes straight back."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.finish_substrate(Empty())
    assert handler.manager.current_substrate.current_position_id == 1

    result = await handler.reopen_position(ReopenPosition())

    assert result.index == 0
    assert result.substrate.current_pixel_index == 0
    sample = await handler.growth_db.find_sample(info.substrate_id, 0)
    assert sample[8] == "planned"
    assert len(handler.manager.current_substrate.accessible_positions) == 3


async def test_reopen_position_survives_a_resume(handler):
    """An accidental finish writes no experiment row, so restore_progress agrees the
    position is free -- the undo is not just an in-memory patch."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.finish_substrate(Empty())
    await handler.reopen_position(ReopenPosition())

    handler.manager.substrates.clear()
    resumed = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))
    assert resumed.current_pixel_index == 0


async def test_reopen_position_refuses_a_position_that_was_really_grown(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.growth_db.add_experiment(
        substrate_id=info.substrate_id, is_pixel=True, pixel_location=0,
    )
    await handler.finish_substrate(Empty())

    with pytest.raises(ValueError, match="recorded growth"):
        await handler.reopen_position(ReopenPosition(index=0))

    forced = await handler.reopen_position(ReopenPosition(index=0, force=True))
    assert forced.index == 0


async def test_list_substrates_reports_what_is_left_and_hides_retired(handler):
    """resume_substrate takes an id and nothing offered a way to learn one."""
    first = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
        substrate_name="STO-42",
    ))
    await handler.growth_db.add_experiment(
        substrate_id=first.substrate_id, is_pixel=True, pixel_location=0,
    )
    second = await handler.register_substrate(RegisterSubstrate(
        materials="LaAlO3", orientation="(001)", width=10, positions=[0.0],
    ))

    # Newest first by default -- what a browsing UI wants on page one.
    listed = await handler.list_substrates(ListSubstrates())
    assert [s.substrate_id for s in listed.substrates] == [second.substrate_id, first.substrate_id]
    assert listed.page.total == 2
    oldest = listed.substrates[-1]
    assert oldest.substrate_name == "STO-42"
    assert (oldest.num_positions, oldest.num_used) == (3, 1)
    assert oldest.created_at is not None and oldest.created_at_iso.endswith("Z")

    ascending = await handler.list_substrates(ListSubstrates(order="asc"))
    assert [s.substrate_id for s in ascending.substrates] == [first.substrate_id, second.substrate_id]

    await handler.retire_substrate(RetireSubstrate(substrate_id=second.substrate_id))

    hidden = await handler.list_substrates(ListSubstrates())
    assert [s.substrate_id for s in hidden.substrates] == [first.substrate_id]
    assert hidden.page.total == 1  # the pager's total respects the filter too
    with_retired = await handler.list_substrates(ListSubstrates(include_retired=True))
    assert len(with_retired.substrates) == 2
    assert with_retired.substrates[0].state == "retired"


async def test_retire_substrate_keeps_its_samples_and_is_reversible(handler):
    """Retiring is the delete that isn't one: growth.db has no restore path, so a
    mistaken registration is hidden rather than removed."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    await handler.retire_substrate(RetireSubstrate(substrate_id=info.substrate_id))

    assert len(await handler.growth_db.get_samples_for_substrate(info.substrate_id)) == 4
    assert await handler.growth_db.get_substrate(info.substrate_id) is not None

    restored = await handler.retire_substrate(
        RetireSubstrate(substrate_id=info.substrate_id, retire=False)
    )
    assert restored.state == "active"


async def test_unload_substrate_leaves_the_record_alone(handler):
    """register/resume push onto a stack, so loading the wrong substrate used to need
    a node restart."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))

    after = await handler.unload_substrate(Empty())

    assert after.substrate is None
    assert handler.readout().mode == "idle"
    assert await handler.growth_db.get_substrate(info.substrate_id) is not None


async def test_migration_adds_state_to_a_database_without_it(tmp_path):
    """An existing growth.db predates the column; CREATE TABLE IF NOT EXISTS is a
    no-op on it, so the migration is the only thing that keeps it readable."""
    db = GrowthDB(str(tmp_path / "old.db"))
    await db.connect()
    await db.conn.execute(
        """CREATE TABLE substrate (
            substrate_id INTEGER PRIMARY KEY, substrate_uuid VARCHAR(100) NOT NULL,
            materials VARCHAR(100) NOT NULL, orientation VARCHAR(50), height FLOAT,
            width FLOAT, thickness FLOAT, pixel_spacing FLOAT, positions TEXT,
            manufacture VARCHAR(50), manufacture_date VARCHAR(50), substrate_name TEXT
        )"""
    )
    await db.conn.execute(
        "INSERT INTO substrate (substrate_uuid, materials) VALUES ('u-1', 'SrTiO3')"
    )
    await db.conn.commit()

    await db.create_database()

    assert "state" in {c[1] for c in await db.get_table_columns("substrate")}
    assert [r[0] for r in await db.get_substrates()] == [1]  # the old row is still listed
    await db.close()


# --- growth.db as data: the CRUD surface a management UI reads ------------------


async def test_list_query_pages_and_windows_by_time(handler):
    """One query shape for every entity: limit/offset for 'the last 5', since/until
    for 'registered this month'. Neither was expressible before."""
    for i in range(7):
        await handler.register_substrate(RegisterSubstrate(
            materials="SrTiO3", orientation="(001)", width=10, positions=[0.0],
            substrate_name=f"STO-{i}",
        ))

    page = await handler.list_substrates(ListSubstrates(limit=5))
    assert len(page.substrates) == 5
    assert page.page.total == 7          # the total ignores the limit, as a pager needs
    assert page.page.has_more is True

    second = await handler.list_substrates(ListSubstrates(limit=5, offset=5))
    assert len(second.substrates) == 2
    assert second.page.has_more is False

    # A window that starts in the future matches nothing; one that starts in the past
    # matches everything. Both ends are honoured.
    assert (await handler.list_substrates(
        ListSubstrates(since=time.time() + 3600))).page.total == 0
    assert (await handler.list_substrates(
        ListSubstrates(since=time.time() - 3600))).page.total == 7
    assert (await handler.list_substrates(
        ListSubstrates(until=time.time() - 3600))).page.total == 0


async def test_list_substrates_search_matches_descriptive_columns(handler):
    await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, positions=[0.0],
        substrate_name="STO-42",
    ))
    await handler.register_substrate(RegisterSubstrate(
        materials="LaAlO3", orientation="(001)", width=10, positions=[0.0],
        substrate_name="LAO-1",
    ))

    assert (await handler.list_substrates(ListSubstrates(search="LaAlO"))).page.total == 1
    assert (await handler.list_substrates(ListSubstrates(search="STO"))).page.total == 1
    assert (await handler.list_substrates(ListSubstrates(search="(001)"))).page.total == 2


async def test_timestamps_are_epoch_and_iso_for_the_same_instant(handler):
    """Every timestamped payload carries both renderings of one stored column, so a
    UI gets something sortable and something printable without parsing."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, positions=[0.0],
    ))
    full = await handler.get_substrate(SubstrateId(substrate_id=info.substrate_id))

    assert isinstance(full.created_at, float)
    assert full.created_at_iso.endswith("Z")
    # Same instant, not two clocks: the ISO string parses back to the epoch value.
    from lumi.experiment.db import to_epoch
    assert to_epoch(full.created_at_iso) == pytest.approx(full.created_at, abs=1)
    assert full.created_at == pytest.approx(time.time(), abs=60)


async def test_project_crud_round_trip(handler):
    project_id = (await handler.register_project(
        RegisterProject(project_name="demo", description="first pass")
    )).project_id

    listed = await handler.list_projects(ListQuery())
    assert [p.project_id for p in listed.projects] == [project_id]
    assert listed.projects[0].description == "first pass"
    assert listed.projects[0].created_at is not None

    renamed = await handler.update_project(
        UpdateProject(project_id=project_id, project_name="demo-v2")
    )
    assert renamed.project_name == "demo-v2"
    assert renamed.description == "first pass"   # untouched field survives

    retired = await handler.retire_project(RetireProject(project_id=project_id))
    assert retired.state == "retired"
    assert (await handler.list_projects(ListQuery())).page.total == 0
    assert (await handler.list_projects(ListQuery(include_retired=True))).page.total == 1

    restored = await handler.retire_project(RetireProject(project_id=project_id, retire=False))
    assert restored.state == "active"


async def test_update_experiment_corrects_a_mistyped_reading(handler):
    """The measured values are what a person read off an instrument, so correcting
    one is a correction -- but identity is not editable."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    experiment_id = await handler.growth_db.add_experiment(
        substrate_id=info.substrate_id, is_pixel=True, pixel_location=0,
        temperature=700.0, pressure=1e-2,
    )

    fixed = await handler.update_experiment(
        UpdateExperiment(experiment_id=experiment_id, pressure=1e-4)
    )
    assert fixed.pressure == pytest.approx(1e-4)
    assert fixed.temperature == pytest.approx(700.0)      # untouched
    assert fixed.substrate_id == info.substrate_id        # identity is not editable

    listed = await handler.list_experiments(ListExperiments(substrate_id=info.substrate_id))
    assert [e.experiment_id for e in listed.experiments] == [experiment_id]
    assert listed.experiments[0].pressure == pytest.approx(1e-4)


async def test_retiring_a_mistaken_growth_hands_its_pixel_back(handler):
    """The payoff of soft-deleting rather than deleting: a dry run logged as a real
    growth stops consuming a position, and the loaded substrate notices immediately."""
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    experiment_id = await handler.growth_db.add_experiment(
        substrate_id=info.substrate_id, is_pixel=True, pixel_location=0,
    )
    handler.manager.substrates.clear()
    resumed = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))
    assert resumed.current_pixel_index == 1          # position 0 is spent

    await handler.retire_experiment(RetireExperiment(experiment_id=experiment_id))

    # In memory now...
    assert handler.manager.current_substrate.current_position_id == 0
    # ...and on the next resume, because get_used_pixel_indices skips retired rows.
    handler.manager.substrates.clear()
    again = await handler.resume_substrate(ResumeSubstrate(substrate_id=info.substrate_id))
    assert again.current_pixel_index == 0


async def test_measurement_crud_and_epoch_timestamp(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    sample = (await handler.growth_db.find_sample(info.substrate_id, 0))[0]
    measurement_id = (await handler.add_measurement(AddMeasurement(
        sample_id=sample, kind="rheed_metric", value=0.42, detail={"note": "draft"},
    ))).measurement_id

    one = await handler.get_measurement(MeasurementId(measurement_id=measurement_id))
    assert isinstance(one.created_at, float)         # was a raw SQLite string before
    assert one.created_at_iso.endswith("Z")
    assert one.value == pytest.approx(0.42)

    fixed = await handler.update_measurement(UpdateMeasurement(
        measurement_id=measurement_id, value=0.51, detail={"note": "final"},
    ))
    assert fixed.value == pytest.approx(0.51)
    assert fixed.detail == {"note": "final"}

    await handler.retire_measurement(RetireMeasurement(measurement_id=measurement_id))
    # A wrong value stops reaching an optimiser without vanishing from the record.
    assert (await handler.list_measurements(ListMeasurements())).page.total == 0
    assert (await handler.list_measurements(
        ListMeasurements(include_retired=True))).page.total == 1


async def test_update_sample_renames_without_touching_geometry(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, pixel_spacing=2.0,
    ))
    sample_id = (await handler.growth_db.find_sample(info.substrate_id, 1))[0]

    updated = await handler.update_sample(UpdateSample(
        sample_id=sample_id, sample_name="corner-piece", notes="cleaved 2026-09-18",
    ))
    assert updated.sample_name == "corner-piece"
    assert updated.notes == "cleaved 2026-09-18"
    assert updated.pixel_index == 1                  # identity untouched

    listed = await handler.list_samples(ListSamples(substrate_id=info.substrate_id))
    assert listed.page.total == 4                    # root + 3 positions
    assert any(s.sample_name == "corner-piece" for s in listed.samples)


async def test_steps_are_not_editable(handler):
    """The journal is the audit trail: it gets a reader and no writer, so what
    happened cannot be quietly rewritten after the fact."""
    with pytest.raises(ValueError, match="not editable"):
        await handler.growth_db.update_row("step", 1, kind="something-else")
    with pytest.raises(ValueError, match="cannot be retired"):
        await handler.growth_db.set_row_state("step", 1, "retired")


async def test_unknown_column_raises_rather_than_no_opping(handler):
    info = await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=10, positions=[0.0],
    ))
    with pytest.raises(ValueError, match="not editable on substrate"):
        await handler.growth_db.update_row("substrate", info.substrate_id, colour="blue")
