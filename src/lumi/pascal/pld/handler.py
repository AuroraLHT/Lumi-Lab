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
import math
import time

from google.protobuf import json_format

from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldChamberState,
    PldControlReply,
    PldControlRequest,
    PldActivityList,
    PldActivityQuery,
    PldBypassMoveRequest,
    PldBypassSpeedRequest,
    PldBypassStepsRequest,
    PldEventList,
    PldEventQuery,
    PldLoggingRequest,
    PldReadout,
    PldReply,
    PldSequenceRequest,
    PldRheedDeflectionRequest,
    PldRheedFocusRequest,
)
from lumi.pascal.pld import convert
from lumi.pascal.pld.activity import ON_STATE, ActivityLog
from lumi.pascal.pld.client import PldClient, PldUnavailable
from lumi.pascal.pld.monitor import GrowthLog, PldEventMonitor, PldStateMonitor
from lumi.pascal.pld.proto import pld_pb2 as pb

log = logging.getLogger(__name__)


def _position(r) -> pb.PositionRequest:
    return pb.PositionRequest(position=r.position, wait=r.wait, speed_mm_s=r.speed_mm_s,
                              accel_mm_s_per_ms=r.accel_mm_s_per_ms)


def _op(rpc: str, build):
    """A handler method for one operation: build the request, call, answer {ok, message}."""

    async def method(self: "PldHandler", req) -> PldReply:
        return await self._operate(rpc, build(req))

    method.__name__ = rpc
    return method


class PldHandler:
    def __init__(self, client: PldClient, state_monitor: PldStateMonitor,
                 event_monitor: PldEventMonitor, growth_log: GrowthLog,
                 activity: ActivityLog | None = None) -> None:
        self.client = client
        self.state_monitor = state_monitor
        self.event_monitor = event_monitor
        self.growth_log = growth_log
        # MqServer journals every op the contract marks journal=True into this, with
        # the request's actor and source: who did what to the chamber.
        self.journal = activity if activity is not None else ActivityLog()
        # The bypass speed this node last set, and when (Unix s): the board boots at its
        # default speed, so a move after a board restart sends it again (see _bypass_move).
        self._bypass_rpm: tuple[float, float] | None = None

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
        return self._with_activity(convert.chamber_state(state))

    def _with_activity(self, state: PldChamberState) -> PldChamberState:
        state.activity = self.journal.recent(ON_STATE)
        return state

    async def recent_activity(self, req: PldActivityQuery) -> PldActivityList:
        return PldActivityList(entries=self.journal.recent(req.limit, req.since_seq),
                               latest_seq=self.journal.latest_seq)

    async def recent_events(self, req: PldEventQuery) -> PldEventList:
        events = list(self.event_monitor.events)[-req.limit:]
        return PldEventList(events=[convert.event(e) for e in events])

    async def next(self) -> PldChamberState | None:
        state = self.state_monitor.take_new()
        return self._with_activity(convert.chamber_state(state)) if state is not None else None

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
    set_mask_position = _op("SetMaskPosition", _position)
    set_sample_position = _op("SetSamplePosition", _position)
    set_rheed_gun_x = _op("SetRheedGunX", _position)
    home_axis = _op("HomeAxis", lambda r: pb.AxisRequest(axis=r.axis, wait=r.wait))
    set_target_spin = _op("SetTargetSpin", lambda r: pb.SwitchRequest(on=r.on))
    set_target_twist = _op("SetTargetTwist", lambda r: pb.SwitchRequest(on=r.on))
    set_motor_free = _op("SetMotorFree", lambda r: pb.SwitchRequest(on=r.on))
    # Needs no control, like StopFire; the reset does.
    stop_motors = _op("StopMotors", lambda r: pb.Empty())
    reset_motors = _op("ResetMotors", lambda r: pb.Empty())
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

    fire_laser = _op("FireLaser", lambda r: pb.LaserRequest(pulses=r.pulses, rate_hz=r.rate_hz, wait=r.wait))

    # Needs no control, and runs alongside a waited fire_laser (each op is its own thread).
    stop_fire = _op("StopFire", lambda r: pb.Empty())

    # The RHEED controller. pld-service ramps the filament (30 mA/s) and HV (500 V/s) to a
    # set point given, and down on switch-off, keeping the remembered set points; on with
    # no value holds at the 507 mA floor / 0 kV (REA 0089410). Its off-ops reply as the
    # ramp starts; the ones here wait until the gun is off. Focus and X/Y it sets in one
    # step, so the node ramps those in small steps itself. rheed_all_off_gentle is the
    # normal all-off; rheed_all_off stays the immediate stop.

    #: Longest ramp-down: HV from 29 kV at 500 V/s, then the filament 2200 -> 507 mA at
    #: 30 mA/s, about a minute each.
    _RHEED_OFF_S = 150.0
    _STEP_S = 0.2
    _FOCUS_MA_PER_S = 50.0
    _DEFLECTION_MA_PER_S = 20.0

    async def _rheed(self) -> "pb.Rheed":
        return (await asyncio.to_thread(self.client.state)).rheed

    async def _rheed_off(self, rpc: str, off) -> PldReply:
        reply = await self._operate(rpc, pb.Empty())
        if not reply.ok:
            return reply
        deadline = asyncio.get_running_loop().time() + self._RHEED_OFF_S
        ramping = False  # the service starts the ramp on its next cycle, not in the reply
        while True:
            rheed = await self._rheed()
            if off(rheed):
                return reply
            if rheed.hv_ramping_down or rheed.filament_ramping_down or rheed.all_ramping_down:
                ramping = True
            elif ramping:
                # The ramp ended without getting there: a new on or set point cancelled it.
                return PldReply(ok=False, message=f"{rpc}: the ramp down was cancelled before the gun was off")
            if asyncio.get_running_loop().time() > deadline:
                return PldReply(ok=False, message=f"{rpc}: still ramping down after {self._RHEED_OFF_S:.0f}s")
            await asyncio.sleep(self._STEP_S)

    async def rheed_hv_off(self, req: Empty) -> PldReply:
        return await self._rheed_off("RheedHvOff", lambda r: not r.hv_on)

    async def rheed_filament_off(self, req: Empty) -> PldReply:
        return await self._rheed_off("RheedFilamentOff", lambda r: not (r.filament_on or r.hv_on))

    # `ma` / `kv` None: on at the floor / 0 kV, held there until a set point.
    rheed_filament_on = _op("RheedFilamentOn", lambda r: pb.RheedFilamentOnRequest(
        **({} if r.ma is None else {"ma": r.ma})))
    rheed_hv_on = _op("RheedHvOn", lambda r: pb.RheedHvOnRequest(**({} if r.kv is None else {"kv": r.kv})))
    set_rheed_filament = _op("SetRheedFilament", lambda r: pb.RheedFilamentRequest(ma=r.ma))
    set_rheed_hv = _op("SetRheedHv", lambda r: pb.RheedHvRequest(kv=r.kv))

    @staticmethod
    def _rheed_goal_refused(rpc: str, goals: list[tuple[str, float, float, float]]) -> PldReply | None:
        """The goal checked against pld-service's limits before the first step: it refuses
        only the step that crosses one, after the earlier steps were applied."""
        for name, value, lo, hi in goals:
            if not math.isfinite(value):
                return PldReply(ok=False, message=f"{rpc}: {name} {value} mA is not a number")
            if (lo or hi) and not lo <= value <= hi:
                return PldReply(ok=False, message=f"{rpc}: {name} {value:g} mA outside {lo:g}..{hi:g} mA; nothing changed")
        return None

    async def _step_rheed(self, start: tuple[float, ...], goal: tuple[float, ...], rate: float, send) -> PldReply:
        """Walk from `start` to `goal` together, every value at most `rate` mA/s."""
        steps = max(1, math.ceil(max(abs(g - s) for s, g in zip(start, goal)) / (rate * self._STEP_S)))
        reply = PldReply(ok=True, message="nothing to change")
        for i in range(1, steps + 1):
            reply = await send(*(s + (g - s) * i / steps for s, g in zip(start, goal)))
            if not reply.ok:
                return reply
            if i < steps:
                await asyncio.sleep(self._STEP_S)
        return reply

    async def set_rheed_focus(self, req: PldRheedFocusRequest) -> PldReply:
        rheed = await self._rheed()
        refused = self._rheed_goal_refused(
            "SetRheedFocus", [("focus", req.ma, rheed.focus_min_ma, rheed.focus_max_ma)])
        if refused is not None:
            return refused
        start = rheed.focus_set_ma if rheed.focus_on and math.isfinite(rheed.focus_set_ma) else 0.0
        return await self._step_rheed(
            (start,), (req.ma,), self._FOCUS_MA_PER_S,
            lambda ma: self._operate("SetRheedFocus", pb.RheedFocusRequest(ma=ma)))

    async def set_rheed_deflection(self, req: PldRheedDeflectionRequest) -> PldReply:
        rheed = await self._rheed()

        def present(on: bool, ma: float) -> float:
            return ma if on and math.isfinite(ma) else 0.0

        refused = self._rheed_goal_refused("SetRheedDeflection", [
            ("X", req.x_ma, rheed.x_min_ma, rheed.x_max_ma), ("Y", req.y_ma, rheed.y_min_ma, rheed.y_max_ma)])
        if refused is not None:
            return refused
        start = (present(rheed.x_on, rheed.x_set_ma), present(rheed.y_on, rheed.y_set_ma))
        return await self._step_rheed(
            start, (req.x_ma, req.y_ma), self._DEFLECTION_MA_PER_S,
            lambda x, y: self._operate("SetRheedDeflection", pb.RheedDeflectionRequest(x_ma=x, y_ma=y)))
    # Needs no control, like StopFire.
    rheed_all_off = _op("RheedAllOff", lambda r: pb.Empty())

    # Needs no control either. HV and filament ramp down and off as in rheed_filament_off;
    # focus, X and Y stay on (pld-service 6c26a5a), so it waits for those two only.
    async def rheed_all_off_gentle(self, req: Empty) -> PldReply:
        return await self._rheed_off("RheedAllOffGentle", lambda r: not (
            r.filament_on or r.hv_on or r.all_ramping_down))

    # --- bypass-valve stepper (REA 98af5c6) ---------------------------------------

    async def _bypass_move(self, rpc: str, request) -> PldReply:
        """A bypass move, after setting the speed again if the board restarted since this
        node set it: pld-service reads the boot speed back but does not re-send ours."""
        resent = ""
        if self._bypass_rpm is not None:
            rpm, since = self._bypass_rpm
            restarted = any(e.kind == "bypass" and e.message.startswith("bypass board (re)started")
                            and e.time > since for e in list(self.event_monitor.events))
            latest = self.state_monitor.latest
            if restarted and latest is not None and latest.bypass.rpm != rpm:
                reply = await self._operate("BypassSetSpeed", pb.BypassRequest(value=rpm))
                if not reply.ok:
                    return PldReply(ok=False, message=(
                        f"{rpc} not sent: the board restarted and setting {rpm:g} rpm again failed: {reply.message}"))
                resent = f" (speed set to {rpm:g} rpm again after a board restart)"
            self._bypass_rpm = (rpm, time.time())
        reply = await self._operate(rpc, request)
        return PldReply(ok=reply.ok, message=reply.message + resent) if resent else reply

    async def bypass_home(self, req: PldBypassMoveRequest) -> PldReply:
        return await self._bypass_move("BypassHome", pb.BypassRequest(wait=req.wait))

    async def bypass_open_fully(self, req: PldBypassMoveRequest) -> PldReply:
        return await self._bypass_move("BypassOpenFully", pb.BypassRequest(wait=req.wait))

    async def bypass_move_to(self, req: PldBypassStepsRequest) -> PldReply:
        return await self._bypass_move("BypassMoveTo", pb.BypassRequest(value=req.steps, wait=req.wait))

    async def bypass_move_by(self, req: PldBypassStepsRequest) -> PldReply:
        return await self._bypass_move("BypassMoveBy", pb.BypassRequest(value=req.steps, wait=req.wait))

    async def bypass_set_speed(self, req: PldBypassSpeedRequest) -> PldReply:
        reply = await self._operate("BypassSetSpeed", pb.BypassRequest(value=req.rpm))
        if reply.ok:
            self._bypass_rpm = (req.rpm, time.time())
        return reply

    bypass_release = _op("BypassRelease", lambda r: pb.Empty())
    # Needs no control, like StopMotors.
    bypass_stop = _op("BypassStop", lambda r: pb.Empty())

    # --- sequences (REA 38ca32e) --------------------------------------------------

    async def run_sequence(self, req: PldSequenceRequest) -> PldReply:
        try:
            request = json_format.ParseDict(
                {"name": req.name, "steps": req.steps, "wait": req.wait}, pb.SequenceRequest())
        except json_format.ParseError as e:
            return PldReply(ok=False, message=f"RunSequence not sent: {e}")
        return await self._operate("RunSequence", request)

    # Pause and cancel need no control, like StopFire; resume does.
    pause_sequence = _op("PauseSequence", lambda r: pb.Empty())
    resume_sequence = _op("ResumeSequence", lambda r: pb.Empty())
    cancel_sequence = _op("CancelSequence", lambda r: pb.Empty())

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

