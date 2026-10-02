# Multi-pixel deposition: one growth per position

The substrate has several positions (pixels); the mask opens one at a time. Follows
`perform_pixel_deposition` in `src/lumi/experiment/recipes.py`, except that the mask is
aligned by `auto_align_center_mask` rather than by a person.

## Once, before the first pixel

1. Check the lab is alive (SKILL.md §3). **Snapshot: chamber `start`.**
2. `current_substrate`: a substrate with a position left. If none, `register_substrate`,
   or `resume_substrate` if it is already in the database. Never register one twice:
   resuming keeps track of which positions are already grown.
3. Align the mask (below).

## Each pixel

4. `current_substrate`: which position is next. None left → the substrate is done.
5. Align the mask (below), then `to_current_pixel`: mask and RHEED to the position.
6. `set_pressure` (the person sets it on the real chamber, then `confirm`); read it back
   and wait until it is within tolerance.
7. `begin_set_laser_power` → the person reads the meter → `confirm_laser_power`. (Skipped
   by the driver if the laser power is already set.)
8. `initiate_heating_laser` if it is off, then `to_temperature` at the substrate's ramp
   rate. Wait, read back. **Snapshot: chamber `heated`.** If the temperature
   changed, align the mask again (thermal expansion moves the holder), then
   `to_current_pixel`.
9. A person sets the excimer to ON and adjusts the RHEED gun and stage.
   `begin_adjust_rheed_gain` → the person sets and confirms the gain. Look at
   `rheed.camera.image`: a good pattern for this position? If not, stop and say so.
10. `perform_preablation` with `move_mask_to_block_position: false` (the mask is already
    over the position). Wait.
11. `start_storage`; it must return `ok`.
12. `perform_deposition`. **Snapshots: chamber + RHEED `depo_start`, RHEED `depo_mid`,
    chamber + RHEED `depo_end`.** Stop on anything in SKILL.md §4.
13. `end_storage`, then `finish_experiment_record` with the measured pressure and laser
    power, `is_pixel: true` and the `pixel_index` from step 4.
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
16. `cool_down` at the substrate's ramp rate in the right atmosphere.
    **Snapshot: chamber `cooled`.**
17. Report: `list_samples` for the substrate (every grown position should say so), plus
    what each snapshot showed.
