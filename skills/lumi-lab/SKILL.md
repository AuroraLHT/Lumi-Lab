---
name: lumi-lab
description: How to run the PLD/RHEED lab through the `lumi` MCP tools safely and sensibly -- the limits never to cross, which steps a person must do, the order of a whole-film or multi-pixel growth, when to stop, how to read RHEED, and how to keep the growth database honest. Use whenever driving the chamber (experiment.driver.*), judging a RHEED pattern or oscillation, planning growth conditions, or reading or correcting the growth history.
---

# Running the Lumi lab

The `lumi` MCP server tells you *what each tool does*. This skill is the lab's
judgement: what is safe, what needs a person, what a good growth looks like.

Where this skill gives no number or rule for something, treat it as unknown: ask the
person, do not guess.

Rules are marked **[hard]** (never break it, stop and ask instead) or **[thumb]** (the
usual choice; deviate if you say why).

## 1. Limits

The driver enforces some of these itself (`cfg/settings.toml`, `[experiment.bounds]`):
a setpoint outside 160–1000 °C is refused, PID engages above 220 °C, and a ramp over
the substrate's limit is refused. Everything else here is only as strong as you
following it.

- **[hard]** Ramp rate, by substrate area (width × height from `get_substrate`):
  10 °C/min for ≥ 75 mm², 20 °C/min for ≥ 50 mm², 30 °C/min for < 50 mm².
  The same for cooling. `to_temperature`, `cool_down` and `anneal` refuse a faster
  `ramp_rate`; leave `ramp_rate` out and they use the current substrate's limit.
  `run_sequence` refuses a version with a temperature step over it (a ramp of 0 is a
  jump, and is refused too).
  With no substrate registered the limit is 10, so register the substrate before
  heating. Slower is always allowed.
  From a cold chamber (below 220 °C) the ramp rate does not apply at first. PID only
  works properly above 220 °C, so `to_temperature` first heats on a fixed current
  (7.8 A, about 265 °C) and only then hands over to PID, which ramps from the
  measured temperature at the rate you gave. Expect that first stretch to run faster
  or slower than the nominal rate, and do not count it against the temperature stop
  rule (§4) or report it as a fault.
  Cooling is passive on the real chamber. Somewhere below 300–400 °C the substrate
  cannot shed heat as fast as the set rate, so it falls behind the setpoint, more and
  more the colder it gets. That is expected: as long as the temperature keeps
  dropping, let it run. Only a temperature that stops falling (or rises) is a problem.
- **[hard]** Do not ablate (`perform_preablation`, `perform_deposition`) unless the
  pressure has settled at its setpoint, the temperature is at its setpoint, and the
  mask is where the step needs it.
- **[hard]** `to_temperature` needs the heating laser on (`initiate_heating_laser`);
  with it off the node refuses. Do not "fix" a refusal by retrying at another value.
- **[hard]** Before heating, ask the person whether to heat in gas or in vacuum, and if
  in gas, which gas and what pressure. Set the pressure first when it is gas.
- **[hard]** Turn the heating laser off (`turn_off_heating_laser`) only once
  `get_current_temperature` reads cold (165 °C or less; 160 °C is the pyrometer's
  floor). Cutting it hot cools the sample all at once. The driver refuses above 165 °C;
  never pass `force` to get round that. In particular, a `to_temperature` with `wait: false` ends its
  task as soon as the ramp starts, so its finishing says nothing about the temperature.
- **[hard]** Use `is_dryrun: true` unless a person is present or a human user has asked
  for a real run, and the lab has not said otherwise for this session.

## 2. What a person must do

These ops return with `pending_confirmation` set. The person at the chamber answers them.
**[hard]** Never answer one on your own reading of the situation. Tell the person what is
being asked, wait, and pass their answer on.

| pending kind | what the person does | tool that resolves it |
| --- | --- | --- |
| `laser_power` | sets the excimer and reads the meter at the view port | `confirm_laser_power` (their measured value) |
| `mask_center_alignment` / `mask_center_check` | looks at the mask on the camera | `confirm_center_mask` / `confirm_mask_center` |
| `rheed_gain` (you raise it with `begin_adjust_rheed_gain`, e.g. when `rheed_on`'s search says pixels are at full scale; the gain is theirs, never set it yourself) | judges the RHEED image | `set_rheed_gain`, `confirm_rheed_gain` |
| `rheed_emission` | (only with `rheed_on` `judge: "person"`) says when the RHEED beam has saturated, from the filament / emission / intensity table | `rheed_emission_step_up` (one 50 mA step, only when they say up), `finish_rheed_on` (at the step they name) |
| `pixel_check` | checks each position on the camera | `resolve_pixel_check` |
| `fiducial_role` | tags the mask-center marker in the UI | (continues by itself) |
| `pressure` | sets the chamber pressure at the gauge (raised by `set_pressure_setpoint` on the real chamber, legacy MI mode only) | `confirm`, then read it back with `get_current_pressure` |
| `proceed` | a plain go/no-go | `confirm` |

The excimer can be switched (`chamber.pld.laser_on` / `laser_standby` / `laser_off`, mode,
HV, energy) only while `chamber_state.excimer.control` is true; otherwise the second PC
holds it and every laser op is refused. Moving that control (`take_laser_control`,
`release_laser`) is the person's call: ask first. Under `--use-legacy-mi-mode` the excimer
stays by hand.

To stop a deposition or preablation that is going wrong, call
`experiment.driver.stop_deposition`: it ends the task (its result says `aborted`) and
stops the laser. `chamber.pld.stop_fire` stops only a pulse train, for a `fire_laser` of
your own. A fire cut short fails with "stopped by request", and that ends the run --
never fire again to make up the pulses. `chamber.pld.stop_motors` halts every axis (and the
laser and a running sequence) for a move going wrong; it latches, and only the person resets
it (`reset_motors`) and re-homes the axes in `motion.homing_needed`. Under `--use-legacy-mi-mode` neither works: ask
the person to press STOP on PASCAL.

The RHEED gun is switched by `experiment.driver.rheed_on` / `rheed_off` (pld driver
only): on at the lab's operating values (25 kV, the filament raised from 1800 mA in 50 mA
steps to 2100 mA at most, focus and deflection), off ramped down. By default you judge
when the beam has saturated, and no confirmation is asked: `rheed_on` returns after
filament and HV with the search open (`searching`). Call `rheed_emission_step_up` once per
step. Each step reads the emission current and the RHEED image's signal (its brightest
10% of pixels above background, `bright_fraction`), and both level off exponentially as the beam saturates. From
3 steps on, the `saturation` hint fits that and says how near each is to its plateau.
Saturated is both within 10% of it (`plateau_fraction`): the last 10% of brightness is
not worth the filament. Call `finish_rheed_on` at the step the hint names, the first
that had both there, so the filament runs no hotter than it needs to. Before a plateau
can be fitted, or when a step adds nothing, a step under about 1% (emission) or 3%
(image) is flat. If the two disagree, take one more step; at 2100 mA there is none, so
choose between the steps it offers. A clipped image (any pixel at full scale in any of
the frames, `rheed_clipped_px`) is not judged (the emission decides). If the readings look wrong (the emission falling, the image dark), stop and
show the person the table. The hint sets the two side by side; it is not the decision. Keep the camera gain fixed during the search, or the intensities don't
compare. Show the person the final table either way. If they want to decide themselves,
pass `judge: "person"`: that raises a `rheed_emission` confirmation, so step up only when
they say so and finish at the step they name. Under `--use-legacy-mi-mode` the person
switches the gun. Focus and deflection depend on the alignment: pass the person's values
when they give them. A `rheed_on` that fails says what was reached and leaves the gun as
it got: tell the person, and do not retry blindly.

Also by hand, with no tool: the RHEED beam position screws, incidence angle and
sample-stage adjustment, and pressing MOTOR ENABLE when `is_motor_free` says the holding lock is released. A
Mask1 move or homing refused with `chamber_state.motion.sample_too_low` set means the sample
sits below its height threshold: ask the person to raise it (by hand), do not retry.
The pressure is set by pld-service itself under the default driver, control first, then
the setpoint:
1. `set_pressure_control(on: true)` arms control and moves nothing (state `armed`).
2. `set_pressure_setpoint` with `wait: true`. In another regime pld-service first
   switches the valves and gauges (MV10/MV11 opened or closed, IG1 switched, MFC1 to 0,
   CV301 opened if closed; minutes for the high regime), then regulates. It returns
   once the reading is within `tolerance` (10%), and fails if control stops meanwhile.
   A setpoint while control is off is refused: do step 1, never retry it as it is.
3. Read it back with `get_current_pressure` before going on.

The result of both is control's state: `off`, `armed`, `switching` or `regulating`
(also `chamber_state.gas.pressure_control_state`). Only `regulating` means the pressure
is being held; never judge it from `pressure_mode`. `set_pressure_control(on: false)`,
and any interlock, stops control and clears the setpoint, so the next control needs both
steps again. Only under `--use-legacy-mi-mode` on the real chamber does
`set_pressure_setpoint` not command the controller: it raises a `pressure` confirmation
asking the person to set it (state `unknown`).

## 3. Growing

Pick the procedure, read it, and follow it in order:

- **`procedures/film-deposition.md`**: a film over the whole substrate, one layer or a
  stack of several.
- **`procedures/pixel-deposition.md`**: one growth per position on a multi-position
  (pixel) substrate, with the mask selecting the position.

What applies to both:

- **Before starting**, check the lab is alive: `system.registry.list_nodes`,
  `check_mi_mode`, `check_logging_alive`, `get_pump_status`, `get_valve_status`,
  `is_motor_free`. `check_mi_mode` answering `off` means the chamber is not taking
  commands: its message says why (pld-service down or read-only on hardware, or, under
  the legacy MI driver, PASCAL not in MI Mode). Ask the person to fix that, and do not
  send anything else until it says `on`. `busy` means someone else is driving (the REA
  GUI holds control, or another MI script is running); check again once they finish.
- **[hard]** Long-running ops (`to_temperature`, `cool_down`, `perform_preablation`,
  `perform_deposition`, `anneal`, `auto_align_center_mask`) return at once with a
  `task_id`. Poll until `current_task` clears, then read `last_task`: check its
  `task.id` is your `task_id`, and that `result.ok` is true (if not, `result.error` says
  why, and `result.aborted` that it was stopped). Then check the step **succeeded** in
  the chamber too: read the temperature or pressure back. The recipes do not stop on a failed step, so you must.
  One task at a time: starting another while one runs is refused. To replace a task,
  `cancel_task` it first, on purpose; never cancel just to get your next step in.
  Cancelling leaves the chamber as it is, not as it was: a cancelled `to_temperature`,
  `cool_down` or `anneal` switches PID off and holds the LD at its present current (the
  substrate stays near where it got to); a cancelled deposition stops the laser and
  leaves shutter, mask and target as they are.
- **[thumb]** Heating takes a long time, and while `to_temperature` waits for the
  substrate PASCAL runs no other command. Pass `wait: false` so its task ends once the
  ramp is under way, do other set-up meanwhile (setting the pressure, the laser
  energy), and poll `get_current_temperature` until it is within 10 °C of the
  setpoint before any step that needs it there. The laser-off rule in §1 still holds.
  From cold the task first raises the heating current by hand, open-loop, until PID can
  take over (about 220 °C); a fire or move meanwhile does not hold it up. If the
  warm-up fails, the task ends `ok: false` with the current set back to where it
  started; read `last_task.result`.
- Targets: the carousel slots are A–F plus Clear and Monitor. The chamber log's `TG No.`
  counts from 0: 0 = A, 1 = B, 2 = C, 3 = D, 4 = E, 5 = F, 6 = Clear, 7 = Monitor.
  `get_current_target` does the conversion and names what is loaded there.
- **[hard]** Never deposit without `start_storage` returning `ok`: the growth would leave
  no record.
- **[hard]** `finish_experiment_record` gets the **measured** pressure, not the requested
  one, and the laser energy `begin_set_laser_power` set at the meter.
- **[hard]** Laser energy: once a day, before the first deposition, the laser test
  (`begin_laser_test`, `reference/laser.md`). `begin_set_laser_power` then sets the
  excimer from it, with no meter reading; it is refused when there was no test today.
- **[hard]** Pre-ablation is set per target material (`list_target_preablation`). For a
  material with none set, ask the person how many pulses and at what rate (0 = never:
  some targets, e.g. HZO, are damaged by extra pulses) and `set_target_preablation`.
  Then leave `num_pulse` out of `perform_preablation`; give it only when the person asks
  for a different amount. Never choose an amount yourself.
- **[hard]** Sequences (`procedures/sequences.md`): a person reviews and approves each
  version once, in the Deck; you never approve one, and you run only the version and hash
  they approved. Run it with `record` (a recording around the whole run); it needs the
  substrate loaded, and records its own layers and growth records. The project is the
  recording's `project_name` (`start_storage` and `record` register it, so
  `register_project` is needed only to name one without recording).
- **[thumb]** Anneal and cool down in O2 for oxides.

### Snapshots

**[hard]** Save a snapshot at each stage with `take_snapshot` (`camera`, `stage`, and a
`note` for anything worth saying):

| stage | chamber camera | RHEED camera |
| --- | --- | --- |
| `start` | yes | |
| `heated` | yes | |
| `depo_start` | yes | yes |
| `depo_mid` (half the pulses ÷ repetition rate after starting) | | yes |
| `depo_end` | yes | yes |
| `cooled` | yes | |
| `other` (anything else; say what in `note`) | as needed | as needed |

The chamber camera only sees the sample when the carousel is at Clear or Monitor; at
any other slot the target is in the way. Check `get_current_target` before a chamber
snapshot or any visual check; if it is at a target, the picture shows the target, not
the sample (during a deposition that is expected: say so in the `note`).

When the RHEED is down for maintenance, skip every RHEED snapshot and the RHEED pattern
checks, and record without it (`reference/equipment.md`).

Each is kept against the loaded sample and the chamber session. Look at each one
(`snapshot_jpeg`): does it show what that stage should? Say what you saw in your report.
A snapshot taken by mistake is hidden with `retire_snapshot`.

## 4. When to stop

Stop and ask the person when any of these happen:

- Temperature more than 10 °C from its setpoint for more than 30 s. Except during the
  warm-up from cold, and while cooling below ~400 °C as long as it is still falling
  (§1).
- Pressure above 1.3 × or below 0.7 × its setpoint.
- `check_logging_alive` says the log is stale, or a node drops out of `list_nodes`.
- A tool call times out (usually a node is down; sometimes the chamber's completion
  message was lost). Do not re-issue a motion or a deposition to "try
  again" before you know whether the first one ran.
- RHEED: the specular spot fades and does not come back, transmission spots appear, or
  rings appear (see `reference/rheed.md`).
- A snapshot shows something the stage should not (§3).

- **[hard]** Otherwise, run the planned pulse count. Do not end a deposition early on
  your reading of the oscillations: not every growth oscillates, and counting them is
  not reliable enough yet.

## 5. Where to go next

- `procedures/film-deposition.md`, `procedures/pixel-deposition.md`: the growths.
- `reference/materials.md`: the laser settings and substrates.
- `reference/rheed.md`: reading patterns and oscillations; using the simulator.
- `procedures/rheed-azimuth-alignment.md`: turning the sample to a crystal direction.
- `procedures/sequences.md`: step lists pld-service runs (wedges, combinatorial runs).
- `reference/laser.md`: the daily laser test and how the energy is set.
- `reference/temperature.md`: what the pyrometer reads, its emissivity history,
  comparing temperatures across it. `procedures/pyrometer-calibration.md`: recalibrating
  it (a person does it).
- `reference/records.md`: keeping the growth database honest.
- `reference/history.md`: answering questions from past growths.
- `reference/equipment.md`: known chamber quirks and how they show up.
