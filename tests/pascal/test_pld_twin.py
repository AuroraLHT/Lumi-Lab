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
from lumi.experiment.pld import PldCommandRunner
from lumi.pascal import command as pcmd
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

    async def chamber_state(self, req=None):
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


async def test_mi_commands_drive_the_chamber(handler):
    h = handler
    runner = PldCommandRunner(HandlerBus(h))
    status, message = await runner.check()
    assert status == "on", message
    before = (await h.chamber_state(Empty())).motion.sample_angle or 0.0
    for command in (
        pcmd.SelectTarget("B", nowait=False),
        pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=10.0, sync=False, nowait=False),
        pcmd.RotateSample(15, sync=False, nowait=False),
        pcmd.SampleShutter(pcmd.PascalState("ON")),
        pcmd.TriggerLaser(num_pulse=20, frequency=10, sync=False, nowait=False),
        pcmd.SetMFC1Flow(2.0),
        pcmd.DataLogging("twin_run", pcmd.PascalState("ON")),
    ):
        await runner.execute(command, timeout=120)
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
    # Auto spin and twist are on by default (36c1eea), and wait=true returns once the
    # twist has stopped at centre: the turret is free for the next SelectTarget.
    assert state.motion.auto_spin and state.motion.auto_twist
    assert state.motion.axes["TG"] == "IDLE"


async def test_stop_fire_aborts_a_deposition(handler):
    # The REA GUI (a client holding no control) stops a waited fire mid-train; the
    # deposition step fails as an abort and is not re-fired.
    from lumi.experiment.pld import PldStopped
    from lumi.pascal.pld.client import PldClient
    from lumi.pascal.pld.proto import pld_pb2 as pb

    h = handler
    assert (await h.stop_fire(Empty())).message == "the laser is not firing: nothing sent"
    runner = PldCommandRunner(HandlerBus(h))
    before = (await h.chamber_state(Empty())).laser.pulses_fired
    fire = asyncio.create_task(runner.execute(
        pcmd.TriggerLaser(num_pulse=1000, frequency=10, sync=False, nowait=False)))
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
    assert state.controller == "lumi-pascal"


async def test_pumps_load_lock_and_heater_gains_are_read(handler):
    # 9858307: read only, plus the refusals that send nothing. Venting is left alone.
    h = handler
    state = await h.chamber_state(Empty())
    assert {"TMP1", "TMP20", "TMP21", "TMP3", "DP1", "DP2", "DP3"} <= set(state.pumps)
    assert state.load_lock.gv1 in ("closed", "open", "moving")
    assert state.heater.kp is not None and state.heater.auto_tune is False
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
        reply = await h.set_mask_position(PldPositionRequest(position=12.0, wait=True))
        assert not reply.ok and "pld-gui" in reply.message
        # Still the GUI's: lumi-pascal cannot take it without force. (ChamberState's
        # `controller` is not read here: pld-service copies it in on its next poll.)
        lumi = PldClient(ADDRESS, client="lumi-pascal")
        try:
            assert not (await asyncio.to_thread(lumi.acquire, False)).granted
        finally:
            lumi.close()
    finally:
        await asyncio.to_thread(gui.release)
        gui.close()
