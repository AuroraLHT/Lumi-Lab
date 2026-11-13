"""The pld driver against the real pld-service on REA's digital twin.

Opt-in: set LUMI_PLD_TWIN to the service's address once both are running, e.g.

    uv run rea-twin --tcp 127.0.0.1:5003                       # in the REA repo
    control/target/debug/pld-service --twin 127.0.0.1:5003 --listen 127.0.0.1:50051
    LUMI_PLD_TWIN=127.0.0.1:50051 uv run pytest tests/pascal/test_pld_twin.py

Never point it at a service started with --hardware: these tests move the chamber and
switch the laser. The tests refuse to run unless the service reports the TWIN backend.
"""

from __future__ import annotations

import asyncio
import csv
import os

import pytest
import pytest_asyncio

from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldFireRequest,
    PldPositionRequest,
    PldLaserEnergyRequest,
    PldLaserHvRequest,
    PldLaserModeRequest,
    PldLaserOnRequest,
    PldPumpRequest,
)
from lumi.experiment.chamber import PldChamber, PldStopped
from lumi.pascal.chamber_log import process_row

ADDRESS = os.environ.get("LUMI_PLD_TWIN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not ADDRESS, reason="set LUMI_PLD_TWIN to a pld-service --twin address"),
]


class HandlerBus:
    """ChamberPldClient's shape over the handler, without the broker."""

    def __init__(self, handler) -> None:
        self.handler = handler

    async def call(self, op, req=None):
        return await getattr(self.handler, op)(req if req is not None else Empty())

    async def chamber_state(self):
        return await self.handler.chamber_state(Empty())


@pytest_asyncio.fixture
async def handler(tmp_path):
    from lumi.pascal.hardware import build_pld_driver

    h, workers = build_pld_driver(ADDRESS, tmp_path)
    for worker in workers:
        worker.start()
    for _ in range(100):
        if h.state_monitor.latest is not None:
            break
        await asyncio.sleep(0.05)
    state = await h.chamber_state(Empty())
    if state.backend != "TWIN":
        for worker in workers:
            worker.stop()
        pytest.fail(f"{ADDRESS} is pld-service on {state.backend}, not the twin; refusing to drive it")
    yield h
    await h.laser_off(Empty())
    await h.release_laser(Empty())
    h.release_on_shutdown()
    for worker in workers:
        worker.stop()


async def test_excimer_cm_rules_and_switching_on(handler):
    h = handler
    await h.release_laser(Empty())  # a known start: CM=1 (refused, harmlessly, if already so)
    reply = await h.laser_off(Empty())
    assert not reply.ok and "CM=1" in reply.message
    assert (await h.chamber_state(Empty())).excimer.control is False

    # Every snapshot carries the laser's latest state, so a CM change shows at once.

    assert (await h.take_laser_control(Empty())).ok
    assert (await h.chamber_state(Empty())).excimer.control is True
    assert (await h.set_laser_mode(PldLaserModeRequest(mode="hv"))).ok
    assert not (await h.set_laser_hv(PldLaserHvRequest(kv=26))).ok
    assert (await h.set_laser_hv(PldLaserHvRequest(kv=20))).ok
    assert not (await h.set_laser_energy(PldLaserEnergyRequest(mj=450))).ok
    assert (await h.set_laser_energy(PldLaserEnergyRequest(mj=250))).ok

    assert (await h.laser_on(PldLaserOnRequest(wait=False))).ok
    await asyncio.sleep(1.0)
    excimer = (await h.chamber_state(Empty())).excimer
    assert excimer.waiting and excimer.status == "OFF,WAIT"
    reply = await h.laser_on(PldLaserOnRequest(wait=True))
    assert reply.ok, reply.message
    excimer = (await h.chamber_state(Empty())).excimer
    assert excimer.opmode == "ON" and excimer.hv == pytest.approx(20.0)
    assert (await h.laser_standby(Empty())).ok
    assert (await h.laser_off(Empty())).ok
    assert (await h.release_laser(Empty())).ok
    assert (await h.chamber_state(Empty())).excimer.control is False


async def test_the_experiment_driver_drives_the_chamber(handler):
    # lumi.experiment.chamber.PldChamber, the experiment node's default backend.
    h = handler
    chamber = PldChamber(HandlerBus(h))
    status, message = await chamber.check()
    assert status == "on", message
    before = (await h.chamber_state(Empty())).motion.sample_angle or 0.0
    await chamber.select_target("B")
    await chamber.target_auto_motion()
    await chamber.move_mask(10.0)
    await chamber.rotate_sample_by(15)
    await chamber.sample_shutter(True)
    await chamber.fire(20, 10)
    await chamber.set_mfc_flow(1, 2.0)
    await chamber.start_logging("twin_run", 1)
    readings = await chamber.readings()
    assert readings["TG No."] == 2 and readings["Mask1"] == pytest.approx(10.0, abs=0.05)
    state = await h.chamber_state(Empty())
    assert state.motion.target == "B"
    assert state.motion.mask_position == pytest.approx(10.0, abs=0.05)
    assert state.motion.sample_angle == pytest.approx((before + 15) % 360, abs=0.5)
    assert state.sample_shutter_away
    assert state.laser.pulses_fired == 20
    assert state.controller == "lumi-pascal"

    await asyncio.sleep(2.5)
    path = h.growth_log.path
    assert path.name == "twin_run.csv"
    with path.open(newline="") as f:
        row = process_row(list(csv.DictReader(f))[-1])
    assert row["TG No."] == "2" and row["Mask1"] == "10.00"


async def test_a_fire_reports_its_own_missed_shots(handler):
    h = handler
    total = (await h.chamber_state(Empty())).laser.missed_shots  # the PLC's running total
    reply = await h.fire_laser(PldFireRequest(pulses=10, rate_hz=10, wait=True))
    assert reply.ok, reply.message
    assert "fired 10 of 10 pulses, 0 missed" in reply.message
    state = await h.chamber_state(Empty())
    assert state.laser.missed_shots == total
    # The train's settings stay in the state after it ends (REA 6d8df20).
    assert (state.laser.rate_hz, state.laser.pulses_requested) == (10.0, 10)
    # Auto spin and twist are on by default (36c1eea), and wait=true returns once the
    # twist has stopped at centre: the turret is free for the next SelectTarget.
    assert state.motion.auto_spin and state.motion.auto_twist
    assert state.motion.axes["TG"] == "IDLE"


async def test_stop_fire_aborts_a_deposition(handler):
    # The REA GUI (a client holding no control) stops a waited fire mid-train; the
    # deposition step fails as an abort and is not re-fired.
    from lumi.pascal.pld.client import PldClient
    from lumi.pascal.pld.proto import pld_pb2 as pb

    h = handler
    assert (await h.stop_fire(Empty())).message == "the laser is not firing: nothing sent"
    chamber = PldChamber(HandlerBus(h))
    before = (await h.chamber_state(Empty())).laser.pulses_fired
    fire = asyncio.create_task(chamber.fire(1000, 10))
    await asyncio.sleep(3.0)
    gui = PldClient(ADDRESS, client="pld-gui")
    try:
        ok, message = await asyncio.to_thread(gui.call, "StopFire", pb.Empty())
    finally:
        gui.close()
    assert ok, message
    with pytest.raises(PldStopped, match="stopped by request: fired"):
        await asyncio.wait_for(fire, 60)
    state = await h.chamber_state(Empty())
    assert 0 < state.laser.pulses_fired - before < 1000
    assert state.laser.stop_requested  # until the next fire (REA 05f8b85)
    assert state.controller == "lumi-pascal"


async def test_stop_deposition_ends_the_drivers_task(handler, tmp_path):
    # experiment.driver.stop_deposition over the twin: the deposition task ends aborted
    # and the train stops part-way.
    import sys
    from pathlib import Path

    from lumi.contracts.payloads.experiment import PerformDeposition, StopDeposition

    sys.path.insert(0, str(Path(__file__).parents[1] / "experiment"))
    from test_pld_chamber import make_handler, wait_task

    h = handler
    driver, db = await make_handler(tmp_path, PldChamber(HandlerBus(h)))
    try:
        before = (await h.chamber_state(Empty())).laser.pulses_fired
        await driver.perform_deposition(PerformDeposition(
            num_pulse=1000, laser_repetition_rate=10.0, target_id="A", is_dryrun=False))
        for _ in range(300):  # past the carousel move, into the train
            if (await h.chamber_state(Empty())).laser.pulses_fired > before:
                break
            await asyncio.sleep(0.1)
        reply = await driver.stop_deposition(StopDeposition(reason="twin test"))
        assert reply.task_kind == "perform_deposition", reply
        assert reply.laser != "the laser is not firing: nothing sent", reply
        result = await wait_task(driver)
        assert result == {"ok": False, "aborted": True, "error": "stopped by request: twin test"}
        await asyncio.sleep(2.0)
        state = await h.chamber_state(Empty())
        fired = state.laser.pulses_fired - before
        await asyncio.sleep(1.0)
        assert 0 < fired == (await h.chamber_state(Empty())).laser.pulses_fired - before < 1000
    finally:
        await db.close()


async def test_pumps_load_lock_and_heater_gains_are_read(handler):
    # 9858307: read only, plus the refusals that send nothing. Venting is left alone.
    h = handler
    state = await h.chamber_state(Empty())
    assert {"TMP1", "TMP20", "TMP21", "TMP3", "DP1", "DP2", "DP3"} <= set(state.pumps)
    assert state.load_lock.gv1 in ("closed", "open", "moving")
    assert state.heater.kp is not None and state.heater.auto_tune is False
    assert (state.heater.tune_cycles_done, state.heater.tune_cycles, state.heater.writes_failing) == (0, 0, False)
    assert state.session.plc_session and state.limits.ig1_max_torr > 0 and state.limits.heater_max_amps > 0
    # The experiment driver's readings name them as PLD Controller's log does.
    readings = await PldChamber(HandlerBus(h)).readings()
    assert all(readings[name] for name in ("DP1 (Main)", "TMP1 (Main)", "TMP2 (RHEED)", "TMP3 (L/L)", "TMP4 (2nd RHEED)"))
    reply = await h.cancel_load_lock(Empty())
    assert reply.ok, reply.message  # nothing running: nothing sent
    if state.pumps["TMP3"].running:  # a dry pump backing a running TMP stays on
        reply = await h.set_pump(PldPumpRequest(pump="DP3", on=False))
        assert not reply.ok, reply.message
        assert (await h.chamber_state(Empty())).pumps["DP3"].running


async def test_another_client_holding_control_is_a_refusal(handler):
    # pld-service answers PERMISSION_DENIED; that is a refusal naming the holder, not an
    # unreachable service, and nothing is taken from the holder.
    from lumi.pascal.pld.client import PldClient

    h = handler
    h.release_on_shutdown()
    gui = PldClient(ADDRESS, client="pld-gui")
    try:
        assert (await asyncio.to_thread(gui.acquire, False)).granted
        # GetState names the holder as soon as AcquireControl returns (REA d0f5cd5).
        assert (await asyncio.to_thread(gui.state)).controller == "pld-gui"
        reply = await h.set_mask_position(PldPositionRequest(position=12.0, wait=True))
        assert not reply.ok and "pld-gui" in reply.message
        # Still the GUI's: lumi-pascal cannot take it without force.
        lumi = PldClient(ADDRESS, client="lumi-pascal")
        try:
            assert not (await asyncio.to_thread(lumi.acquire, False)).granted
        finally:
            lumi.close()
    finally:
        await asyncio.to_thread(gui.release)
        gui.close()


async def test_cancel_task_holds_the_ld_current(handler, tmp_path):
    # experiment.driver.cancel_task over the twin: the to_temperature task ends aborted,
    # the PID goes off with the LD at the current it carried, and the waited
    # SetTemperature behind the task ends with it rather than running on in the node.
    import sys
    from pathlib import Path

    from lumi.contracts.payloads.experiment import CancelTask, ToTemperature

    sys.path.insert(0, str(Path(__file__).parents[1] / "experiment"))
    from test_pld_chamber import make_handler, wait_task

    h = handler
    driver, db = await make_handler(tmp_path, PldChamber(HandlerBus(h)))
    try:
        start = (await h.chamber_state(Empty())).heater.temperature
        if start is None or start < 250:
            pytest.skip(f"the twin's substrate is at {start} C; this needs it above the PID floor")
        await driver.manager.initiate_heating_laser(timeout=30)
        await driver.to_temperature(ToTemperature(temperature=start + 200, ramp_rate=1.0))
        for _ in range(100):
            if (await h.chamber_state(Empty())).heater.temperature_goal == start + 200:
                break
            await asyncio.sleep(0.1)
        reply = await driver.cancel_task(CancelTask(reason="twin test"))
        assert reply.task_kind == "to_temperature" and reply.chamber.startswith("PID off: LD held at"), reply
        assert await wait_task(driver) == {"ok": False, "aborted": True, "error": "stopped by request: twin test"}
        for _ in range(50):  # the state is the service's poll, a cycle behind the reply
            heater = (await h.chamber_state(Empty())).heater
            if not heater.pid_on:
                break
            await asyncio.sleep(0.1)
        assert not heater.pid_on
        held = heater.current_set
        await asyncio.sleep(3.0)
        heater = (await h.chamber_state(Empty())).heater
        assert not heater.pid_on and heater.current_set == held
    finally:
        await db.close()
