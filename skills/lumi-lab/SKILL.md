---
name: lumi-lab
description: How to run the PLD/RHEED lab through the `lumi` MCP tools safely and sensibly -- the limits never to cross, which steps a person must do, the order of a whole-film or multi-pixel growth, when to stop, how to read RHEED, and how to keep the growth database honest. Use whenever driving the chamber (experiment.driver.*), judging a RHEED pattern or oscillation, planning growth conditions, or reading or correcting the growth history.
---

# Running the Lumi lab

The `lumi` MCP server tells you *what each tool does*. This skill is the lab's
judgement: what is safe, what needs a person, what a good growth looks like.

**Draft.** Lines marked `TODO(lab)` need a number or rule from the people who run the
chamber. Until one is filled in, treat that limit as unknown: ask, do not guess.

Rules are marked **[hard]** (never break it, stop and ask instead) or **[thumb]** (the
usual choice; deviate if you say why).

## 1. Limits

The driver enforces some of these itself (`cfg/settings.toml`, `[experiment.bounds]`):
a setpoint outside 160–1000 °C is refused, and PID engages above 220 °C. Everything
else here is only as strong as you following it.

- **[hard]** Ramp rate, by substrate area (width × height from `get_substrate`):
  10 °C/min for ≥ 75 mm², 20 °C/min for ≥ 50 mm², 30 °C/min for < 50 mm².
  The same for cooling.
- **[hard]** Do not ablate (`perform_preablation`, `perform_deposition`) unless the
  pressure has settled at its setpoint, the temperature is at its setpoint, and the
  mask is where the step needs it.
- **[hard]** `to_temperature` needs the heating laser on (`initiate_heating_laser`);
  with it off the node refuses. Do not "fix" a refusal by retrying at another value.
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
| `rheed_gain` | judges the RHEED image | `set_rheed_gain`, `confirm_rheed_gain` |
| `pixel_check` | checks each position on the camera | `resolve_pixel_check` |
| `fiducial_role` | tags the mask-center marker in the UI | (continues by itself) |
| `proceed` | a plain go/no-go | `confirm` |

Also by hand, with no tool: excimer ON/standby, RHEED gun and sample-stage adjustment,
pressing MOTOR ENABLE when `is_motor_free` says the holding lock is released, and
setting the pressure. **[hard]** Do not use `set_pressure` on the real chamber: it does
not work in a real run yet. Ask the person to set the pressure, then read it back with
`get_current_pressure`.

## 3. Growing

Pick the procedure, read it, and follow it in order:

- **`procedures/film-deposition.md`**: a film over the whole substrate, one layer or a
  stack of several.
- **`procedures/pixel-deposition.md`**: one growth per position on a multi-position
  (pixel) substrate, with the mask selecting the position.

What applies to both:

- **Before starting**, check the lab is alive: `system.registry.list_nodes`,
  `check_logging_alive`, `get_pump_status`, `get_valve_status`, `is_motor_free`.
- **[hard]** Long-running ops (`to_temperature`, `cool_down`, `perform_preablation`,
  `perform_deposition`, `anneal`, `auto_align_center_mask`) return at once with a
  `task_id`. Poll until `current_task` clears, then check the step **succeeded**: read the
  temperature or pressure back. The recipes do not stop on a failed step, so you must.
- **[hard]** Never deposit without `start_storage` returning `ok`: the growth would leave
  no record.
- **[hard]** `finish_experiment_record` gets the **measured** pressure and laser power,
  not the requested ones.
- **[thumb]** Pre-ablate before each deposition: 2000 pulses at 10 Hz, or 1000 pulses at
  5 Hz for strongly absorbing electrode materials (LSMO, SRO, pyrochlores, ITO, ...).
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

Each is kept against the loaded sample and the chamber session. Look at each one
(`snapshot_jpeg`): does it show what that stage should? Say what you saw in your report.
A snapshot taken by mistake is hidden with `retire_snapshot`.

## 4. When to stop

Stop and ask the person when any of these happen:

- Temperature more than 10 °C from its setpoint for more than 30 s.
- Pressure above 1.3 × or below 0.7 × its setpoint.
- `check_logging_alive` says the log is stale, or a node drops out of `list_nodes`.
- A tool call times out (usually a node is down; a lost MI completion message is a
  known issue, see `docs/TODO.md`). Do not re-issue a motion or a deposition to "try
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
- `reference/records.md`: keeping the growth database honest.
- `reference/history.md`: answering questions from past growths.
- `reference/equipment.md`: known chamber quirks and how they show up.
