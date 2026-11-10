"""The `chamber.pld` capability: pld-service on the bus.

pld-service listens on 127.0.0.1 only and refuses remote binds by design, so the rest
of the lab cannot reach it directly. This node runs on the PLD PC beside it and is the
bridge: the broker carries the requests (its accounts, its TLS, and its per-op topic
permissions from `lumi.contracts.policy`), and this handler makes the local gRPC call.
No new port is opened on the PLD PC.

Every operation is a blocking gRPC call (a `wait=true` move or ramp blocks until the
chamber arrives), so each runs in a worker thread and the node's event loop stays free.

The excimer takes settings only from the port its CM names. This handler never moves CM
on its own: take_laser_control and release_laser are the only ops that do, nothing is
sent to the laser when the node stops, and a laser op refused for want of CM comes back
as the service's refusal ("the laser's other port has control (CM=1)"); the readout's
`laser_control` is `excimer.control` (current from pld-service 50c5f4f on; before that it
could lag a take_laser_control by a poll, 2 s). With the service read-only (hardware without
--allow-operations) every operation is refused here and nothing is sent.
"""

from __future__ import annotations

import asyncio
import logging

from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldChamberState,
    PldControlReply,
    PldControlRequest,
    PldEventList,
    PldEventQuery,
    PldLoggingRequest,
    PldReadout,
    PldReply,
)
from lumi.pascal.pld import convert
from lumi.pascal.pld.client import PldClient, PldUnavailable
from lumi.pascal.pld.monitor import GrowthLog, PldEventMonitor, PldStateMonitor
from lumi.pascal.pld.proto import pld_pb2 as pb

log = logging.getLogger(__name__)


def _op(rpc: str, build):
    """A handler method for one operation: build the request, call, answer {ok, message}."""

    async def method(self: "PldHandler", req) -> PldReply:
        return await self._operate(rpc, build(req))

    method.__name__ = rpc
    return method


class PldHandler:
    def __init__(self, client: PldClient, state_monitor: PldStateMonitor,
                 event_monitor: PldEventMonitor, growth_log: GrowthLog) -> None:
        self.client = client
        self.state_monitor = state_monitor
        self.event_monitor = event_monitor
        self.growth_log = growth_log

    # --- readings -----------------------------------------------------------------

    async def chamber_state(self, req: Empty) -> PldChamberState:
        # Fresh from the service rather than the monitor's copy: a caller that just
        # moved something wants the poll after it, not up to a second before.
        try:
            state = await asyncio.to_thread(self.client.state)
        except PldUnavailable:
            if self.state_monitor.latest is None:
                raise
            raise RuntimeError(
                f"pld-service at {self.client.address} is not answering; the last state "
                "it sent is stale and is not returned"
            ) from None
        return convert.chamber_state(state)

    async def recent_events(self, req: PldEventQuery) -> PldEventList:
        events = list(self.event_monitor.events)[-req.limit:]
        return PldEventList(events=[convert.event(e) for e in events])

    async def next(self) -> PldChamberState | None:
        state = self.state_monitor.take_new()
        return convert.chamber_state(state) if state is not None else None

    # --- control ------------------------------------------------------------------

    async def acquire_control(self, req: PldControlRequest) -> PldControlReply:
        reply = await asyncio.to_thread(self.client.acquire, req.force)
        return PldControlReply(granted=reply.granted, controller=reply.controller, message=reply.message)

    async def release_control(self, req: Empty) -> PldControlReply:
        reply = await asyncio.to_thread(self.client.release)
        return PldControlReply(granted=reply.granted, controller=reply.controller, message=reply.message)

    # --- operations ---------------------------------------------------------------

    async def _operate(self, rpc: str, request) -> PldReply:
        latest = self.state_monitor.latest
        if latest is not None and latest.read_only:
            return PldReply(ok=False, message=(
                f"{rpc} not sent: pld-service is read-only (hardware without --allow-operations)"))
        ok, message = await asyncio.to_thread(self.client.call, rpc, request)
        if not ok:
            log.warning("pld-service refused %s: %s", rpc, message)
        return PldReply(ok=ok, message=message)

    set_valve = _op("SetValve", lambda r: pb.ValveRequest(valve=r.valve, open=r.open, wait=r.wait))
    select_target = _op("SelectTarget", lambda r: pb.TargetRequest(target=r.target, wait=r.wait))
    set_mask_position = _op("SetMaskPosition", lambda r: pb.PositionRequest(position=r.position, wait=r.wait))
    set_sample_position = _op("SetSamplePosition", lambda r: pb.PositionRequest(position=r.position, wait=r.wait))
    set_rheed_gun_x = _op("SetRheedGunX", lambda r: pb.PositionRequest(position=r.position, wait=r.wait))
    home_axis = _op("HomeAxis", lambda r: pb.AxisRequest(axis=r.axis, wait=r.wait))
    set_target_spin = _op("SetTargetSpin", lambda r: pb.SwitchRequest(on=r.on))
    set_target_twist = _op("SetTargetTwist", lambda r: pb.SwitchRequest(on=r.on))
    set_motor_free = _op("SetMotorFree", lambda r: pb.SwitchRequest(on=r.on))
    set_target_auto_motion = _op("SetTargetAutoMotion", lambda r: pb.AutoMotionRequest(spin=r.spin, twist=r.twist))
    set_heater_key = _op("SetHeaterKey", lambda r: pb.SwitchRequest(on=r.on))
    set_heater_start = _op("SetHeaterStart", lambda r: pb.SwitchRequest(on=r.on))
    set_heater_threshold = _op("SetHeaterThreshold", lambda r: pb.SwitchRequest(on=r.on))
    set_heating_current = _op("SetHeatingCurrent", lambda r: pb.CurrentRequest(amps=r.amps))
    set_heater_current_limits = _op(
        "SetHeaterCurrentLimits", lambda r: pb.CurrentLimitsRequest(min_amps=r.min_amps, max_amps=r.max_amps))
    set_temperature = _op("SetTemperature", lambda r: pb.TemperatureRequest(
        celsius=r.celsius, ramp_c_per_min=r.ramp_c_per_min, wait=r.wait, tolerance=r.tolerance))
    set_temperature_control = _op("SetTemperatureControl", lambda r: pb.SwitchRequest(on=r.on))
    set_sample_shutter = _op("SetSampleShutter", lambda r: pb.ShutterRequest(in_front=r.in_front))
    set_mfc_flow = _op("SetMfcFlow", lambda r: pb.FlowRequest(mfc=r.mfc, sccm=r.sccm))
    set_pressure = _op("SetPressure", lambda r: pb.PressureRequest(
        torr=r.torr, gauge=r.gauge, wait=r.wait, tolerance=r.tolerance))
    set_pressure_control = _op("SetPressureControl", lambda r: pb.SwitchRequest(on=r.on))
    set_pressure_flow_limits = _op(
        "SetPressureFlowLimits", lambda r: pb.FlowLimitsRequest(min_sccm=r.min_sccm, max_sccm=r.max_sccm))
    tune_pressure = _op("TunePressure", lambda r: pb.SwitchRequest(on=r.on))
    set_ion_gauge = _op("SetIonGauge", lambda r: pb.IonGaugeRequest(gauge=r.gauge, on=r.on))
    tune_heater = _op("TuneHeater", lambda r: pb.SwitchRequest(on=r.on))
    auto_vent = _op("AutoVent", lambda r: pb.Empty())
    auto_vac = _op("AutoVac", lambda r: pb.Empty())
    cancel_load_lock = _op("CancelLoadLock", lambda r: pb.Empty())
    set_pump = _op("SetPump", lambda r: pb.PumpRequest(pump=r.pump, on=r.on))
    take_laser_control = _op("TakeLaserControl", lambda r: pb.Empty())
    laser_on = _op("LaserOn", lambda r: pb.LaserOnRequest(wait=r.wait))
    laser_standby = _op("LaserStandby", lambda r: pb.Empty())
    laser_off = _op("LaserOff", lambda r: pb.Empty())
    set_laser_mode = _op("SetLaserMode", lambda r: pb.LaserModeRequest(mode=r.mode))
    set_laser_hv = _op("SetLaserHv", lambda r: pb.LaserHvRequest(kv=r.kv))
    set_laser_energy = _op("SetLaserEnergy", lambda r: pb.LaserEnergyRequest(mj=r.mj))
    release_laser = _op("ReleaseLaser", lambda r: pb.Empty())

    async def fire_laser(self, req) -> PldReply:
        # The service has no rate reading; the log's LaserHz and Laser set are what was
        # asked for here.
        self.state_monitor.laser_rate = float(req.rate_hz)
        self.state_monitor.laser_set = int(req.pulses)
        return await self._operate("FireLaser", pb.LaserRequest(pulses=req.pulses, rate_hz=req.rate_hz, wait=req.wait))

    # Needs no control, and runs alongside a waited fire_laser (each op is its own thread).
    stop_fire = _op("StopFire", lambda r: pb.Empty())

    # --- the growth log -----------------------------------------------------------

    async def start_logging(self, req: PldLoggingRequest) -> PldReply:
        path = await asyncio.to_thread(self.growth_log.start, req.file_name)
        self.state_monitor.interval_s = float(req.interval_s)
        return PldReply(ok=True, message=str(path))

    async def stop_logging(self, req: Empty) -> PldReply:
        await asyncio.to_thread(self.growth_log.stop)
        return PldReply(ok=True, message="growth log closed")

    # --- state --------------------------------------------------------------------

    def readout(self) -> PldReadout:
        latest = self.state_monitor.latest
        return PldReadout(
            driver="pld",
            address=self.client.address,
            connected=self.state_monitor.connected,
            backend=pb.Backend.Name(latest.backend) if latest is not None else "",
            controller=latest.controller if latest is not None else "",
            holds_control=latest is not None and latest.controller == self.client.client,
            read_only=bool(latest.read_only) if latest is not None else False,
            laser_control=latest.excimer.control if latest is not None and latest.excimer.present else None,
            log_file=str(self.growth_log.path) if self.growth_log.active else None,
        )

    def release_on_shutdown(self) -> None:
        """Hand control back when the node stops, so the REA GUI is not locked out."""
        latest = self.state_monitor.latest
        if latest is None or latest.controller != self.client.client:
            return
        try:
            self.client.release()
            log.info("released pld-service control")
        except PldUnavailable as e:
            log.warning("could not release pld-service control: %s", e)

