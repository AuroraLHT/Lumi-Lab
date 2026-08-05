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
    BeginSetLaserPower,
    ConfirmLaserPower,
    MoveTo,
    RegisterSubstrate,
    ToTemperature,
)
from lumi.contracts.payloads.storage import StorageStatus
from lumi.experiment.db import GrowthDB
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration

LOG_VALUES = {
    "Motor free": "TRUE",
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


async def test_move_mask_to_position_requires_motor_free(handler):
    handler.sources["chamber_log"].values["Motor free"] = "FALSE"
    with pytest.raises(RuntimeError):
        await handler.move_mask_to_position(MoveTo(position=50))


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
