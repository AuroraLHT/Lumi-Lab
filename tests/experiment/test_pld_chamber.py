"""The experiment driver on pld-service: `PldChamber` under `ExperimentHandler`.

T0: no broker. Two layers:

- a recording fake of `ChamberPldClient`, for exactly which `chamber.pld` op each
  experiment op issues, with which fields, in which order;
- the whole path in-process -- ExperimentHandler -> manager -> PldChamber -> the chamber
  node's PldHandler -> gRPC -> SimPldService -- for the flows that matter end to end.
"""

from __future__ import annotations

import asyncio
import math

import pytest
import pytest_asyncio

from lumi.base.mq.client import RemoteError
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.experiment import (
    BeginSetLaserPower,
    CheckMiMode,
    PerformDeposition,
    SampleAngle,
    SetMfcControl,
    SetPressure,
    SetTarget,
    StartMiLogging,
    StopDeposition,
    ToTemperature,
)
from lumi.contracts.payloads.pld import (
    PldChamberState,
    PldHeater,
    PldMotion,
    PldPressures,
    PldPump,
    PldReply,
)
from lumi.experiment.chamber import MiChamber, PldChamber, PldRefused, readings_from_state
from lumi.experiment.db import GrowthDB
from lumi.experiment.handlers import ExperimentHandler
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration

from test_handlers import LOG_VALUES, FakeChamberConfig, FakeChamberLog, FakeMi, FakeRheedCamera, FakeStorage


def hot_state(**heater) -> PldChamberState:
    """A chamber with the heater running at 300 C under manual current, axes locked."""
    return PldChamberState(
        connected=True, backend="TWIN",
        heater=PldHeater(temperature=300.0, started=True, power_on=True, current_set=6.0, **heater),
        motion=PldMotion(target="A", sample_angle=350.0, mask_position=100.0),
        pressures=PldPressures(ig1=1e-6, ig1_on=True, cdg10=0.0),
        valves={"MV10": True, "MV3": True},
        pumps={"TMP1": PldPump(running=True), "DP1": PldPump(running=True)},
    )


class FakePldClient:
    def __init__(self, state: PldChamberState) -> None:
        self.state = state
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, str] = {}
        self.raise_remote: str | None = None
        #: Ops that block, as a real fire or carousel move does, until `release` is set.
        self.blocking: set[str] = set()
        self.release = asyncio.Event()

    async def call(self, op, req=None):
        if self.raise_remote:
            raise RemoteError("chamber.pld", op, "RuntimeError", self.raise_remote)
        self.calls.append((op, req.model_dump() if req is not None and not isinstance(req, Empty) else {}))
        if op in self.blocking:
            await self.release.wait()
        if op in self.refuse:
            return PldReply(ok=False, message=self.refuse[op])
        return PldReply(ok=True, message="done")

    async def chamber_state(self):
        return self.state


def ops(client):
    return [op for op, _ in client.calls]


async def wait_task(handler) -> dict:
    for _ in range(2000):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            return event.task_result
        await asyncio.sleep(0.01)
    pytest.fail("task never reported completion")


BOUNDS = dict(
    mask_travel_max=160.0, temperature_min=160.0, temperature_max=1000.0,
    temperature_pid_engage_threshold=220.0, warm_up_current=7.8, warm_up_step=0.1,
    warm_up_current_ramp_rate=0.015, warm_up_wait_interval=0.01, warm_up_max_waittime=1.0,
    motor_ready_timeout=0.5,
)
PLD_CONFIG = dict(
    center_mask_pos=100.0, center_rheed_pos=0.0, plumb_center=0.0,
    target_to_mask_distance=60.16, mask_to_sample_distance=0.0,
    rheed_limit=(-3.0, 3.0), mask_block_position=75.0,
)


async def make_handler(tmp_path, chamber, *, bounds=None) -> tuple[ExperimentHandler, GrowthDB]:
    db = GrowthDB(str(tmp_path / "growth.db"))
    await db.connect()
    await db.create_database()
    sources = {
        "chamber": chamber,
        "chamber_log": FakeChamberLog(LOG_VALUES),
        "chamber_config": FakeChamberConfig({"TG1name": "SRO"}, {"PIDsettings": {"LDmin": "220"}}),
        "rheed_camera": FakeRheedCamera(),
        "storage": FakeStorage(),
    }
    h = ExperimentHandler(
        sources=sources, growth_db=db, pld_config=PLDChamberConfiguration(**PLD_CONFIG),
        bounds=ExperimentBounds(**(bounds or BOUNDS)), target_mapper={"A": "TG1name"},
    )
    h.build_manager()
    return h, db


@pytest_asyncio.fixture
async def fake(tmp_path):
    client = FakePldClient(hot_state())
    handler, db = await make_handler(tmp_path, PldChamber(client))
    yield handler, client
    await db.close()


# --- which ops ----------------------------------------------------------------------


async def test_to_temperature_engages_pid_then_ramps_from_where_it_is(fake):
    handler, client = fake
    await handler.to_temperature(ToTemperature(temperature=650, ramp_rate=20))
    assert (await wait_task(handler))["ok"] is True
    assert client.calls == [
        ("set_temperature_control", {"on": True}),
        ("set_temperature", {"celsius": 650.0, "ramp_c_per_min": 20.0, "wait": True, "tolerance": 0.0}),
    ]


async def test_cool_down_is_one_setpoint(fake):
    handler, client = fake
    await handler.manager.cool_down(ramp_rate=10)
    assert client.calls == [("set_temperature", {
        "celsius": 160.0, "ramp_c_per_min": 10.0, "wait": True, "tolerance": 0.0})]


async def test_heater_on_and_off(fake):
    handler, client = fake
    await handler.manager.initiate_heating_laser(timeout=1)
    assert client.calls == [(op, {"on": True}) for op in (
        "set_heater_key", "set_heater_start", "set_heater_threshold", "set_temperature_control")]
    client.calls.clear()
    client.state.heater.temperature = 150.0
    await handler.manager.turn_off_heating_laser()
    assert client.calls == [(op, {"on": False}) for op in (
        "set_temperature_control", "set_heater_threshold", "set_heater_start", "set_heater_key")]


async def test_set_target_turns_on_auto_spin_and_twist(fake):
    handler, client = fake
    await handler.set_target(SetTarget(target_id="B"))
    assert client.calls == [
        ("select_target", {"target": "B", "wait": True}),
        ("set_target_auto_motion", {"spin": True, "twist": True}),
    ]


async def test_clear_is_sent_by_name(fake):
    # pcmd.Targets.Clear str()s as "Targets.Clear"; the service wants "Clear".
    handler, client = fake
    await handler.begin_set_laser_power(BeginSetLaserPower(laser_power=100.0))
    assert client.calls == [
        ("set_sample_shutter", {"in_front": True}),
        ("select_target", {"target": "Clear", "wait": True}),
    ]


async def test_rotation_is_absolute_and_wraps(fake):
    handler, client = fake
    await handler.rotate_sample_by(SampleAngle(angle=20.0))
    await handler.rotate_sample_to(SampleAngle(angle=-90.0))
    assert client.calls == [
        ("set_sample_position", {"position": pytest.approx(10.0), "wait": True}),
        ("set_sample_position", {"position": 270.0, "wait": True}),
    ]


async def test_deposition_opens_the_shutter_and_fires_waiting(fake):
    handler, client = fake
    await handler.perform_deposition(PerformDeposition(
        num_pulse=300, laser_repetition_rate=5.0, target_id="A", is_dryrun=False))
    assert (await wait_task(handler))["ok"] is True
    assert client.calls[-2:] == [
        ("set_sample_shutter", {"in_front": False}),
        ("fire_laser", {"pulses": 300, "rate_hz": 5.0, "wait": True}),
    ]


async def test_a_stopped_deposition_fails_as_an_abort_and_is_not_refired(fake):
    handler, client = fake
    client.refuse["fire_laser"] = "stopped by request: fired 412 of 1000 pulses"
    await handler.perform_deposition(PerformDeposition(
        num_pulse=1000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
    result = await wait_task(handler)
    assert result["ok"] is False
    assert result["error"].startswith("PldStopped:") and "fired 412 of 1000" in result["error"]
    assert result["aborted"] is True
    assert ops(client).count("fire_laser") == 1


async def until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition never held")


async def test_stop_deposition_ends_the_task_then_stops_the_laser(fake):
    handler, client = fake
    client.blocking.add("fire_laser")
    ack = await handler.perform_deposition(PerformDeposition(
        num_pulse=1000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
    await until(lambda: "fire_laser" in ops(client))
    reply = await handler.stop_deposition(StopDeposition(reason="plume looked wrong"))
    assert (reply.task_id, reply.task_kind, reply.laser) == (ack.task_id, "perform_deposition", "done")
    assert ops(client)[-2:] == ["fire_laser", "stop_fire"]
    assert handler.readout().current_task is None
    result = await wait_task(handler)
    assert result == {"ok": False, "aborted": True, "error": "stopped by request: plume looked wrong"}
    # The motion claim went with the task: the chamber can be moved again.
    await handler.set_target(SetTarget(target_id="B"))
    assert ops(client).count("fire_laser") == 1


async def test_stop_deposition_while_the_carousel_turns_never_fires(fake):
    # Cancelled before StopFire is sent, so the fire it had yet to issue never starts.
    handler, client = fake
    client.blocking.add("select_target")
    await handler.perform_deposition(PerformDeposition(
        num_pulse=1000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
    await until(lambda: "select_target" in ops(client))
    reply = await handler.stop_deposition(StopDeposition())
    assert reply.task_kind == "perform_deposition"
    client.release.set()
    await asyncio.sleep(0.05)
    assert "fire_laser" not in ops(client) and ops(client)[-1] == "stop_fire"
    assert (await wait_task(handler))["error"] == "stopped by request: stop_deposition"


async def test_stop_deposition_with_nothing_running_still_stops_the_laser(fake):
    # A fire_laser of someone's own has no task behind it; another task is not touched.
    handler, client = fake
    reply = await handler.stop_deposition(StopDeposition())
    assert (reply.task_id, ops(client)) == (None, ["stop_fire"])
    client.blocking.add("set_temperature")
    await handler.to_temperature(ToTemperature(temperature=400, ramp_rate=10))
    await until(lambda: "set_temperature" in ops(client))
    assert (await handler.stop_deposition(StopDeposition())).task_id is None
    assert handler.readout().current_task.kind == "to_temperature"
    client.release.set()
    assert (await wait_task(handler))["ok"] is True


async def test_stop_deposition_is_refused_under_legacy_mi(fake):
    handler, client = fake
    handler.manager.chamber = MiChamber(FakeMi(), handler.sources["chamber_log"])
    with pytest.raises(RuntimeError, match="press STOP on PASCAL"):
        await handler.stop_deposition(StopDeposition())


async def test_pressure_goes_to_the_service_even_when_set_by_hand_under_mi(fake):
    handler, client = fake
    assert handler.pressure_by_hand  # the setting is for legacy MI mode only
    await handler.set_pressure(SetPressure(pressure=0.01))
    assert handler.readout().pending_confirmation is None
    assert client.calls == [("set_pressure", {"torr": 0.01, "gauge": "", "wait": False, "tolerance": 0.0})]


async def test_mfc_gate_and_logging(fake):
    handler, client = fake
    await handler.set_mfc_control(SetMfcControl(enabled=True))
    await handler.set_mfc_control(SetMfcControl(enabled=False))
    status = await handler.start_mi_logging(StartMiLogging(interval_s=2, file_name="run.csv"))
    assert status.file_name == "run.csv"
    assert client.calls == [
        ("set_mfc_flow", {"mfc": 1, "sccm": 0.0}),
        ("start_logging", {"file_name": "run.csv", "interval_s": 2.0}),
    ]
    with pytest.raises(PldRefused, match="MFC2"):
        await handler.manager.set_mfc_flow("2", 1.0)


async def test_a_refusal_raises_with_the_services_reason(fake):
    handler, client = fake
    client.refuse["select_target"] = "motor holding lock is released"
    with pytest.raises(PldRefused, match="holding lock"):
        await handler.set_target(SetTarget(target_id="A"))


async def test_a_legacy_chamber_node_says_which_flag_to_pass(fake):
    handler, client = fake
    client.raise_remote = "this chamber node runs --use-legacy-mi-mode, so the pld-service driver is off"
    with pytest.raises(RuntimeError, match="experiment node defaults to the pld driver"):
        await handler.set_target(SetTarget(target_id="A"))


@pytest.mark.parametrize("state,status", [
    (PldChamberState(connected=True, backend="TWIN"), "on"),
    (PldChamberState(connected=False), "off"),
    (PldChamberState(connected=True, read_only=True), "off"),
    (PldChamberState(connected=True, controller="pld-gui"), "busy"),
    (PldChamberState(connected=True, controller="lumi-pascal"), "on"),
])
async def test_check(tmp_path, state, status):
    handler, db = await make_handler(tmp_path, PldChamber(FakePldClient(state)))
    try:
        result = await handler.check_mi_mode(CheckMiMode(timeout_s=1))
        assert result.status == status, result.message
    finally:
        await db.close()


# --- readings off the live state ----------------------------------------------------


async def test_readings_come_from_the_service_not_the_log(fake):
    handler, client = fake
    m = handler.manager
    assert await m.get_current_temperature() == 300.0
    assert await m.get_current_pressure() == (pytest.approx(1e-6), "Vac Pres Main")
    assert await m.get_current_mask_position() == 100.0
    assert await m.is_heating_laser_on() is True
    assert await m.is_motor_free() is False
    assert (await m.get_current_target())[0] == 1
    valves = await m.get_valve_status()
    assert valves["MV10 (Main)"] and valves["MV3 (L/L)"] and not valves["FV1 (Main)"]
    pumps = await m.get_pump_status()
    assert pumps["TMP1 (Main)"] and pumps["DP1 (Main)"] and not pumps["TMP3 (L/L)"]
    # The high regime: IG1 off, so the capacitance gauge.
    client.state.pressures = PldPressures(ig1=None, ig1_on=False, cdg10=0.01)
    assert await m.get_current_pressure() == (pytest.approx(0.01), "Prc Pres Main")


def test_no_reading_is_zero_not_nan():
    row = readings_from_state(PldChamberState())
    assert row["HT Temp moni"] == 0.0 and not math.isnan(row["Vac Pres Main"])
    assert row["Motor free"] is False and row["TG No."] == 0


# --- end to end over gRPC, against the simulator ------------------------------------


class HandlerBus:
    """ChamberPldClient's shape over the chamber node's PldHandler, without a broker."""

    def __init__(self, pld_handler) -> None:
        self.pld = pld_handler

    async def call(self, op, req=None):
        return await getattr(self.pld, op)(req if req is not None else Empty())

    async def chamber_state(self):
        return await self.pld.chamber_state(Empty())


@pytest_asyncio.fixture
async def sim(tmp_path):
    from lumi.pascal.hardware import build_pld_driver, build_sim_pld_service

    model, service = build_sim_pld_service(time_scale=200.0)
    service.start()
    pld, workers = build_pld_driver(service.address, tmp_path / "log")
    for worker in workers:
        worker.start()
    fast = dict(BOUNDS, warm_up_current_ramp_rate=50.0, warm_up_max_waittime=20.0, warm_up_wait_interval=0.05)
    handler, db = await make_handler(tmp_path, PldChamber(HandlerBus(pld)), bounds=fast)
    yield handler, model, service
    pld.release_on_shutdown()
    for worker in workers:
        worker.stop()
    service.stop()
    await db.close()


async def test_a_growth_runs_end_to_end_on_the_sim(sim):
    handler, model, service = sim
    assert (await handler.check_mi_mode(CheckMiMode(timeout_s=1))).status == "on"
    await handler.manager.initiate_heating_laser(timeout=5)
    await handler.to_temperature(ToTemperature(temperature=400, ramp_rate=120))
    result = await wait_task(handler)
    assert result["ok"] is True, result
    assert abs(await handler.manager.get_current_temperature() - 400) < 10

    await handler.perform_deposition(PerformDeposition(
        num_pulse=50, laser_repetition_rate=10.0, target_id="C", is_dryrun=False))
    result = await wait_task(handler)
    assert result["ok"] is True, result
    state = await handler.manager.chamber.state()
    assert state.motion.target == "C" and state.laser.pulses_fired == 50
    assert state.sample_shutter_away and state.motion.auto_spin and state.motion.auto_twist
    assert service.servicer.controller == "lumi-pascal"


async def test_stop_fire_from_another_client_aborts_the_deposition(sim):
    from lumi.pascal.pld.client import PldClient
    from lumi.pascal.pld.proto import pld_pb2 as pb

    handler, model, service = sim
    await handler.perform_deposition(PerformDeposition(
        num_pulse=100_000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
    await asyncio.sleep(1.0)
    gui = PldClient(service.address, client="pld-gui")
    try:
        ok, message = await asyncio.to_thread(gui.call, "StopFire", pb.Empty())
    finally:
        gui.close()
    assert ok, message
    result = await wait_task(handler)
    assert result["ok"] is False and result["error"].startswith("PldStopped:")
    assert result["aborted"] is True
    assert 0 < model.laser_pulses < 100_000


async def test_stop_deposition_on_the_sim(sim):
    handler, model, service = sim
    await handler.perform_deposition(PerformDeposition(
        num_pulse=100_000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
    await asyncio.sleep(1.0)
    reply = await handler.stop_deposition(StopDeposition(reason="test"))
    assert reply.task_kind == "perform_deposition" and reply.laser.startswith("stop sent"), reply
    result = await wait_task(handler)
    assert result["aborted"] is True and result["error"] == "stopped by request: test"
    fired = model.laser_pulses
    await asyncio.sleep(0.5)
    assert model.laser_idle and 0 < fired == model.laser_pulses < 100_000
    laser = (await handler.manager.chamber.state()).laser
    assert laser.stop_requested and laser.pulses_requested == 100_000
