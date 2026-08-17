"""The chamber itself: setpoints in, physics forward in time, a log row out.

Deliberately a lumped-parameter model, not a physics package. The point is that every
quantity the control code *reads back* moves when the command that should move it is
issued, on a timescale close enough to the real machine that timeouts, ramp waits and
"has it got there yet?" polls behave the way they will in the lab. Where a real number
was available it is anchored to one:

- the heater is the chamber's measured calibration: a 6.5 A minimum on the heating
  laser diode, and temperature linear in drive current from 7 A -> 160 degC to
  30 A -> 1000 degC. The floor is `PLDconfig.ini`'s `[PIDsettings] Pyro_min=160`, which
  is exactly why a cold chamber logs a flat 160 in the recorded asset;
- the gas line likewise: pressure linear in MFC flow from 0.5 sccm -> 1 mTorr to
  30 sccm -> 200 mTorr;
- the process gauge's ~1.1e-3 Torr zero offset is read off the recorded asset, where an
  ion gauge showing 6.7e-9 Torr sits next to a `Prc Pres Main` of 1.11e-3;
- the target angles default to the `[TGsettings]` block of the shipped INI, and
  `build_chamber_sim` overrides them with whatever the config reader actually serves.

Both calibrations are overridable from `[pascal.sim]` in settings.toml -- they are
measurements, and they will be re-measured.

Threading: the MI backend thread calls the setpoint methods, the log writer thread
calls `tick()` and `snapshot()`. One lock covers all of it -- the model is tiny and
touched a few hundred times a second, so there is nothing to win by being clever, and
a torn read here would surface as a physically impossible log row.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field

# PLDconfig_07102025.ini [TGsettings]. The carousel slot letters are the ones
# `settings.experiment.target_mapper` uses (A..F, Clear, Monitor).
DEFAULT_TARGET_ANGLES: dict[str, float] = {
    "A": 51.5, "B": 103.0, "C": 154.5, "D": 205.5, "E": 257.0, "F": 308.5,
    "Clear": 0.0, "Monitor": 0.0,
}
DEFAULT_TARGET_NUMBERS: dict[str, int] = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "Clear": 7, "Monitor": 8,
}


@dataclass
class ChamberSimConfig:
    """Everything the model needs. Populated from `[pascal.sim]` in settings.toml."""

    # --- heater -------------------------------------------------------------
    # Measured on the chamber: the heating laser diode has a 6.5 A minimum, and the
    # substrate temperature is linear in drive current from 7 A -> 220 degC to
    # 30 A -> 1000 degC.
    #
    # Note the calibration anchor (220 at 7 A) is not the pyrometer floor. `pyro_min`
    # is PLDconfig [PIDsettings] Pyro_min -- the pyrometer cannot *read* below it, which
    # is why an idle chamber logs a flat 160 -- while the lowest temperature the diode
    # can actually hold is the anchor extended down to `ld_min`, about 203 degC. Between
    # the two the substrate is simply cold and the pyrometer says 160.
    #
    # The anchor matters beyond realism: `_warm_up_to_pid_limit` ramps to PLDconfig's
    # `[PIDsettings] LDmin = 8.5`, and the growth only proceeds if that clears
    # `experiment.bounds.temperature_pid_engage_threshold = 220`. 8.5 A is 271 degC
    # here, so it does.
    pyro_min: float = 160.0
    ld_min: float = 6.5
    current_at_temperature_min: float = 7.0
    current_at_temperature_max: float = 30.0
    temperature_min: float = 220.0
    temperature_max: float = 1000.0
    heater_tau: float = 8.0
    current_tau: float = 1.0
    temperature_noise: float = 0.4
    default_ramp_rate: float = 20.0  # degC/min, matching manager.to_temperature

    # --- gas and pressure ---------------------------------------------------
    base_pressure: float = 5.0e-9
    # Measured: process pressure is linear in MFC flow from 0.5 sccm -> 1 mTorr to
    # 30 sccm -> 200 mTorr. Below 0.5 sccm the line is continued down to the base
    # pressure at zero flow, which the calibration does not cover.
    flow_at_pressure_min: float = 0.5
    flow_at_pressure_max: float = 30.0
    pressure_at_flow_min: float = 1.0e-3
    pressure_at_flow_max: float = 200.0e-3
    # The capacitance gauge's zero offset: `Prc Pres Main` never reads below this even
    # under full vacuum. Read off the recorded asset.
    process_gauge_offset: float = 1.11e-3
    # Constant pump speed, so the chamber is a first-order system with tau = V/S and
    # the same time constant pumping up or down. Set so a 1e-7 -> 1e-1 Torr change
    # settles (99% of the way, 4.6 tau) in roughly the 120s measured on the chamber.
    pressure_tau: float = 26.0
    mfc_tau: float = 1.5
    load_lock_pressure: float = 3.09e-7
    backing_pressure: float = 1.0e-4
    # Not modelled -- a second-range gauge whose relationship to the others cannot be
    # inferred from the recorded log. Held at its idle reading.
    process_gauge_2: float = 1.04

    # --- motion -------------------------------------------------------------
    # Measured on the chamber: 10 deg/s on the rotary axes, 10 mm/s on the masks.
    # Targets sit ~60 degrees apart, so a slot-to-slot move is about 6 seconds. (The
    # carousel angles themselves are read from PLDconfig's [TGsettings] rather than
    # assumed -- the checked-in file has them 51.5 degrees apart, giving ~5s.)
    target_rotation_speed: float = 10.0  # deg/s
    # The sample stage: `Set Sample Position` drives it to an absolute angle and
    # `Rotate Sample` turns it by a relative one; `Sample Rotation ON` free-spins it.
    # Same 10 deg/s for all three.
    sample_rotation_speed: float = 10.0  # deg/s
    sample_spin_speed: float = 10.0  # deg/s while free-spinning
    mask_speed: float = 10.0  # mm/s
    rheed_gun_speed: float = 1.0  # mm/s -- slower than the masks, and its travel is +/-3
    # `TG-Z` is a logged column with no command in lumi.pascal.command that drives it,
    # so nothing moves this axis today. The speed is here for when something does.
    tg_z_speed: float = 2.0  # mm/s -- NOT measured, see docs/TODO.md
    # PLDconfig [MotorSettings] Mask1setmax / Mask2setmax. A command past these parks
    # the mask on the stop and raises the limit-sensor bit in `A Motor1`.
    mask1_travel_max: float = 160.0
    mask2_travel_max: float = 5.0
    # Pneumatic sample shutter: measured at roughly a second, and not instant, so a
    # script can be caught firing into a shutter that has not finished opening.
    shutter_travel_time: float = 1.0
    # The deposition laser gate travels too. Assumed to match the sample shutter -- it
    # was reported as "not instant" without a figure.
    gate_travel_time: float = 1.0

    # --- laser --------------------------------------------------------------
    # 0 by default: `perform_preablation` compares MissedPuls before and after and
    # raises if it moved, so a non-zero rate here is a fault-injection knob, not a
    # default.
    missed_pulse_rate: float = 0.0

    target_angles: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TARGET_ANGLES))
    target_numbers: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TARGET_NUMBERS))

    seed: int | None = 20260806


class _Axis:
    """A motor axis that takes time to get somewhere.

    `Motor free` (bit 0 of `Motor Stat`) is the AND of every axis being idle, and
    `ExperimentManager.move_mask_to_position` refuses to move unless it is set -- so an
    axis that arrives instantly would make the interlock untestable.

    `rotary` axes take the shorter way round. The target carousel is one: without it,
    `Select Target F` -> `Select Target Clear` is 308.5 degrees of travel instead of
    51.5, so a script's timing is wrong by a factor of six on any wrap.
    """

    def __init__(self, position: float, speed: float, *, rotary: bool = False, limit: float | None = None) -> None:
        self.position = position
        self.target = position
        self.speed = speed
        self.rotary = rotary
        # Hard travel stop. A command past it parks the axis on the limit and raises
        # the limit-sensor alarm, which is what the real machine's log shows. The flag
        # latches until this axis is next commanded somewhere legal (`Seek Mask Home`
        # is the obvious clear), rather than clearing itself on arrival -- an alarm you
        # can only see for one log row is an alarm nobody sees.
        self.limit = limit
        self.limit_hit = False

    def _delta(self) -> float:
        delta = self.target - self.position
        if self.rotary:
            delta = (delta + 180.0) % 360.0 - 180.0
        return delta

    @property
    def busy(self) -> bool:
        return abs(self._delta()) > 1e-9

    def move_to(self, target: float) -> None:
        target = float(target)
        if self.limit is not None and not (0.0 <= target <= self.limit):
            self.limit_hit = True
            target = min(max(target, 0.0), self.limit)
        else:
            self.limit_hit = False
        self.target = target

    def tick(self, dt: float) -> None:
        delta = self._delta()
        if abs(delta) <= 1e-9:
            return
        step = self.speed * dt
        if abs(delta) <= step:
            self.position = self.target
        else:
            self.position += math.copysign(step, delta)
            if self.rotary:
                self.position %= 360.0

    def eta(self) -> float:
        return abs(self._delta()) / self.speed if self.speed > 0 else 0.0


class ChamberModel:
    def __init__(self, config: ChamberSimConfig | None = None) -> None:
        self.config = config or ChamberSimConfig()
        self._lock = threading.RLock()
        self._rng = random.Random(self.config.seed)

        c = self.config
        # degC per amp, from the two measured calibration points.
        self._temp_slope = (c.temperature_max - c.temperature_min) / (
            c.current_at_temperature_max - c.current_at_temperature_min
        )
        # Torr per sccm, likewise.
        self._pressure_slope = (c.pressure_at_flow_max - c.pressure_at_flow_min) / (
            c.flow_at_pressure_max - c.flow_at_pressure_min
        )

        # --- heater
        self.temperature_mode = "Manual"
        self.heating_laser_on = False
        self.heating_locked = True
        self.ramp_enabled = True
        self.ramp_rate = c.default_ramp_rate
        self.temperature_target = c.pyro_min  # where the operator asked for
        self.temperature_setpoint = c.pyro_min  # the ramped setpoint, logged as HT Temp set
        self.temperature_tolerance = 5.0
        self.manual_current = 0.18  # the recorded asset's idle standby current
        self.current_command = 0.18
        self.current_moni = 0.0
        self.temperature_true = c.pyro_min
        self.min_current = 0.0
        # The top of the calibrated range: 30 A is 1000 degC, and there is no measured
        # curve past it. `Set Maximum Current` overrides.
        self.max_current = c.current_at_temperature_max

        # --- laser
        self.laser_hz = 0.0
        self.laser_set = 0
        self.laser_pulses = 0
        self.missed_pulses = 0
        self._pulses_remaining = 0.0
        self._pulse_fraction = 0.0
        self.gate_open = False
        self.laser_enabled = True

        # --- gas / pressure
        self.mfc_set = {1: 0.0, 2: 0.0}
        self.mfc_moni = {1: 0.0, 2: 0.0}
        self.mfc_control = False
        self.pressure_control = False
        self.pressure_setpoint = 0.0
        self.control_mfc = 1
        self.pressure_gauge = "CDG10"
        self.true_pressure = c.base_pressure

        # --- motion
        self.target_slot = "A"
        self.target_number = c.target_numbers.get("A", 1)
        self._pending_slot: str | None = None
        self.rotation = _Axis(c.target_angles.get("A", 0.0), c.target_rotation_speed, rotary=True)
        self.mask1 = _Axis(0.0, c.mask_speed, limit=c.mask1_travel_max)
        self.mask2 = _Axis(0.0, c.mask_speed, limit=c.mask2_travel_max)
        self.tg_z = _Axis(0.0, c.tg_z_speed)
        self.rheed_gun_x = _Axis(0.0, c.rheed_gun_speed)
        self.target_rotation_mode = "AUTO"
        self.target_twist_mode = "AUTO"
        self.sample_rotation_mode = "OFF"
        # Positioned by `Rotate Sample`, which takes time like any other axis, and spun
        # continuously by `Sample Rotation ON`. The two are separate: the spin advances
        # the angle without the axis being "busy", so a spinning sample does not hold
        # `Motor free` low forever the way an unfinished move does.
        self.sample_rot = _Axis(261.60, c.sample_rotation_speed, rotary=True)
        #: Every positioning axis, in one place: `Motor free` is the AND of them all and
        #: `motion_eta` the max, so a new axis must never be added to one and not the
        #: other.
        self._axes = (self.rotation, self.mask1, self.mask2, self.tg_z,
                      self.rheed_gun_x, self.sample_rot)
        self.shutter_open = False
        self._shutter_target = False
        self._shutter_travel = 0.0
        self._gate_target = False
        self._gate_travel = 0.0
        self.log_interval_command = 60  # what `Log Interval` last asked for, in seconds

        self.sim_time = 0.0

    # --- the measured calibration curves ------------------------------------

    def temperature_for_current(self, current: float) -> float:
        """Steady-state substrate temperature for a heating-laser drive current.

        Linear between the two measured points, floored at the pyrometer minimum.
        Below `ld_min` the diode is under its lasing threshold and delivers nothing.
        """
        c = self.config
        if current < c.ld_min:
            return c.pyro_min
        temperature = c.temperature_min + self._temp_slope * (current - c.current_at_temperature_min)
        return max(c.pyro_min, temperature)

    def current_for_temperature(self, temperature: float) -> float:
        """The inverse, extended below the calibration anchor rather than clamped to it.

        Asking for less than the anchor's 220 degC returns a current under `ld_min`,
        which `temperature_for_current` maps back to the pyrometer floor -- so the pair
        round-trips, and a setpoint the diode cannot hold turns it off instead of
        pinning it at its lasing threshold and overshooting the setpoint forever.
        """
        c = self.config
        current = c.current_at_temperature_min + (temperature - c.temperature_min) / self._temp_slope
        return max(0.0, current)

    def pressure_for_flow(self, flow: float) -> float:
        """Chamber pressure in Torr for a total MFC flow in sccm."""
        c = self.config
        if flow <= 0:
            return c.base_pressure
        if flow < c.flow_at_pressure_min:
            # Under the calibrated range: continue the line down to base pressure at
            # zero flow rather than extrapolating the measured slope into negatives.
            fraction = flow / c.flow_at_pressure_min
            return c.base_pressure + (c.pressure_at_flow_min - c.base_pressure) * fraction
        return c.pressure_at_flow_min + self._pressure_slope * (flow - c.flow_at_pressure_min)

    def flow_for_pressure(self, pressure: float) -> float:
        """The inverse, used by the closed-loop pressure controller."""
        c = self.config
        if pressure <= c.base_pressure:
            return 0.0
        if pressure < c.pressure_at_flow_min:
            span = c.pressure_at_flow_min - c.base_pressure
            return c.flow_at_pressure_min * (pressure - c.base_pressure) / span
        return c.flow_at_pressure_min + (pressure - c.pressure_at_flow_min) / self._pressure_slope

    # --- setpoints, called from the MI backend thread -----------------------

    def set_temperature_control(self, mode: str) -> None:
        with self._lock:
            self.temperature_mode = "PID" if mode.upper() == "PID" else "Manual"

    def set_temperature_ramp(self, rate: float | None, enabled: bool) -> None:
        with self._lock:
            self.ramp_enabled = enabled
            if enabled and rate is not None:
                self.ramp_rate = float(rate)

    def set_temperature(self, temperature: float) -> None:
        with self._lock:
            self.temperature_target = float(temperature)
            if not self.ramp_enabled:
                self.temperature_setpoint = float(temperature)

    def set_temperature_tolerance(self, tolerance: float) -> None:
        with self._lock:
            self.temperature_tolerance = float(tolerance)

    def set_heating_current(self, current: float) -> None:
        with self._lock:
            self.manual_current = max(self.min_current, min(self.max_current, float(current)))

    def set_current_limits(self, minimum: float | None = None, maximum: float | None = None) -> None:
        with self._lock:
            if minimum is not None:
                self.min_current = float(minimum)
            if maximum is not None:
                self.max_current = float(maximum)

    def set_heating_laser(self, on: bool) -> None:
        with self._lock:
            self.heating_laser_on = bool(on)

    def set_heating_lock(self, locked: bool) -> None:
        with self._lock:
            self.heating_locked = bool(locked)

    def select_target(self, slot: str) -> None:
        with self._lock:
            c = self.config
            if slot not in c.target_angles:
                raise KeyError(f"unknown target slot {slot!r}; have {sorted(c.target_angles)}")
            # `TG No.` is committed on *arrival*, not on command: while the carousel is
            # turning the log still shows the target actually under the plume, which is
            # what anything correlating a deposition with its target has to see.
            self._pending_slot = slot
            self.rotation.move_to(c.target_angles[slot])

    def set_target_rotation_mode(self, mode: str) -> None:
        with self._lock:
            self.target_rotation_mode = mode.upper()

    def set_target_twist_mode(self, mode: str) -> None:
        with self._lock:
            self.target_twist_mode = mode.upper()

    def set_sample_rotation_mode(self, mode: str) -> None:
        with self._lock:
            self.sample_rotation_mode = mode.upper()

    def set_shutter(self, open_: bool) -> None:
        with self._lock:
            if bool(open_) == self._shutter_target:
                return
            self._shutter_target = bool(open_)
            self._shutter_travel = self.config.shutter_travel_time

    def set_gate(self, open_: bool) -> None:
        with self._lock:
            if bool(open_) == self._gate_target:
                return
            self._gate_target = bool(open_)
            self._gate_travel = self.config.gate_travel_time

    def trigger_laser(self, num_pulse: int, frequency: float) -> float:
        """Queue `num_pulse` pulses at `frequency` Hz. Returns the expected duration."""
        with self._lock:
            self.laser_set = int(num_pulse)
            self.laser_hz = float(frequency)
            self._pulses_remaining = float(num_pulse)
            self._pulse_fraction = 0.0
            self.gate_open = True
            self._gate_target = True
            self._gate_travel = 0.0
            return num_pulse / frequency if frequency > 0 else 0.0

    def move_mask(self, mask_id: int, distance: float) -> None:
        with self._lock:
            (self.mask1 if int(mask_id) == 1 else self.mask2).move_to(distance)

    def move_tg_z(self, distance: float) -> None:
        with self._lock:
            self.tg_z.move_to(distance)

    def move_rheed_gun_x(self, position: float) -> None:
        with self._lock:
            self.rheed_gun_x.move_to(position)

    def set_sample_angle(self, angle: float) -> None:
        """`Set Sample Position`: an absolute sample angle, 0-360."""
        with self._lock:
            self.sample_rot.move_to(float(angle) % 360.0)

    def rotate_sample_by(self, angle: float) -> None:
        """`Rotate Sample`: relative to wherever the sample is now."""
        with self._lock:
            self.sample_rot.move_to((self.sample_rot.position + float(angle)) % 360.0)

    def seek_home(self) -> None:
        with self._lock:
            self.mask1.move_to(0.0)
            self.mask2.move_to(0.0)

    def set_mfc_flow(self, mfc_id: int, flow: float) -> None:
        # Only MFC1/MFC2 are plumbed -- 3..5 log as -1.00 like the recorded asset. The
        # script grammar accepts `MFC(\d) Flow Set=`, though, so an MI script asking for
        # MFC3 used to add a key here that `_tick_gas` then hit a KeyError on, killing
        # the log-writer thread. Ignore what the chamber does not have.
        with self._lock:
            if int(mfc_id) not in self.mfc_set:
                return
            self.mfc_set[int(mfc_id)] = float(flow)

    def set_mfc_control(self, enabled: bool) -> None:
        with self._lock:
            self.mfc_control = bool(enabled)

    def set_pressure(self, pressure: float) -> None:
        with self._lock:
            self.pressure_setpoint = float(pressure)

    def set_pressure_control(self, on: bool) -> None:
        with self._lock:
            self.pressure_control = bool(on)

    def select_control_mfc(self, mfc: str) -> None:
        with self._lock:
            self.control_mfc = 2 if str(mfc).upper().endswith("2") else 1

    def select_pressure_gauge(self, gauge: str) -> None:
        with self._lock:
            self.pressure_gauge = str(gauge)

    def set_log_interval(self, seconds: int) -> None:
        with self._lock:
            self.log_interval_command = int(seconds)

    # --- what the MI backend waits on ---------------------------------------

    @property
    def motor_free(self) -> bool:
        with self._lock:
            return not any(a.busy for a in self._axes)

    @property
    def laser_idle(self) -> bool:
        with self._lock:
            return self._pulses_remaining <= 0

    @property
    def shutter_idle(self) -> bool:
        with self._lock:
            return self._shutter_travel <= 0

    @property
    def gate_idle(self) -> bool:
        with self._lock:
            return self._gate_travel <= 0

    def at_temperature(self) -> bool:
        with self._lock:
            reached_setpoint = abs(self.temperature_setpoint - self.temperature_target) < 0.5
            return reached_setpoint and abs(self.logged_temperature - self.temperature_setpoint) <= self.temperature_tolerance

    def motion_eta(self) -> float:
        with self._lock:
            return max(a.eta() for a in self._axes)

    @property
    def logged_temperature(self) -> float:
        """What the pyrometer reports -- floored at Pyro_min, which is why a cold
        chamber logs a flat 160 rather than room temperature."""
        return max(self.config.pyro_min, self.temperature_true)

    # --- physics ------------------------------------------------------------

    def tick(self, dt: float) -> None:
        if dt <= 0:
            return
        with self._lock:
            self.sim_time += dt
            self._tick_heater(dt)
            self._tick_laser(dt)
            self._tick_gas(dt)
            self._tick_motion(dt)

    def _tick_heater(self, dt: float) -> None:
        c = self.config
        # The ramped setpoint is what the log shows as `HT Temp set`; the operator's
        # request is where it is heading. Ramp rates are degC/minute on this machine.
        if self.ramp_enabled:
            step = self.ramp_rate / 60.0 * dt
            delta = self.temperature_target - self.temperature_setpoint
            self.temperature_setpoint = (
                self.temperature_target if abs(delta) <= step
                else self.temperature_setpoint + math.copysign(step, delta)
            )
        else:
            self.temperature_setpoint = self.temperature_target

        if not self.heating_laser_on:
            # Standby bias only; the diode is under its 6.5 A minimum and lasing
            # nothing, so this shows in the log and heats nothing.
            self.current_command = min(self.manual_current, c.ld_min)
        elif self.temperature_mode == "PID":
            wanted = self.current_for_temperature(self.temperature_setpoint)
            # No ld_min floor here, deliberately. The calibrated range now bottoms out
            # at 220 degC (7 A), so a setpoint below that -- `cool_down` asks for 160 --
            # wants a current under the diode's 6.5 A lasing minimum. Clamping up to
            # ld_min would hold the substrate at ~203 degC while `HT Temp set` read 160,
            # so `HT Temp moni` would sit permanently above the setpoint and every
            # "have we arrived?" poll in the recipes would wait forever. Letting the
            # command fall below ld_min switches the diode off, which is what the real
            # controller does and what lets the substrate coast to the floor.
            self.current_command = min(self.max_current, max(0.0, wanted))
        else:
            self.current_command = max(self.min_current, min(self.max_current, self.manual_current))

        self.current_moni += (self.current_command - self.current_moni) * _alpha(dt, c.current_tau)
        # The heating laser is what puts power into the substrate; with it off the
        # standby current still shows in the log but the substrate coasts back down to
        # the pyrometer floor.
        delivered = self.current_moni if self.heating_laser_on else 0.0
        target_temperature = self.temperature_for_current(delivered)
        self.temperature_true += (target_temperature - self.temperature_true) * _alpha(dt, c.heater_tau)

    def _tick_laser(self, dt: float) -> None:
        if self._pulses_remaining <= 0:
            self.gate_open = False
            return
        self._pulse_fraction += self.laser_hz * dt
        fired = int(self._pulse_fraction)
        if fired <= 0:
            return
        self._pulse_fraction -= fired
        fired = int(min(fired, self._pulses_remaining))
        self._pulses_remaining -= fired
        self.laser_pulses += fired
        rate = self.config.missed_pulse_rate
        if rate > 0:
            self.missed_pulses += sum(1 for _ in range(fired) if self._rng.random() < rate)
        if self._pulses_remaining <= 0:
            self.gate_open = False

    def _tick_gas(self, dt: float) -> None:
        c = self.config
        if self.pressure_control and self.pressure_setpoint > 0:
            # Closed loop: the controller trims the flow, so invert the calibration to
            # get the flow the log should show for the requested pressure.
            self.mfc_set[self.control_mfc] = self.flow_for_pressure(self.pressure_setpoint)

        for mfc_id, setpoint in self.mfc_set.items():
            flow = setpoint if self.mfc_control or self.pressure_control else 0.0
            self.mfc_moni[mfc_id] += (flow - self.mfc_moni[mfc_id]) * _alpha(dt, c.mfc_tau)

        equilibrium = self.pressure_for_flow(sum(self.mfc_moni.values()))
        self.true_pressure += (equilibrium - self.true_pressure) * _alpha(dt, c.pressure_tau)

    def _tick_motion(self, dt: float) -> None:
        for axis in self._axes:
            axis.tick(dt)
        if self._pending_slot is not None and not self.rotation.busy:
            self.target_slot = self._pending_slot
            self.target_number = self.config.target_numbers.get(self._pending_slot, 0)
            self._pending_slot = None
        if self._shutter_travel > 0:
            self._shutter_travel -= dt
            if self._shutter_travel <= 0:
                self._shutter_travel = 0.0
                self.shutter_open = self._shutter_target
        if self._gate_travel > 0:
            self._gate_travel -= dt
            if self._gate_travel <= 0:
                self._gate_travel = 0.0
                self.gate_open = self._gate_target
        if self.sample_rotation_mode in ("ON", "AUTO"):
            # Free spin: advance both the axis position and its target together, so the
            # axis never reads busy and a later `Rotate Sample` starts from where the
            # sample actually is.
            step = self.config.sample_spin_speed * dt
            self.sample_rot.position = (self.sample_rot.position + step) % 360.0
            self.sample_rot.target = self.sample_rot.position

    # --- rendering ----------------------------------------------------------

    def snapshot(self) -> dict:
        """One log row's worth of values, keyed by `LOG_COLUMNS`, unformatted."""
        with self._lock:
            c = self.config
            temperature = self.logged_temperature
            if c.temperature_noise:
                temperature += self._rng.gauss(0.0, c.temperature_noise)

            spinning = self.target_rotation_mode in ("ON", "AUTO") and not self.rotation.busy
            values = {
                "TG No.": self.target_number,
                "TG Rotate": self.rotation.position,
                "TG Spin Speed": 60 if spinning else 0,
                "TG-Z": self.tg_z.position,
                "Mask1": self.mask1.position,
                "Mask2": self.mask2.position,
                "Substrate": self.sample_rot.position,
                "FocusLns": 100.0,
                "MirrorPs": -1.0,
                "ATN": -1.0,
                "BTFvalve": -1.0,
                # The commanded repetition rate *while a train is running*. The
                # recorded asset idles this column at 0 rather than holding the last
                # rate, so it goes back to 0 when the train ends.
                "LaserHz": self.laser_hz if self._pulses_remaining > 0 else 0.0,
                "LaserPuls": self.laser_pulses,
                "Laser moni": self.laser_pulses,
                "Laser set": self.laser_set,
                "MissedPuls": self.missed_pulses,
                "PwrMeter": -1,
                "HT set": self.current_command,
                "HT moni": self.current_moni,
                "Delta HT curr": self.current_command - self.current_moni,
                "HT Temp set": self.temperature_setpoint,
                "HT Temp moni": temperature,
                # Same convention as `Delta HT curr`: set minus monitor.
                "Delta Temp": self.temperature_setpoint - temperature,
                "Prc Pres Main": self.true_pressure + c.process_gauge_offset,
                "Prc Pres Main2": c.process_gauge_2,
                "Vac Pres Main": self.true_pressure,
                "Back Pres Main": c.backing_pressure,
                "Prc Pres L/L": -1.0,
                "Vac Pres L/L": c.load_lock_pressure,
                "Back Pres L/L": c.backing_pressure,
                "Heat Stat": self._heat_stat(),
                "DepoLaserStat": self._depo_laser_stat(),
                # All seven pumps running and every mapped valve open, as in the
                # recorded asset. The simulator has no pump-down sequence.
                "Pump Stat": 0x007F,
                "Valve Stat1": 0x077F,
                "Valve Stat2": 0x0000,
                "Shut Stat": 0x0001 if self.shutter_open else 0x0000,
                "Motor Stat": self._motor_stat(),
                "Other Stat": 0x0000,
                "A Utility1": 0x0000, "A Utility2": 0x0000,
                "A Pump1": 0x0000, "A Pump2": 0x0000,
                "A EXT1": 0x0000, "A EXT2": 0x0000,
                "A Motor1": self._motor_alarms(), "A Motor2": 0x0000,
                "W Pross": 0x0000, "W etc": 0x0000,
            }
            for mfc_id in (1, 2):
                values[f"MFC{mfc_id} set"] = self.mfc_set[mfc_id]
                values[f"MFC{mfc_id} moni"] = self.mfc_moni[mfc_id]
                values[f"Delta MFC{mfc_id}"] = self.mfc_set[mfc_id] - self.mfc_moni[mfc_id]
            for mfc_id in (3, 4, 5):
                # Not installed on this chamber -- the controller logs -1.00, as in the
                # recorded asset.
                values[f"MFC{mfc_id} set"] = -1.0
                values[f"MFC{mfc_id} moni"] = -1.0
                values[f"Delta MFC{mfc_id}"] = 0.0
            return values

    def _heat_stat(self) -> int:
        # chamber_log.PARSE_DICT["Heat Stat"]: 0 heater PS, 1 PID control,
        # 2 ramp-rate setting, 3 ON/OFF monitor in the PS. The recorded asset sits at
        # 0x0004 with the heater cold: the ramp rate is configured, nothing is on.
        word = 0x0004 if self.ramp_enabled else 0
        if self.heating_laser_on:
            word |= 0x0001 | 0x0008
        if self.temperature_mode == "PID":
            word |= 0x0002
        return word

    def _depo_laser_stat(self) -> int:
        # 0 interlock, 1 HV, 2 gate shutter. The recorded asset holds 0x0001 the whole
        # session including while pulses are counting, i.e. that machine logs the
        # interlock and never the gate. Bit 0 is reproduced exactly; bit 2 is driven
        # honestly from the gate so a deposition window is visible in the status word
        # as well as in the pulse counter.
        word = 0x0001 if self.laser_enabled else 0x0000
        if self.gate_open:
            word |= 0x0004
        return word

    def _motor_alarms(self) -> int:
        # chamber_log.PARSE_DICT["A Motor1"]: bit 10 "Limit sensor in Mask 1",
        # bit 12 "Limit sensor in Mask 2". A mask commanded past its travel stop parks
        # on the stop and raises these -- the manager validates client-side, but a raw
        # MI script does not go through the manager.
        word = 0
        if self.mask1.limit_hit:
            word |= 1 << 10
        if self.mask2.limit_hit:
            word |= 1 << 12
        return word

    def _motor_stat(self) -> int:
        # bit 0 "Motor free", bit 1 "Target spin". This is the bit the recorded asset
        # never sets, which is why `is_motor_free()` is False forever when replaying it.
        word = 0x0001 if self.motor_free else 0x0000
        if self.target_rotation_mode in ("ON", "AUTO"):
            word |= 0x0002
        return word


def _alpha(dt: float, tau: float) -> float:
    """Discrete first-order lag coefficient. Exact rather than `dt/tau` so a coarse
    tick can never overshoot into oscillation."""
    if tau <= 0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)
