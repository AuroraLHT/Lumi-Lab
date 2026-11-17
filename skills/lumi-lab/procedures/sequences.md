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

A step is one operation, in pld.proto's names, with an optional `label`:

| step | what it does |
|---|---|
| `{"select_target": {"target": "A", "wait": true}}` | carousel to a slot |
| `{"mask": {"position": 20, "wait": false, "speed_mm_s": 0.5, "accel_mm_s_per_ms": 0.18}}` | Mask1; speed/accel optional (cap 8.33 mm/s) |
| `{"sample": {...}}`, `{"rheed_gun_x": {...}}` | the other axes (no speed) |
| `{"sample_shutter": {"in_front": false}}` | false = open for deposition |
| `{"fire_laser": {"pulses": 100, "rate_hz": 5, "wait": true}}` | a pulse train |
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

1. Before: everything a deposition needs that the list does not do itself: substrate,
   temperature, pressure, the laser test if it has `meter_energy`, pre-ablation,
   `start_storage`.
2. `run_sequence` with the name, version and `content_hash` the person approved. Watch
   `sequence` in the driver state (step i of n, loops, operations done / total, message).
3. While it runs pld-service refuses every other operation, from every client, except
   stop_fire, the RHEED all-offs and pause/cancel. `pause_sequence`: a wait stops
   counting, a waited fire stops its train (resume fires the pulses left), any other step
   finishes first. `stop_deposition` or `cancel_sequence` ends it: a move or ramp under
   way carries on, nothing is undone.
4. A refused step fails the run; nothing is retried. Report it with the step and reason;
   do not edit and re-run without the person.
5. After: `end_storage`, `finish_experiment_record` (the pulses and energy the list used),
   and say what the journal shows (`sequence_step` rows, one per operation).
