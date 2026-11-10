"""The chamber, as the experiment node drives it: one backend per driver.

`ExperimentManager` says what it wants done -- "select target A", "ramp to 650 C at
20 C/min from where the substrate is", "fire 300 pulses at 5 Hz" -- and reads what the
chamber reports, in PLD Controller's log columns. A backend does both for one driver:

- `PldChamber` (the default): pld-service through `chamber.pld`. Each action is the
  service operation that does it, and the readings are the service's live state.
- `MiChamber` (`--use-legacy-mi-mode`): PASCAL's MI scripts through `chamber.mi_mode`,
  and the readings are the newest row of PASCAL's chamber log -- what the manager did
  before there was a choice, command for command.

The readings come back keyed and decoded as `lumi.pascal.chamber_log.process_row` leaves
a log row ("TG No.", "HT Temp moni", "Motor free", "MV10 (Main)", ...), so the manager's
interlocks read the same fields whichever driver is behind them.

Under pld-service a refusal (`ok=false`) raises `PldRefused` with the service's reason,
and a fire cut short by StopFire raises `PldStopped`: an abort, never re-fired. The
service enforces every chamber safety rule itself; this only surfaces its answer.
"""

from __future__ import annotations

import logging
from typing import Protocol

from lumi.base.mq.client import RemoteError
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldAutoMotionRequest,
    PldChamberState,
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
from lumi.experiment.mi import MiCommandRunner, brief as mi_brief
from lumi.generated.clients.chamber import ChamberLogClient, ChamberPldClient
from lumi.pascal import command as pcmd
from lumi.pascal.log_fields import PARSE_DICT
from lumi.pascal.pld.columns import values_from_state

log = logging.getLogger(__name__)

#: The client name the chamber node uses with pld-service (`lumi.pascal.pld.client`).
PLD_CLIENT_NAME = "lumi-pascal"


class PldRefused(RuntimeError):
    """pld-service refused an operation; the message is its reason."""


class PldStopped(PldRefused):
    """A fire was stopped by StopFire -- a person (the REA GUI, or any client) ended the
    deposition. An abort: the task fails and the run goes no further. Never re-fired."""

    #: How pld-service starts a FireLaser reply cut short by StopFire.
    PREFIX = "stopped by request"


class ChamberBackend(Protocol):
    """What `ExperimentManager` needs of the chamber. Every action returns once the
    chamber has done it (a move arrived, a train fired) unless it says otherwise."""

    driver: str  # "pld" or "legacy-mi"

    async def readings(self) -> dict: ...
    async def check(self, timeout: float = 10.0) -> tuple[str, str]: ...

    async def select_target(self, target: str) -> None: ...
    async def target_auto_motion(self) -> None: ...
    async def move_mask(self, position: float) -> None: ...
    async def move_rheed_gun(self, position: float) -> None: ...
    async def rotate_sample_to(self, angle: float) -> None: ...
    async def rotate_sample_by(self, delta: float) -> None: ...
    async def sample_shutter(self, open_: bool) -> None: ...
    async def fire(self, pulses: int, rate_hz: float) -> None: ...
    async def stop_fire(self) -> str: ...

    async def heater_on(self) -> None: ...
    async def heater_off(self) -> None: ...
    async def manual_current(self) -> None: ...
    async def set_heating_current(self, amps: float) -> None: ...
    async def ramp_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None: ...
    async def coast_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None: ...

    async def set_mfc_flow(self, mfc: int, sccm: float) -> None: ...
    async def set_mfc_control(self, enabled: bool) -> None: ...
    async def set_pressure(self, torr: float) -> None: ...
    async def set_pressure_control(self, on: bool) -> None: ...

    async def start_logging(self, file_name: str, interval_s: int) -> None: ...
    async def stop_logging(self) -> None: ...


# --- pld-service ------------------------------------------------------------------


def readings_from_state(state: PldChamberState) -> dict:
    """pld-service's state as a decoded log row: the columns `values_from_state` writes
    to the growth log, plus each status word's named bits, as `process_row` adds them."""
    values = values_from_state(state)
    row = dict(values)
    for column, bits in PARSE_DICT.items():
        word = int(values.get(column, 0) or 0)
        row.update({name: bool(word >> bit & 1) for bit, name in bits.items()})
    row["time"] = state.time
    return row


class PldChamber:
    """The chamber through `chamber.pld` -- see the module docstring."""

    driver = "pld"

    def __init__(self, client: ChamberPldClient, *, client_name: str = PLD_CLIENT_NAME) -> None:
        self.client = client
        self.client_name = client_name

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
            if reply.message.startswith(PldStopped.PREFIX):
                raise PldStopped(f"{op} {reply.message}; the run is aborted, not retried")
            raise PldRefused(f"pld-service refused {op}: {reply.message}")
        if reply.message and reply.message not in ("done", "ok"):
            log.info("pld %s: %s", op, reply.message)
        return reply

    async def state(self) -> PldChamberState:
        return await self.client.chamber_state()

    async def readings(self) -> dict:
        return readings_from_state(await self.state())

    async def check(self, timeout: float = 10.0) -> tuple[str, str]:
        """Whether the driver can operate the chamber: `on`, `off`, or `busy` (another
        client holds control)."""
        try:
            state = await self.state()
        except RemoteError as e:
            return "off", f"chamber.pld did not answer: {e.error_message}"
        except TimeoutError:
            return "off", "the chamber node did not answer; is it running?"
        if not state.connected:
            return "off", "pld-service is up but not every chamber port answered its last poll"
        if state.read_only:
            return "off", ("pld-service is running on hardware read-only: every operation is "
                           "refused. A person has to restart it with --allow-operations")
        if state.controller and state.controller != self.client_name:
            return "busy", (f"{state.controller!r} holds control of pld-service; operations are "
                            "refused until it releases (taking it by force is acquire_control "
                            "force=true, a person's call)")
        return "on", f"pld-service ({state.backend}) is connected and taking operations"

    # --- motion -------------------------------------------------------------------

    async def select_target(self, target: str) -> None:
        # `pcmd.Targets.Clear` str()s as "Targets.Clear"; the service wants "Clear".
        await self._call("select_target", PldTargetRequest(target=str(getattr(target, "value", target)), wait=True))

    async def target_auto_motion(self) -> None:
        await self._call("set_target_auto_motion", PldAutoMotionRequest(spin=True, twist=True))

    async def move_mask(self, position: float) -> None:
        await self._call("set_mask_position", PldPositionRequest(position=float(position), wait=True))

    async def move_rheed_gun(self, position: float) -> None:
        await self._call("set_rheed_gun_x", PldPositionRequest(position=float(position), wait=True))

    async def rotate_sample_to(self, angle: float) -> None:
        await self._call("set_sample_position", PldPositionRequest(position=float(angle) % 360.0, wait=True))

    async def rotate_sample_by(self, delta: float) -> None:
        # The service takes absolute angles only.
        angle = (await self.state()).motion.sample_angle
        if angle is None:
            raise PldRefused("pld-service reports no sample angle to rotate from")
        await self.rotate_sample_to(angle + float(delta))

    async def sample_shutter(self, open_: bool) -> None:
        await self._call("set_sample_shutter", PldShutterRequest(in_front=not open_))

    async def fire(self, pulses: int, rate_hz: float) -> None:
        await self._call("fire_laser", PldFireRequest(pulses=int(pulses), rate_hz=float(rate_hz), wait=True))

    async def stop_fire(self) -> str:
        return (await self._call("stop_fire")).message

    # --- heater -------------------------------------------------------------------

    async def heater_on(self) -> None:
        # MI's Heating Laser Lock ACTIVE / Heating Laser ON / Threshold ON / PID, in order.
        for op in ("set_heater_key", "set_heater_start", "set_heater_threshold", "set_temperature_control"):
            await self._call(op, PldSwitchRequest(on=True))

    async def heater_off(self) -> None:
        for op in ("set_temperature_control", "set_heater_threshold", "set_heater_start", "set_heater_key"):
            await self._call(op, PldSwitchRequest(on=False))

    async def manual_current(self) -> None:
        await self._call("set_temperature_control", PldSwitchRequest(on=False))

    async def set_heating_current(self, amps: float) -> None:
        await self._call("set_heating_current", PldCurrentRequest(amps=float(amps)))

    async def ramp_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None:
        # PID first: engaged, it holds the present temperature; the setpoint then ramps
        # from there. SetTemperature with `wait` returns once the substrate arrives.
        await self._call("set_temperature_control", PldSwitchRequest(on=True))
        await self._call("set_temperature", PldTemperatureRequest(
            celsius=float(celsius), ramp_c_per_min=float(ramp_c_per_min), wait=wait))

    async def coast_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None:
        # pld-service has one setpoint operation; below its range (about 140 C) it
        # refuses, and that refusal is the answer.
        await self._call("set_temperature", PldTemperatureRequest(
            celsius=float(celsius), ramp_c_per_min=float(ramp_c_per_min), wait=wait))

    # --- gas ----------------------------------------------------------------------

    async def set_mfc_flow(self, mfc: int, sccm: float) -> None:
        if int(mfc) != 1:
            raise PldRefused(f"MFC{mfc} is not used in this chamber (pld-service drives MFC1 only)")
        await self._call("set_mfc_flow", PldFlowRequest(mfc=1, sccm=float(sccm)))

    async def set_mfc_control(self, enabled: bool) -> None:
        # The service has no MFC master gate: closing it is MFC1 to zero, opening it nothing.
        if not enabled:
            await self._call("set_mfc_flow", PldFlowRequest(mfc=1, sccm=0.0))

    async def set_pressure(self, torr: float) -> None:
        await self._call("set_pressure", PldPressureRequest(torr=float(torr)))

    async def set_pressure_control(self, on: bool) -> None:
        await self._call("set_pressure_control", PldSwitchRequest(on=bool(on)))

    # --- the growth log -----------------------------------------------------------

    async def start_logging(self, file_name: str, interval_s: int) -> None:
        await self._call("start_logging", PldLoggingRequest(file_name=file_name, interval_s=float(interval_s)))

    async def stop_logging(self) -> None:
        await self._call("stop_logging")


# --- legacy: MI mode --------------------------------------------------------------


class MiChamber:
    """The chamber through PASCAL's MI mode and its chamber log -- see the module
    docstring. Each action is the MI script the manager issued before pld-service."""

    driver = "legacy-mi"

    def __init__(self, runner: MiCommandRunner, chamber_log: ChamberLogClient) -> None:
        self.runner = runner
        self.chamber_log = chamber_log

    async def _run(self, *commands) -> None:
        for command in commands:
            await self.runner.execute(command)

    async def readings(self) -> dict:
        batch = await self.chamber_log.log()
        if not batch.entries:
            raise RuntimeError("chamber log has no rows yet")
        return batch.entries[-1].values

    async def check(self, timeout: float = 10.0) -> tuple[str, str]:
        """Whether PASCAL is taking MI commands, as (status, message).

        Nothing in the chamber log says whether MI Mode is on: with it off, PASCAL never
        picks a script up and every command just hangs until it times out. So send one
        that does nothing (`Wait 0 sec`) and see whether it completes. "busy" when
        another script is running -- the probe would only queue behind it.
        """
        running = [
            e for e in (await self.runner.client.list_execution()).executions
            if not e.is_execution_finished
        ]
        probe = pcmd.Wait(0).to_text()
        if running and running[0].commands == probe:
            # An earlier probe that PASCAL never picked up. It holds the queue, so a new
            # one would only sit behind it -- and it still being there is the answer.
            return "off", (
                "an earlier MI Mode probe is still waiting for PASCAL to pick it up: MI "
                "Mode is still off. Ask a person to switch PASCAL to MI Mode"
            )
        if running:
            return "busy", (
                f"another MI script is running ({mi_brief(running[0].commands)}); the probe "
                "would queue behind it, so MI Mode cannot be checked until it finishes"
            )
        try:
            await self.runner.execute(probe, timeout=timeout, raise_on_abort=False)
        except TimeoutError:
            return "off", (
                f"PASCAL did not run a no-op script within {timeout:.0f}s: MI Mode is "
                "probably off. Ask a person to switch PASCAL to MI Mode. The probe stays "
                "queued and runs harmlessly once it is on"
            )
        return "on", "PASCAL ran a no-op script: MI Mode is on"

    # --- motion -------------------------------------------------------------------

    async def select_target(self, target: str) -> None:
        await self._run(pcmd.SelectTarget(target, nowait=False))

    async def target_auto_motion(self) -> None:
        await self._run(pcmd.TargetRotationMode(mode=pcmd.TargetRotationModeType.AUTO),
                        pcmd.TargetTwistMode(mode=pcmd.TargetTwistModeType.AUTO))

    async def move_mask(self, position: float) -> None:
        await self._run(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=position, sync=False, nowait=False))

    async def move_rheed_gun(self, position: float) -> None:
        await self._run(pcmd.SetRHEEDGunX(position))

    async def rotate_sample_to(self, angle: float) -> None:
        await self._run(pcmd.SetSamplePosition(angle, sync=False, nowait=False))

    async def rotate_sample_by(self, delta: float) -> None:
        await self._run(pcmd.RotateSample(delta, sync=False, nowait=False))

    async def sample_shutter(self, open_: bool) -> None:
        await self._run(pcmd.SampleShutter(pcmd.PascalState("ON" if open_ else "OFF")))

    async def fire(self, pulses: int, rate_hz: float) -> None:
        await self._run(pcmd.TriggerLaser(num_pulse=pulses, frequency=rate_hz, sync=False, nowait=False))

    async def stop_fire(self) -> str:
        raise NotImplementedError(
            "MI mode has no stop for a running train; use PASCAL's own STOP. pld-service "
            "(the default driver) has stop_fire")

    # --- heater -------------------------------------------------------------------

    async def heater_on(self) -> None:
        await self._run(
            pcmd.HeatingLaserLock(locked=False, nowait=False),
            pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
            pcmd.HeatingLaserThreshold(state=pcmd.PascalState("ON")),
            pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        )

    async def heater_off(self) -> None:
        await self._run(
            pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL),
            pcmd.HeatingLaserThreshold(state=pcmd.PascalState("OFF")),
            pcmd.HeatingLaser(state=pcmd.PascalState("OFF"), nowait=False),
            pcmd.HeatingLaserLock(locked=True, nowait=False),
        )

    async def manual_current(self) -> None:
        await self._run(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL))

    async def set_heating_current(self, amps: float) -> None:
        await self._run(pcmd.SetHeatingCurrent(current=float(amps)))

    async def ramp_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None:
        # Target first, then the ramp rate, then PID on: the controller then ramps from
        # where the substrate is. Engaged before the target, PID would chase whatever
        # setpoint it still holds -- after the manual warm-up, the cold 160. The same
        # target again without (Nowait) changes nothing but holds the script until the
        # substrate arrives.
        await self._run(
            pcmd.TemperatureSet(celsius, nowait=True),
            pcmd.TemperatureRamp(ramp_c_per_min, state=pcmd.PascalState("ON")),
            pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        )
        if wait:
            await self._run(pcmd.TemperatureSet(celsius, nowait=False))

    async def coast_to(self, celsius: float, ramp_c_per_min: float, wait: bool) -> None:
        await self._run(
            pcmd.TemperatureRamp(ramp_c_per_min, state=pcmd.PascalState("ON")),
            pcmd.TemperatureSet(celsius, nowait=not wait),
        )

    # --- gas ----------------------------------------------------------------------

    async def set_mfc_flow(self, mfc: int, sccm: float) -> None:
        await self._run(pcmd.SetMFC1Flow(sccm) if int(mfc) == 1 else pcmd.SetMFC2Flow(sccm))

    async def set_mfc_control(self, enabled: bool) -> None:
        await self._run(pcmd.SetMFCControl(enable=enabled))

    async def set_pressure(self, torr: float) -> None:
        await self._run(pcmd.SetPressure(torr))

    async def set_pressure_control(self, on: bool) -> None:
        await self._run(pcmd.PressureControl(state=pcmd.PascalState(on)))

    # --- the chamber log ----------------------------------------------------------

    async def start_logging(self, file_name: str, interval_s: int) -> None:
        await self._run(pcmd.SetLogInterval(interval_s), pcmd.DataLogging(file_name, pcmd.PascalState("ON")))

    async def stop_logging(self) -> None:
        await self._run(pcmd.DataLogging("", pcmd.PascalState("OFF")))


__all__ = ["ChamberBackend", "MiChamber", "PldChamber", "PldRefused", "PldStopped", "readings_from_state"]
