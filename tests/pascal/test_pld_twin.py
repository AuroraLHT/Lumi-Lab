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
    PldLaserEnergyRequest,
    PldLaserHvRequest,
    PldLaserModeRequest,
    PldLaserOnRequest,
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
    assert (await h.chamber_state(Empty())).laser.missed_shots == total
