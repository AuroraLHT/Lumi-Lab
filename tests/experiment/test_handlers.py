"""ExperimentHandler unit tests, mocked sources.

T0: no broker, no hardware. `sources` are lightweight stand-ins for the generated
clients (just the methods manager.py actually calls), not real CapabilityClients --
the same tier as tests/contracts, exercising the ported driving logic and the
pending-confirmation/current-task state machine without a live chamber.
"""

from __future__ import annotations

import asyncio

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
    RegisterSubstrate,
    ResumeSubstrate,
    SetMfcControl,
    SetMfcFlow,
    SetPressure,
    SetPressureControl,
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

    async def log(self) -> LogBatch:
        return LogBatch(entries=[LogEntry(time=0.0, time_stamp="t0", values=self.values)])


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
