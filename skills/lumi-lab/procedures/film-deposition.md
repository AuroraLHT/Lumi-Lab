# Whole-film deposition: one layer, or a stack

The film covers the whole substrate; the mask is not used to pick a position. Follows
`perform_single_deposition` in `src/lumi/experiment/recipes.py`, and the layer loop in
`notebooks/_single_deposition.py`.

Plan first: the layers bottom-up, each with its target slot, temperature, pressure,
laser power, repetition rate, pulse count and pre-ablation. Resolve each slot's material
with `get_target_name_by_id` now; slots change when targets are swapped.

## Once, before the first layer

1. Check the lab is alive (SKILL.md §3). **Snapshot: chamber `start`.**
2. `current_substrate`: the substrate to grow on. If none, `register_substrate`, or
   `resume_substrate` if it is already in the database. Never register one twice.
3. `initiate_heating_laser`, then `to_temperature` to the first layer's temperature at
   the substrate's ramp rate (SKILL.md §1). Wait for the task, read the temperature back.
   **Snapshot: chamber `heated`.**
4. A person adjusts the RHEED gun and sample stage. Look at `rheed.camera.image`: is it a
   good substrate pattern (`reference/rheed.md`)? If not, stop and say so.

## Each layer

5. If the temperature changes from the last layer, `to_temperature` (wait, read back).
6. A person sets the pressure; read it back with `get_current_pressure` and wait until it
   is within tolerance (SKILL.md §4).
7. A person sets the excimer to ON. `begin_set_laser_power` → the person reads the meter
   → `confirm_laser_power` with their value.
8. `perform_preablation` with this layer's target (SKILL.md §3 for pulses and rate). Wait.
9. `start_storage`; it must return `ok`. One recording per layer.
10. `perform_deposition`. **Snapshots: chamber + RHEED `depo_start`, RHEED `depo_mid`,
    chamber + RHEED `depo_end`.** Watch `rheed.integrator.cache`
    while it runs; stop on anything in SKILL.md §4.
11. `end_storage`, then `finish_experiment_record` with the measured pressure and laser
    power, `is_pixel: false`.

Do **not** `finish_substrate` between layers: every layer goes on the same substrate,
and finishing it after the first leaves nothing to grow the next on.

## After the last layer

12. A person sets the excimer to standby.
13. `anneal` if the stack calls for it, then `cool_down` at the substrate's ramp rate in
    the right atmosphere (O2 for oxides).
    **Snapshot: chamber `cooled`.**
14. `finish_substrate`.
15. Report: `get_sample` (the layer stack should match the plan), plus what each snapshot
    showed (`list_snapshots` for the sample).
