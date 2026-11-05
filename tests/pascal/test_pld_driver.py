"""The pld-service driver on the chamber node, end to end over real gRPC.

T0: no broker, no PLD PC. The far end is `SimPldService` -- the chamber simulator behind
the `pld.v1.Chamber` API on an ephemeral loopback port -- and everything on this side is
production code: `PldClient`, the monitors, the growth-log writer and `PldHandler`. The
log it writes is read back through `chamber_log.process_row`, the parser every consumer
of the chamber log goes through.
"""

from __future__ import annotations

import asyncio
import csv
import math

import pytest
import pytest_asyncio

from lumi.contracts.chamber import MI_MODE, PLD
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldControlRequest,
    PldEventQuery,
    PldFireRequest,
    PldLaserHvRequest,
    PldLoggingRequest,
    PldPositionRequest,
    PldPressureRequest,
    PldReadout,
    PldSwitchRequest,
    PldTargetRequest,
    PldTemperatureRequest,
)
from lumi.pascal.chamber_log import process_row
from lumi.pascal.handlers import DisabledCapability
from lumi.pascal.hardware import build_pld_driver, build_sim_pld_service
from lumi.pascal.pld import convert
from lumi.pascal.pld.client import PldClient
from lumi.pascal.pld.monitor import values_from_state
from lumi.pascal.pld.proto import pld_pb2 as pb
from lumi.pascal.sim.columns import LOG_COLUMNS

# Explicit, for runs under tests/pascal/pytest.ini (strict asyncio mode).
pytestmark = pytest.mark.asyncio

SPEED = 200.0


@pytest_asyncio.fixture
async def driver(tmp_path):
    model, service = build_sim_pld_service(time_scale=SPEED)
    service.start()
    handler, workers = build_pld_driver(service.address, tmp_path)
    for worker in workers:
        worker.start()
    for _ in range(100):  # the first state
        if handler.state_monitor.latest is not None:
            break
        await asyncio.sleep(0.05)
    yield handler, model, service
    handler.release_on_shutdown()
    for worker in workers:
        worker.stop()
    service.stop()


async def test_operations_take_control_on_demand(driver):
    handler, _, service = driver
    assert handler.readout().controller == ""
    reply = await handler.select_target(PldTargetRequest(target="B"))
    assert reply.ok, reply.message
    assert service.servicer.controller == "lumi-pascal"
    state = await handler.chamber_state(Empty())
    assert state.motion.target == "B"
    assert state.controller == "lumi-pascal"


async def test_another_holder_is_a_refusal_not_a_steal(driver):
    handler, _, service = driver
    service.servicer.controller = "gui"
    reply = await handler.set_mask_position(PldPositionRequest(position=5.0))
    assert not reply.ok
    assert "gui" in reply.message
    assert service.servicer.controller == "gui"  # never taken by force on its own
    forced = await handler.acquire_control(PldControlRequest(force=True))
    assert forced.granted and forced.controller == "lumi-pascal"


async def test_refusals_come_back_as_answers(driver):
    handler, _, _ = driver
    # Between the low and high pressure regimes.
    reply = await handler.set_pressure(PldPressureRequest(torr=1e-3))
    assert not reply.ok and "regime" in reply.message
    # The excimer takes no settings until its port is taken.
    reply = await handler.set_laser_hv(PldLaserHvRequest(kv=20.0))
    assert not reply.ok and "TakeLaserControl" in reply.message
    assert (await handler.take_laser_control(Empty())).ok
    assert (await handler.set_laser_hv(PldLaserHvRequest(kv=20.0))).ok


async def test_wait_blocks_until_the_chamber_arrives(driver):
    handler, model, _ = driver
    assert (await handler.set_heater_key(PldSwitchRequest(on=True))).ok
    assert (await handler.set_heater_start(PldSwitchRequest(on=True))).ok
    reply = await handler.set_temperature(PldTemperatureRequest(celsius=400, ramp_c_per_min=60, wait=True))
    assert reply.ok, reply.message
    assert model.at_temperature()
    reply = await handler.fire_laser(PldFireRequest(pulses=50, rate_hz=10))
    assert reply.ok
    state = await handler.chamber_state(Empty())
    assert state.laser.pulses_fired == 50
    assert state.heater.pid_on and abs(state.heater.temperature - 400) < 10


async def test_the_growth_log_parses_like_pld_controllers(driver):
    handler, _, _ = driver
    await handler.select_target(PldTargetRequest(target="C"))
    await handler.set_mask_position(PldPositionRequest(position=12.5))
    await handler.set_pressure(PldPressureRequest(torr=1e-2))
    await handler.set_pressure_control(PldSwitchRequest(on=True))
    await asyncio.sleep(1.5)  # a row after the moves

    path = handler.growth_log.path
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == list(LOG_COLUMNS)
    row = process_row(rows[-1])
    assert row["TG No."] == "3"
    assert row["Mask1"] == "12.50"
    # The high regime: IG1 off, so the manager reads the capacitance gauge instead.
    assert row["Vac Pres Main"] == "0.00E+0"
    assert row["MV10 (Main)"] is False
    assert row["Motor free"] is False
    assert handler.readout().log_file == str(path)


async def test_start_logging_rotates_the_file(driver):
    handler, _, _ = driver
    first = handler.growth_log.path
    reply = await handler.start_logging(PldLoggingRequest(file_name="growth_42", interval_s=0.5))
    assert reply.ok and reply.message.endswith("growth_42.csv")
    assert handler.growth_log.path != first
    assert (await handler.stop_logging(Empty())).ok
    assert handler.readout().log_file is None


async def test_the_bus_stream_carries_new_states(driver):
    handler, _, _ = driver
    handler.state_monitor.take_new()
    await asyncio.sleep(1.0)
    item = await handler.next()
    assert item is not None and item.connected and item.backend == "TWIN"


async def test_events_are_kept(driver):
    handler, _, _ = driver
    await handler.select_target(PldTargetRequest(target="A"))
    await asyncio.sleep(0.5)
    kinds = [e.kind for e in (await handler.recent_events(PldEventQuery())).events]
    assert "control" in kinds and "move_done" in kinds


async def test_unreachable_service_is_not_fatal(tmp_path):
    handler, workers = build_pld_driver("127.0.0.1:1", tmp_path)
    try:
        readout = handler.readout()
        assert readout.connected is False and readout.driver == "pld"
    finally:
        handler.client.close()


async def test_nan_readings_become_none_and_zero_in_the_log():
    state = pb.ChamberState(
        heater=pb.Heater(temperature=math.nan, temperature_setpoint=math.nan, pid_on=False),
        pressures=pb.Pressures(ig1=math.nan, cdg10=0.02),
        motion=pb.Motion(target="Clear"),
        backend=pb.HARDWARE, read_only=True,
    )
    model = convert.chamber_state(state)
    assert model.heater.temperature is None and model.pressures.ig1 is None
    assert model.backend == "HARDWARE" and model.read_only is True
    values = values_from_state(state)
    assert values["HT Temp moni"] == 0.0 and values["Vac Pres Main"] == 0.0
    assert values["TG No."] == 7


async def test_the_unused_driver_refuses_every_op():
    pld_off = DisabledCapability(PLD, "use --use-legacy-mi-mode", PldReadout(driver="legacy-mi"))
    with pytest.raises(RuntimeError, match="legacy"):
        await pld_off.select_target(PldTargetRequest(target="A"))
    assert pld_off.readout().driver == "legacy-mi"
    assert await pld_off.next() is None
    # The node probes handlers for these; a catch-all must not answer them.
    assert not hasattr(pld_off, "journal") and not hasattr(pld_off, "journals_own")
    mi_off = DisabledCapability(MI_MODE, "pld drives the chamber", None)
    for op in MI_MODE.ops:
        assert callable(getattr(mi_off, op.name))


async def test_client_refusal_without_control_is_reported(driver):
    _, _, service = driver
    client = PldClient(service.address, client="notebook")
    try:
        service.servicer.controller = "lumi-pascal"
        ok, message = client.call("SetTargetSpin", pb.SwitchRequest(on=True))
        assert not ok and "lumi-pascal" in message
    finally:
        client.close()


async def test_every_handler_serves_its_whole_capability(driver):
    # What EquipmentNode.start checks, without a broker: every op bound to a coroutine.
    from lumi.base.mq.server import CapabilityServer
    from lumi.contracts.payloads.chamber import MIModeReadout

    handler, _, _ = driver
    for cap, h in (
        (PLD, handler),
        (PLD, DisabledCapability(PLD, "off", PldReadout(driver="legacy-mi"))),
        (MI_MODE, DisabledCapability(MI_MODE, "off", MIModeReadout())),
    ):
        server = CapabilityServer(cap, "chamber", h, channel=None, exchange=None, instance_id="t")
        assert set(server._ops) == {op.name for op in cap.ops}
