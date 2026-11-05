"""Driving the chamber through pld-service: the experiment node's default driver.

`ExperimentManager` was written against MI mode, so it speaks in `lumi.pascal.command`
objects -- `pcmd.SelectTarget("A", nowait=False)` -- and hands each one to
`self.chamber_mi.execute(...)`. `PldCommandRunner` has that same `execute`, and runs
each command as the matching `chamber.pld` operation instead of as an MI script. So
every recipe, interlock and MCP tool above it is unchanged, and `--use-legacy-mi-mode`
swaps `MiCommandRunner` back in.

What the translation has to know:

- `(Nowait)` becomes `wait=false`. Otherwise the service blocks until the chamber gets
  there, as MI mode did.
- MI's `Temperature Ramp` and `Temperature Set` are separate commands, and a setpoint
  given with PID off is only stored -- `to_temperature` sets the target, then the ramp,
  then engages PID, so the controller ramps from where the substrate is. pld-service's
  SetTemperature takes target and ramp together and engages PID itself. So both are
  held here: a `TemperatureSet` with PID off is only remembered, and engaging PID sends
  the remembered target with the held ramp. Sending it straight away instead would
  start PID with no ramp -- a setpoint jump.
- Target rotation/twist `AUTO` is the service's auto motion (on with each fire, off
  after); `ON`/`OFF` switch it directly.
- `Rotate Sample` is relative; the service only takes absolute angles, so the current
  angle is read first.
- The service has no MFC master gate (`MFC Control`). Disabling it zeroes MFC1;
  enabling it is a no-op. MFC2 is not used in this chamber.
- `Data Logging` / `Log Interval` drive the growth log the chamber node writes from the
  service's state (`chamber.pld.start_logging`), since PLD Controller is not running.

A refusal (`ok=false`) raises `PldRefused` with the service's reason. The service
enforces every safety rule; this only surfaces its answer.
"""

from __future__ import annotations

import asyncio
import logging

from lumi.base.mq.client import RemoteError
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldAutoMotionRequest,
    PldCurrentRequest,
    PldFireRequest,
    PldFlowRequest,
    PldLoggingRequest,
    PldPositionRequest,
    PldPressureRequest,
    PldReply,
    PldShutterRequest,
    PldSwitchRequest,
    PldTargetRequest,
    PldTemperatureRequest,
)
from lumi.generated.clients.chamber import ChamberPldClient
from lumi.pascal import command as pcmd

log = logging.getLogger(__name__)


class PldRefused(RuntimeError):
    """pld-service refused an operation; the message is its reason."""


class PldCommandRunner:
    """`MiCommandRunner`'s `execute()`, over `chamber.pld`. See the module docstring."""

    def __init__(self, client: ChamberPldClient) -> None:
        self.client = client
        #: `Temperature Ramp`, held for the next `Temperature Set`; 0 = jump. MI's
        #: default ramp is on, at PASCAL's power-up rate.
        self._ramp_c_per_min = 0.0
        #: The last `Temperature Set`, sent when PID engages (see the module docstring).
        self._target_c: float | None = None
        #: Whether PID is on, as this runner last set it; None = ask the service. Kept
        #: because the service's state is its last poll, which can predate the
        #: operation that just answered.
        self._pid_on: bool | None = None
        self._auto_spin, self._auto_twist = True, False
        self._log_interval_s = 1.0

    async def start(self) -> None:
        """Nothing to subscribe to: every operation answers when it is done."""

    async def execute(self, command, *, timeout=None, raise_on_abort=None) -> PldReply:
        """Run one `lumi.pascal.command` object as its pld-service operation.

        `timeout` and `raise_on_abort` are accepted for `MiCommandRunner` parity and
        ignored: each op's wait is bounded by the contract (`Op.timeout_s`), and the
        service has no abort report -- an operation either answers ok or is refused.
        """
        handler = getattr(self, f"_do_{type(command).__name__}", None)
        if handler is None:
            text = command.to_text().strip() if hasattr(command, "to_text") else str(command)
            raise NotImplementedError(
                f"{text!r} has no pld-service equivalent. Run the chamber and experiment "
                "nodes with --use-legacy-mi-mode to send raw MI scripts"
            )
        return await handler(command)

    async def _call(self, op: str, req=None) -> PldReply:
        try:
            reply = await self.client.call(op, req if req is not None else Empty())
        except RemoteError as e:
            if "--use-legacy-mi-mode" in e.error_message:
                raise RuntimeError(
                    f"{e.error_message}. The experiment node defaults to the pld driver; start "
                    "it with --use-legacy-mi-mode too, or the chamber node without it"
                ) from None
            raise
        if not reply.ok:
            raise PldRefused(f"pld-service refused {op}: {reply.message}")
        if reply.message and reply.message not in ("done", "ok"):
            log.info("pld %s: %s", op, reply.message)
        return reply

    # --- status -------------------------------------------------------------------

    async def check(self) -> tuple[str, str]:
        """Whether the driver can operate the chamber, as `check_mi_mode`'s
        (status, message): `on`, `off`, or `busy` (another client holds control)."""
        try:
            state = await self.client.chamber_state(Empty())
        except RemoteError as e:
            return "off", f"chamber.pld did not answer: {e.error_message}"
        except TimeoutError:
            return "off", "the chamber node did not answer; is it running?"
        if not state.connected:
            return "off", "pld-service is up but not every chamber port answered its last poll"
        if state.read_only:
            return "off", ("pld-service is running on hardware read-only: every operation is "
                           "refused. A person has to restart it with --allow-operations")
        mine = self.client_name
        if state.controller and state.controller != mine:
            return "busy", (f"{state.controller!r} holds control of pld-service; operations are "
                            "refused until it releases (taking it by force is acquire_control "
                            "force=true, and is logged)")
        backend = state.backend or "unknown backend"
        return "on", f"pld-service ({backend}) is connected and taking operations"

    #: The service sees this node's operations as the chamber node's client.
    client_name = "lumi-pascal"

    # --- motion -------------------------------------------------------------------

    async def _do_SelectTarget(self, c: pcmd.SelectTarget) -> PldReply:
        return await self._call("select_target", PldTargetRequest(target=str(c.target), wait=not c.nowait))

    async def _do_TargetRotationMode(self, c: pcmd.TargetRotationMode) -> PldReply:
        return await self._spin_or_twist("spin", str(c.mode).upper())

    async def _do_TargetTwistMode(self, c: pcmd.TargetTwistMode) -> PldReply:
        return await self._spin_or_twist("twist", str(c.mode).upper())

    async def _spin_or_twist(self, which: str, mode: str) -> PldReply:
        auto = mode == "AUTO"
        if which == "spin":
            self._auto_spin = auto
        else:
            self._auto_twist = auto
        reply = await self._call("set_target_auto_motion",
                                 PldAutoMotionRequest(spin=self._auto_spin, twist=self._auto_twist))
        if not auto:
            reply = await self._call(f"set_target_{which}", PldSwitchRequest(on=mode == "ON"))
        return reply

    async def _do_SetMaskPosition(self, c: pcmd.SetMaskPosition) -> PldReply:
        if int(c.mask_id) != 1:
            raise PldRefused(f"pld-service drives Mask1 only, not M{c.mask_id}")
        return await self._call("set_mask_position", PldPositionRequest(position=c.distance, wait=not c.nowait))

    async def _do_SetSamplePosition(self, c: pcmd.SetSamplePosition) -> PldReply:
        return await self._call("set_sample_position",
                                PldPositionRequest(position=float(c.position) % 360.0, wait=not c.nowait))

    async def _do_RotateSample(self, c: pcmd.RotateSample) -> PldReply:
        state = await self.client.chamber_state(Empty())
        if state.motion.sample_angle is None:
            raise PldRefused("pld-service reports no sample angle to rotate from")
        target = (state.motion.sample_angle + float(c.angle)) % 360.0
        return await self._call("set_sample_position", PldPositionRequest(position=target, wait=not c.nowait))

    async def _do_SetRHEEDGunX(self, c: pcmd.SetRHEEDGunX) -> PldReply:
        return await self._call("set_rheed_gun_x", PldPositionRequest(position=c.position, wait=True))

    # --- heater -------------------------------------------------------------------

    async def _do_HeatingLaserLock(self, c: pcmd.HeatingLaserLock) -> PldReply:
        return await self._call("set_heater_key", PldSwitchRequest(on=not bool(c.locked)))

    async def _do_HeatingLaser(self, c: pcmd.HeatingLaser) -> PldReply:
        return await self._call("set_heater_start", PldSwitchRequest(on=bool(c.state)))

    async def _do_HeatingLaserThreshold(self, c: pcmd.HeatingLaserThreshold) -> PldReply:
        return await self._call("set_heater_threshold", PldSwitchRequest(on=bool(c.state)))

    async def _do_SetHeatingCurrent(self, c: pcmd.SetHeatingCurrent) -> PldReply:
        reply = await self._call("set_heating_current", PldCurrentRequest(amps=float(c.current)))
        self._pid_on = False  # the service switches PID off for a manual current
        return reply

    async def _do_TemperatureControl(self, c: pcmd.TemperatureControl) -> PldReply:
        mode = getattr(c.mode, "value", c.mode)
        pid = str(mode).upper() == "PID"
        reply = await self._call("set_temperature_control", PldSwitchRequest(on=pid))
        self._pid_on = pid
        # Used once, and never carried across a MANUAL: `initiate_heating_laser` engages
        # PID with no new target, and must hold the present temperature, not a
        # setpoint left over from an earlier run.
        target, self._target_c = self._target_c, None
        if pid and target is not None:
            # PID now holds the present temperature; send it the remembered target at
            # the held ramp, so it ramps from here. Not waited on: a following
            # TemperatureSet without (Nowait) is what waits for arrival.
            reply = await self._call("set_temperature", PldTemperatureRequest(
                celsius=target, ramp_c_per_min=self._ramp_c_per_min, wait=False))
        return reply

    async def _do_TemperatureRamp(self, c: pcmd.TemperatureRamp) -> PldReply:
        # Held, not sent: the service takes the ramp with the setpoint.
        self._ramp_c_per_min = float(c.ramp_rate) if bool(c.state) else 0.0
        return PldReply(ok=True, message=f"ramp {self._ramp_c_per_min:g} C/min for the next setpoint")

    async def _do_TemperatureSet(self, c: pcmd.TemperatureSet) -> PldReply:
        if self._pid_on is None:
            self._pid_on = (await self.client.chamber_state(Empty())).heater.pid_on
        if not self._pid_on:
            self._target_c = float(c.temperature)
            # As on PASCAL, a setpoint with PID off is only stored; engaging PID uses it.
            # There is nothing to wait for -- no controller is driving towards it.
            return PldReply(ok=True, message=f"{self._target_c:g} C stored; PID is off, so it applies when PID engages")
        return await self._call("set_temperature", PldTemperatureRequest(
            celsius=float(c.temperature), ramp_c_per_min=self._ramp_c_per_min, wait=not c.nowait))

    async def _do_SampleShutter(self, c: pcmd.SampleShutter) -> PldReply:
        # MI's Sample Shutter ON opens it (away from the sample).
        return await self._call("set_sample_shutter", PldShutterRequest(in_front=not bool(c.state)))

    # --- laser --------------------------------------------------------------------

    async def _do_TriggerLaser(self, c: pcmd.TriggerLaser) -> PldReply:
        return await self._call("fire_laser", PldFireRequest(
            pulses=int(c.num_pulse), rate_hz=float(c.frequency), wait=not c.nowait))

    # --- gas ----------------------------------------------------------------------

    async def _do_SetMFC1Flow(self, c: pcmd.SetMFC1Flow) -> PldReply:
        return await self._call("set_mfc_flow", PldFlowRequest(mfc=1, sccm=float(c.MFC_flow)))

    async def _do_SetMFC2Flow(self, c: pcmd.SetMFC2Flow) -> PldReply:
        raise PldRefused("MFC2 is not used in this chamber (pld-service drives MFC1 only)")

    async def _do_SetMFCControl(self, c: pcmd.SetMFCControl) -> PldReply:
        if bool(c.enable):
            return PldReply(ok=True, message="pld-service has no MFC master gate; nothing to enable")
        return await self._call("set_mfc_flow", PldFlowRequest(mfc=1, sccm=0.0))

    async def _do_SetPressure(self, c: pcmd.SetPressure) -> PldReply:
        return await self._call("set_pressure", PldPressureRequest(torr=float(c.pressure)))

    async def _do_PressureControl(self, c: pcmd.PressureControl) -> PldReply:
        return await self._call("set_pressure_control", PldSwitchRequest(on=bool(c.state)))

    # --- the growth log -----------------------------------------------------------

    async def _do_SetLogInterval(self, c: pcmd.SetLogInterval) -> PldReply:
        # Applied with the next Data Logging, as on PASCAL.
        self._log_interval_s = float(c.time)
        return PldReply(ok=True, message=f"log interval {c.time}s for the next log file")

    async def _do_DataLogging(self, c: pcmd.DataLogging) -> PldReply:
        if bool(c.state):
            return await self._call("start_logging", PldLoggingRequest(
                file_name=c.file_name or None, interval_s=self._log_interval_s))
        return await self._call("stop_logging")

    # --- the MI probe -------------------------------------------------------------

    async def _do_Wait(self, c: pcmd.Wait) -> PldReply:
        await asyncio.sleep(float(c.time))
        return PldReply(ok=True, message=f"waited {c.time}s")
