"""Canned experiment sequences, ported from manager.py's `perform_experiment`.

Deliberately client-side, not server ops: the experiment contract exposes granular
primitives (to_temperature, perform_deposition, start_storage, ...) precisely so a
notebook user can recombine them freely, and these functions are one example
combination rather than the only one -- copy one into a notebook cell and edit it
instead of treating it as fixed.

Two provider hooks replace the original's blocking `ainput`/`strong_ainput`:

- `input_provider(message)`: pause for a purely local confirmation ("flip the
  excimer laser to ON, then continue") that never touches server state -- these
  were never gated ops, just the notebook waiting on a person.
- `value_provider(prompt) -> str`: collect a value a human has to physically read
  (a laser power meter, "is this aligned?"). Used together with the server's
  begin_*/confirm_* gated ops, which are the actual state machine; this just
  supplies the human's answer to it.

Both default to real terminal input (safe under asyncio via run_in_executor) so a
notebook cell calling these functions with no extra arguments behaves like the
original. An MCP-driven agent supplies its own providers instead of blocking stdin.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable

from lumi.contracts.payloads.experiment import (
    BeginSetLaserPower,
    ConfirmLaserPower,
    ConfirmMaskCenter,
    EndStorage,
    FinishCurrentPixel,
    FinishExperimentRecord,
    PerformDeposition,
    PerformPreablation,
    SetRheedGain,
    StartStorage,
    ToTemperature,
)
from lumi.experiment.client import ExperimentSession

log = logging.getLogger(__name__)

InputProvider = Callable[[str], Awaitable[None]]
ValueProvider = Callable[[str], Awaitable[str]]


async def _blocking_input(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def default_input_provider(message: str) -> None:
    await _blocking_input(f"{message} Press Enter to continue... ")


async def default_value_provider(prompt: str) -> str:
    return await _blocking_input(prompt)


async def _ask_float(
    value_provider: ValueProvider,
    prompt: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Collect a number from a human, re-prompting on anything unparseable or out of
    range rather than letting `float()` raise straight through the recipe and drop
    the session.

    A person answering these prompts can fat-finger a value -- an empty line, a
    stray unit, a "y" typed in the wrong box. The complaint is folded into the next
    prompt string so this behaves the same for a terminal user and for an
    agent-supplied provider (which just sees the corrected prompt).
    """
    ask = prompt
    while True:
        raw = (await value_provider(ask) or "").strip()
        try:
            value = float(raw)
        except ValueError:
            ask = f"{raw!r} is not a number -- {prompt}" if raw else f"a value is required -- {prompt}"
            continue
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            lo = "-inf" if minimum is None else f"{minimum:g}"
            hi = "inf" if maximum is None else f"{maximum:g}"
            ask = f"{value:g} is outside [{lo}, {hi}] -- {prompt}"
            continue
        return value


async def _wait_for_task(exp: ExperimentSession, task_id: str, poll_interval: float = 0.5) -> dict | None:
    """Poll the server's authoritative state (not the locally cached subscription
    value) until the given task is no longer current -- avoids a race between the
    RPC reply carrying task_id and the update-channel push that sets current_task.

    Returns the task's outcome (`{"ok": ...}`) when the session saw one. Note that the
    functions below do not act on it: a failed ramp does not stop the sequence, which
    is a known gap -- see docs/TODO.md."""
    while True:
        state = await exp.driver.get_state()
        if state.current_task is None or state.current_task.id != task_id:
            return exp.last_task_result
        await asyncio.sleep(poll_interval)


async def run_check_mask_center(exp: ExperimentSession, value_provider: ValueProvider = default_value_provider) -> None:
    """Port of BaseExperimentManager.check_mask_center's loop."""
    await exp.driver.begin_check_mask_center()
    while True:
        answer = await value_provider("[Manual] Is the current mask center aligned on the camera? (y/n) ")
        aligned = answer.strip().lower() == "y"
        corrected = None
        if not aligned:
            corrected = await _ask_float(value_provider, "Enter a new center mask position: ")
        status = await exp.driver.confirm_mask_center(ConfirmMaskCenter(aligned=aligned, corrected_position=corrected))
        if status.pending is None:
            return


async def run_adjust_rheed_gain(exp: ExperimentSession, value_provider: ValueProvider = default_value_provider) -> None:
    """Port of BaseExperimentManager.adjust_rheed_gain's loop."""
    config = await exp.driver.begin_adjust_rheed_gain()  # Ack -- the current gain is in the pending message
    while True:
        gain = await _ask_float(value_provider, "Enter the gain (0-240): ", minimum=0, maximum=240)
        await exp.driver.set_rheed_gain(SetRheedGain(gain=gain))
        good = (await value_provider("Is the RHEED image good? (y/n) ")).strip().lower() == "y"
        if good:
            await exp.driver.confirm_rheed_gain()
            return


async def run_ensure_motor_ready(
    exp: ExperimentSession, input_provider: InputProvider = default_input_provider,
) -> None:
    """Pause until a person re-engages the motor holding lock.

    "Motor free" in the chamber log means the electromagnet lock is *released* -- the
    axes are back-driveable by hand and a commanded move drives nothing (a power cut
    is the usual cause). Re-engaging it is a physical button on the chamber
    controller, so ask for it here rather than letting the next mask move sit in
    `_await_motor_ready` until it times out. A no-op on a healthy chamber.
    """
    while (await exp.driver.is_motor_free()).free:
        await input_provider(
            "[Manual] The motor holding lock is released -- press MOTOR ENABLE on the "
            "chamber controller to re-engage it, then continue"
        )


async def run_set_laser_power(
    exp: ExperimentSession, laser_power: float, *, target_id: str | None = None, force: bool = False,
    value_provider: ValueProvider = default_value_provider,
) -> float:
    """Port of BaseExperimentManager.set_laser_power. Returns the measured power --
    the cached value immediately if already set to `laser_power` and not `force`d,
    otherwise the value the human reports off the meter."""
    await exp.driver.begin_set_laser_power(BeginSetLaserPower(laser_power=laser_power, target_id=target_id, force=force))
    state = await exp.driver.get_state()
    if state.pending_confirmation is None or state.pending_confirmation.kind != "laser_power":
        return state.laser_power_real  # begin_set_laser_power short-circuited: already set

    measured = await _ask_float(
        value_provider, f"[Manual] Set Laser Power to {laser_power:.2f} W. Type the measured value: ", minimum=0,
    )
    result = await exp.driver.confirm_laser_power(ConfirmLaserPower(measured_power=measured))
    return result.measured_power


async def perform_single_deposition(
    exp: ExperimentSession,
    *,
    experiment_uuid: str | uuid.UUID | None = None,
    project_name: str,
    pressure: float,
    temperature: float,
    laser_power: float,
    laser_repetition_rate: float,
    target_id: str,
    target_material: str,
    num_pulse: int = 1000,
    do_preablation: bool = False,
    preablation_pulse: int = 1000,
    preablation_frequency: float = 10.0,
    before_experiment_waittime: float = 2.0,
    after_experiment_waittime: float = 2.0,
    ramp_rate: float = 20.0,
    is_dryrun: bool = False,
    finish_substrate: bool = True,
    input_provider: InputProvider = default_input_provider,
    value_provider: ValueProvider = default_value_provider,
) -> tuple[bool, tuple[str | None, dict | None]]:
    """Port of SingleDepoExperimentManager.perform_experiment: one substrate, one
    position, a fixed recipe. Returns (success, (storage_name, final_conditions))."""
    experiment_uuid = str(experiment_uuid or uuid.uuid4())

    if not is_dryrun:
        ack = await exp.driver.to_temperature(ToTemperature(temperature=temperature, ramp_rate=ramp_rate))
        await _wait_for_task(exp, ack.task_id)

    await input_provider("[Manual] adjust RHEED source and sample stage")

    await input_provider(f"[Manual] Set Pressure to {pressure:.2e} Torr.")
    real_pressure = (await exp.driver.get_current_pressure()).pressure

    await input_provider("[Manual] Set excimer laser to ON mode")
    real_laser_power = await run_set_laser_power(exp, laser_power, value_provider=value_provider)

    await input_provider("[Manual] adjust RHEED source and sample stage")

    current = await exp.driver.current_substrate()
    if current.substrate is None or current.substrate.current_pixel_index is None or (
        current.substrate.current_pixel_index >= len(current.substrate.positions)
    ):
        log.warning(
            "no pixel left to grow on: substrate=%s index=%s of %s positions",
            getattr(current.substrate, "substrate_id", None),
            getattr(current.substrate, "current_pixel_index", None),
            len(current.substrate.positions) if current.substrate else 0,
        )
        return False, (None, None)

    # The steps below drive the mask and carousel; make sure the motor is enabled
    # before the first one so it does not stall in `_await_motor_ready`.
    await run_ensure_motor_ready(exp, input_provider)

    if do_preablation:
        ack = await exp.driver.perform_preablation(PerformPreablation(
            target_id=target_id, num_pulse=preablation_pulse, frequency=preablation_frequency, is_dryrun=is_dryrun,
        ))
        await _wait_for_task(exp, ack.task_id)

    state = await exp.driver.get_state()
    start = await exp.driver.start_storage(StartStorage(project_name=project_name, is_dryrun=is_dryrun))
    if not start.ok:
        log.warning("start_storage refused: %s", start.message or "(no reason given)")
        return False, (None, None)

    await asyncio.sleep(before_experiment_waittime)

    ack = await exp.driver.perform_deposition(PerformDeposition(
        num_pulse=num_pulse, laser_repetition_rate=laser_repetition_rate, target_id=target_id, is_dryrun=is_dryrun,
    ))
    await _wait_for_task(exp, ack.task_id)

    await asyncio.sleep(after_experiment_waittime)

    end = await exp.driver.end_storage(EndStorage(is_dryrun=is_dryrun))
    if not end.ok:
        log.warning("end_storage failed: %s", end.message or "(no reason given)")
        return False, (None, None)

    await exp.driver.finish_experiment_record(FinishExperimentRecord(
        substrate_id=current.substrate.substrate_id, project_id=state.project_id, experiment_uuid=experiment_uuid,
        temperature=temperature, pressure=real_pressure, laser_power=real_laser_power,
        laser_repetition_rate=laser_repetition_rate, target_material=target_material, num_pulse=num_pulse,
        is_pixel=False, storage_name=start.storage_name, record_uuid=start.record_uuid,
    ))

    if finish_substrate:
        await exp.driver.finish_substrate()

    final_conditions = {
        "Pressure": real_pressure, "Laser Power": real_laser_power, "Temperature": temperature,
        "Laser Repetition Rate": laser_repetition_rate, "Target Material": target_material, "Num Pulse": num_pulse,
    }
    await input_provider("[Manual] Set excimer laser to Standby mode")
    return True, (start.storage_name, final_conditions)


async def perform_pixel_deposition(
    exp: ExperimentSession,
    *,
    experiment_uuid: str | uuid.UUID | None = None,
    project_name: str,
    pressure: float,
    temperature: float,
    laser_power: float,
    laser_repetition_rate: float,
    target_id: str,
    target_material: str,
    num_pulse: int = 1000,
    do_preablation: bool = False,
    preablation_pulse: int = 1000,
    preablation_frequency: float = 10.0,
    before_experiment_waittime: float = 2.0,
    after_experiment_waittime: float = 2.0,
    ramp_rate: float = 20.0,
    is_dryrun: bool = False,
    is_experiment_finished: bool = True,
    input_provider: InputProvider = default_input_provider,
    value_provider: ValueProvider = default_value_provider,
) -> tuple[bool, bool, tuple[str | None, dict | None]]:
    """Port of PixelExperimentManager.perform_experiment: a multi-pixel substrate,
    depositing at whatever position is currently active. Returns (success,
    is_terminated, (storage_name, final_conditions)) -- is_terminated=True means "no
    pixel positions left", distinct from an ordinary failure."""
    experiment_uuid = str(experiment_uuid or uuid.uuid4())

    current = await exp.driver.current_substrate()
    if current.substrate is None or current.substrate.current_pixel_index is None or (
        current.substrate.current_pixel_index >= len(current.substrate.positions)
    ):
        log.warning("no pixel left to grow on")
        return False, False, (None, None)
    pixel_index = current.substrate.current_pixel_index

    # current_substrate() above already confirmed a valid current position exists,
    # so unlike the original's to_current_pixel() returning False for "no pixels
    # left", a failure here is a genuine hardware fault (an out-of-bounds position)
    # and is left to raise rather than being folded into is_terminated.
    await run_ensure_motor_ready(exp, input_provider)
    await exp.driver.to_current_pixel()

    await input_provider(f"[Manual] Set Pressure to {pressure:.2e} Torr.")
    real_pressure = (await exp.driver.get_current_pressure()).pressure

    real_laser_power = await run_set_laser_power(exp, laser_power, value_provider=value_provider)
    await input_provider("[Manual] Set excimer laser to Standby mode")
    await input_provider("[Manual] adjust RHEED source and sample stage")

    if not is_dryrun:
        ack = await exp.driver.to_temperature(ToTemperature(temperature=temperature, ramp_rate=ramp_rate))
        await _wait_for_task(exp, ack.task_id)

    final_conditions = {
        "Pressure": real_pressure, "Laser Power": real_laser_power, "Temperature": temperature,
        "Laser Repetition Rate": laser_repetition_rate, "Target Material": target_material, "Num Pulse": num_pulse,
    }

    await run_check_mask_center(exp, value_provider=value_provider)
    await exp.driver.to_current_pixel()

    await input_provider("[Manual] Set excimer laser to ON mode")
    await input_provider("[Manual] adjust RHEED source and sample stage")
    await run_adjust_rheed_gain(exp, value_provider=value_provider)

    if do_preablation:
        ack = await exp.driver.perform_preablation(PerformPreablation(
            target_id=target_id, num_pulse=preablation_pulse, frequency=preablation_frequency,
            is_dryrun=is_dryrun, move_mask_to_block_position=False,
        ))
        await _wait_for_task(exp, ack.task_id)

    state = await exp.driver.get_state()
    start = await exp.driver.start_storage(StartStorage(project_name=project_name, is_dryrun=is_dryrun))
    if not start.ok:
        log.warning("start_storage refused: %s", start.message or "(no reason given)")
        return False, False, (start.storage_name, final_conditions)

    await asyncio.sleep(before_experiment_waittime)

    ack = await exp.driver.perform_deposition(PerformDeposition(
        num_pulse=num_pulse, laser_repetition_rate=laser_repetition_rate, target_id=target_id, is_dryrun=is_dryrun,
    ))
    await _wait_for_task(exp, ack.task_id)

    await asyncio.sleep(after_experiment_waittime)

    if not is_dryrun:
        end = await exp.driver.end_storage(EndStorage(is_dryrun=is_dryrun))
        if not end.ok:
            log.warning("end_storage failed: %s", end.message or "(no reason given)")
            return False, False, (start.storage_name, final_conditions)

        await exp.driver.finish_experiment_record(FinishExperimentRecord(
            substrate_id=current.substrate.substrate_id, project_id=state.project_id, experiment_uuid=experiment_uuid,
            temperature=temperature, pressure=real_pressure, laser_power=real_laser_power,
            laser_repetition_rate=laser_repetition_rate, target_material=target_material, num_pulse=num_pulse,
            is_pixel=True, pixel_index=pixel_index, storage_name=start.storage_name, record_uuid=start.record_uuid,
        ))

    if is_experiment_finished:
        await exp.driver.finish_current_pixel(FinishCurrentPixel())

    return True, False, (start.storage_name, final_conditions)
