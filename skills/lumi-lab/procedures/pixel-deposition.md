# Multi-pixel deposition: one growth per position

The substrate has several positions (pixels); the mask opens one at a time. Follows
`perform_pixel_deposition` in `src/lumi/experiment/recipes.py`, except that the mask is
aligned by `auto_align_center_mask` rather than by a person.

## Once, before the first pixel

1. Check the lab is alive (SKILL.md §3). **Snapshot: chamber `start`.**
2. `current_substrate`: a substrate with a position left. If none, `register_substrate`,
   or `resume_substrate` if it is already in the database. Never register one twice:
   resuming keeps track of which positions are already grown. A new substrate needs a
   name first: ask the person unless the project's convention is known, and pass it as
   `substrate_name` (`reference/records.md`).
3. Align the mask (below). Laser test and pre-ablation amounts as in
   `film-deposition.md` steps 4–5, if not done today / not set.

## Each pixel

4. `current_substrate`: which position is next. None left → the substrate is done.
5. Align the mask (below), then `to_current_pixel`: mask and RHEED to the position.
6. `set_pressure` (the person sets it on the real chamber, then `confirm`); read it back
   and wait until it is within tolerance.
7. `begin_set_laser_power` with the energy at the meter: set from today's laser test, no
   meter reading (skipped by the driver if already set).
8. Ask the person (first pixel only): heat in gas or in vacuum? `initiate_heating_laser`
   if it is off, then `to_temperature` at the substrate's ramp rate with `wait: false`
   (SKILL.md §3). Poll the temperature until it is within tolerance. **Snapshot: chamber `heated`.** If the temperature
   changed, align the mask again (thermal expansion moves the holder), then
   `to_current_pixel`.
9. A person sets the excimer to ON. `rheed_on` if the gun is off
   (`chamber.pld.chamber_state` `rheed.filament_on`; SKILL.md §2), then the person adjusts
   the beam and stage.
   `begin_adjust_rheed_gain` → the person sets and confirms the gain. Look at
   `rheed.camera.image`: a good pattern for this position? If not, stop and say so.
   (Skipped while the RHEED is down for maintenance.)
10. `perform_preablation` with `move_mask_to_block_position: false` (the mask is already
    over the position), `num_pulse` left out (the target's set amount). Wait.
11. `start_storage`; it must return `ok`. With the RHEED down, `save_frame`, `save_ai`
    and `save_integration` all `false`.
12. `perform_deposition`. **Snapshots: chamber + RHEED `depo_start`, RHEED `depo_mid`,
    chamber + RHEED `depo_end`.** Stop on anything in SKILL.md §4.
13. `end_storage`, then `finish_experiment_record` with the measured pressure and the
    laser energy set in step 7, `is_pixel: true` and the `pixel_index` from step 4.
14. `finish_current_pixel`, only if the position was really grown on. A mistaken one is
    undone with `reopen_position`.

## Aligning the mask

**[hard]** Align before the first pixel, before every pixel, and after every
temperature change.

`auto_align_center_mask`, then wait for the task. If it holds on a `fiducial_role`
confirmation, no marker is tagged yet: tell the person to tag the mask-center marker in
the UI, and it carries on by itself. If the task fails (no slit seen, cancelled), stop:
do not grow through an unaligned mask, and do not guess a position. Ask the person
whether to align by hand (`begin_check_mask_center`).

## After the last pixel

15. A person sets the excimer to standby.
16. `cool_down` at the substrate's ramp rate in the right atmosphere. Wait until
    `get_current_temperature` reads 165 °C or less. **Snapshot: chamber `cooled`.**
17. `turn_off_heating_laser`, only now (SKILL.md §1), then `rheed_off`.
18. Report: `list_samples` with this substrate's `substrate_id` (every grown position
    should say so; left out, it lists every substrate), plus what each snapshot showed.
