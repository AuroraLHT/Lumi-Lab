# Known chamber problems

| symptom | likely cause | what to do |
| --- | --- | --- |
| A mask, RHEED or target move fails after ~60 s | The motor holding lock is released (`is_motor_free` says free), usually after a power cut. Moves are refused until it is re-engaged. | Ask a person to press MOTOR ENABLE on the chamber controller, then retry the move. |
| Only the mask will not move; everything else does | The sample holder is too low, and the interlock blocks the mask so it cannot hit the holder. | Ask a person to check the holder and raise it to the right height. |
| A call hangs, then times out (~120 s) | An MI completion message was lost. | Do not re-issue the move or the deposition. Read the state back first (`get_current_mask_position`, `get_current_log`) to see whether it ran, and tell a person. |
| A tool call times out at once | Its node is down. | `system.registry.list_nodes`. |
| `to_temperature` is refused | The heating laser is off. | `initiate_heating_laser` first. |
| Chamber readings stop changing | PASCAL stopped writing its log. | `check_logging_alive`; if the log is dead, `start_mi_logging`, or ask a person. |
| The mask is off centre after alignment | The alignment algorithm got it wrong, or the motor is not energised (`is_motor_free` says free). | Check `is_motor_free` first. Then re-run `auto_align_center_mask`, or align by hand with a person (`begin_align_center_mask`). |
| The RHEED pattern disappears during a deposition | The RHEED gun shut down, from arcing or a burnt-out filament. | Let the current deposition finish; do not start another. Ask a person to fix the RHEED, and resume only once they say it is working. |
| The laser energy reads low | Misaligned optics, a coated laser window, or old laser gas. | Ask a person to check all three. Do not raise the energy setting to compensate. |
