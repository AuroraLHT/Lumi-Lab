# Known chamber problems

| symptom | likely cause | what to do |
| --- | --- | --- |
| A mask, RHEED or target move fails after ~60 s | The motor holding lock is released (`is_motor_free` says free), usually after a power cut. Moves are refused until it is re-engaged. | Ask a person to press MOTOR ENABLE on the chamber controller, then retry the move. |
| Only the mask will not move; everything else does | The sample holder is too low, and the interlock blocks the mask so it cannot hit the holder. | Ask a person to check the holder and raise it to the right height. |
| A call hangs, then times out (~120 s) | An MI completion message was lost. | Do not re-issue the move or the deposition. Read the state back first (`get_current_mask_position`, `get_current_log`) to see whether it ran, and tell a person. |
| A tool call times out at once | Its node is down. | `system.registry.list_nodes`. |
| A move is refused: "... is still moving the chamber" | Another move (or a deposition, pre-ablation or auto-alignment) is still running. Moves run one at a time. | Wait for it to finish, read the position back, then decide whether the move is still needed. Do not loop on the refusal. |
| `to_temperature` is refused | The heating laser is off. | `initiate_heating_laser` first. |
| Chamber readings stop changing | PASCAL stopped writing its log. | `check_logging_alive`; if the log is dead, `start_mi_logging`, or ask a person. |
| The mask is off centre after alignment | The alignment algorithm got it wrong, or the motor is not energised (`is_motor_free` says free). | Check `is_motor_free` first. Then re-run `auto_align_center_mask`, or align by hand with a person (`begin_align_center_mask`). |
| The RHEED pattern disappears during a deposition | The RHEED gun shut down, from arcing or a burnt-out filament. | Let the current deposition finish; do not start another. Ask a person to fix the RHEED, and resume only once they say it is working. |
| Every operation is refused with "read_only" / "--allow-operations" | pld-service was started on the hardware read-only. | Ask a person to restart it with `--allow-operations`. Do not retry. |
| Operations are refused because another client holds control | Someone is driving from the REA GUI. | `check_mi_mode` says `busy` and who; wait for them, or ask. Never take control by force. |
| A laser op is refused with "the laser's other port has control (CM=1)" | The second PC holds the excimer's settings port (CM=1). | Ask the person whether to `take_laser_control`; never take it unasked. Nothing moves CM back at exit -- `release_laser` hands it back. |
| A deposition fails with "stopped by request: fired N of M pulses" | Someone (the REA GUI's STOP, `stop_deposition` or `stop_fire`) stopped the pulse train. | Stop the run and report how many pulses were fired. Do not re-fire or go on to the next step. |
| A `chamber.pld` operation is refused (ok=false) | pld-service enforces the chamber's interlocks and limits itself; the message says which. | Report the reason. Do not retry around it. |
| Every command hangs and times out, though the nodes are up and the log is alive | (legacy MI driver only) PASCAL is not in MI Mode, so it never picks the scripts up. | `check_mi_mode`; if `off`, ask a person to switch PASCAL to MI Mode. |
| The RHEED is down for maintenance | (planned) | Grow without it: `start_storage` with `save_frame`, `save_ai` and `save_integration` `false` (the chamber log is still recorded), no RHEED snapshots, no substrate-pattern check. Say in the report that the growth has no RHEED record. |
| A chamber snapshot shows a target, not the sample | The carousel is not at Clear or Monitor; the target blocks the camera's view. | `get_current_target`. Expected during a deposition; otherwise move to Clear before looking. |
| The laser energy reads low | Misaligned optics, a coated laser window, or old laser gas. | Ask a person to check all three. Do not raise the energy setting to compensate. |
