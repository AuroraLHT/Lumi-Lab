# Sequences: step lists pld-service runs

For a routine whose timing matters or that repeats many times (a wedge, a combinatorial
library), pld-service runs a stored list of steps by itself, so it does not depend on
Lumi's timing or connection. pld driver only.

## The rule

A person reviews a list **once**, as one exact version, and approves it in the Deck.
After that the agent may run that version without asking. Saving different steps under
the same name makes a new version, which needs approval again. You cannot approve (the
driver refuses the agent's `mcp:` calls): show the person `get_sequence` and ask.

## Steps

A step is one operation, in pld.proto's names, with an optional `label`. Before
writing select_target steps, read what is loaded: `show_available_targets` (pld-service's
own `target_names`, the one source; slot letters, not the material you remember). Write
the material beside each select_target as `expect_material`, so the sequence cannot
fire on a target that has since been swapped. Name the sequence after what is loaded.

| step | what it does |
|---|---|
| `{"select_target": {"target": "E", "wait": true}, "expect_material": "SrRuO3"}` | carousel to a slot; `expect_material` (always add it) is the material the step grows from, and the run is refused if the slot holds anything else |
| `{"mask": {"position": 20, "wait": false, "speed_mm_s": 0.5, "accel_mm_s_per_ms": 0.18}}` | Mask1; speed/accel optional (cap 8.33 mm/s) |
| `{"sample": {...}}`, `{"rheed_gun_x": {...}}` | the other axes (no speed) |
| `{"sample_shutter": {"in_front": false}}` | false = open for deposition |
| `{"fire_laser": {"pulses": 100, "rate_hz": 5, "wait": true}}` | a pulse train, at most 20 Hz (Lumi's soft cap; the excimer does 100) |
| `{"meter_energy": {"mj": 120}}` | Lumi's: energy at the meter, set from today's laser test |
| `{"temperature": {...}}`, `{"pressure": {...}}`, `{"mfc_flow": {...}}` | as the direct operations |
| `{"target_spin": {"on": true}}`, `{"target_twist": ...}`, `{"target_auto_motion": {"spin": true, "twist": true}}` | target motion (no twist at Clear) |
| `{"wait_s": 30}` | wait |
| `{"pause": "check the film"}` | wait for a person to resume |
| `{"wait_axis_idle": {"axis": "Mask1"}}` | after a move with `wait: false` |
| `{"loop_start": 5}` ... `{"loop_end": {}}` | repeat; nests |

A raw `laser_energy` (a source energy) is refused: use `meter_energy`.

Wedge pattern: `mask` with `wait: false` and a speed, then a waited `fire_laser`, then
`wait_axis_idle` Mask1: the PLC runs the move and the train together.

## Running one

1. Before: everything a deposition needs that the list does not do itself: the
   substrate loaded (the project is `record`'s `project_name`) (and the pixel moved to, `to_pixel`: one run is one pixel),
   temperature, pressure, the laser test if it has `meter_energy`, pre-ablation if the
   list does not do it. A run without a substrate is refused; `no_sample: true` is for a
   rehearsal on a dummy only, and records no layers.
2. `run_sequence` with the name, version and `content_hash` the person approved, and
   `record: {project_name, note}`: Lumi starts the recording before step 1 and ends it
   after the run, however it ends. Watch `sequence` in the driver state (step i of n,
   loops, operations done / total, layers so far, message).
3. While it runs pld-service refuses every other operation, from every client, except
   stop_fire, stop_motors (which cancels the run), the RHEED all-offs and pause/cancel. `pause_sequence`: a wait stops
   counting, a waited fire stops its train (resume fires the pulses left), any other step
   finishes first. `stop_deposition` or `cancel_sequence` ends it: a move or ramp under
   way carries on, nothing is undone. If something moves while it is paused (the Deck
   console, pld-gui), the journal says what.
4. A refused step fails the run; nothing is retried. Report it with the step and reason;
   do not edit and re-run without the person. State `unknown` means Lumi lost track of
   the run (pld-service stopped answering for 15 s, or restarted): the layers recorded so
   far stand, but the chamber may still be firing or moving. Look at `chamber_state` and
   tell the person before anything else.
5. What it records by itself:
   - **Layers**: every fire with the shutter open grows a layer of the selected slot's
     material, with the pulses actually fired (a fire cut short counts what it fired).
     Fires one after another on the same material are one layer (a wedge is one; an
     ABAB superlattice stays ABAB). A fire with the shutter in front, or at Clear, is no
     layer (a pre-ablation).
   - **Growth records**: one per layer, with the temperature and pressure read at its
     first pulse, the energy at the meter, and the sequence's name, version, hash and
     run id. Do not call `finish_experiment_record` for a run.
   - **Journal**: `sequence_step` per operation, `sequence_layer` per layer,
     `sequence_event` for pause, resume and moves during a pause.
6. After: `get_sample` should show the layers; report them and what the journal shows.
