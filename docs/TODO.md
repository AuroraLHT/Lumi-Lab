# TODO

Known gaps, with enough context to pick them up cold. Ordered roughly by how much
damage they can do, not by effort.

## Aborting a running MI script

**There is no way to stop a script once the controller has loaded it.** The PASCAL
firmware exposes no cancel through the MI file protocol — writing, renaming or deleting
the script/assist files does not interrupt an execution that is already running.

What exists today is misleading and should be treated as a bug, not a feature:

- `$stop` (`src/lumi/pascal/mi_mode.py:339`) clears the pending queue and calls
  `MIModeExecution.stop()` (`mi_mode.py:151`), which unlinks the script and assist
  files, rewrites them empty, and sets `is_stopped = is_execution_finished = True`.
- That releases whoever is waiting on the execution and marks it finished **locally**.
  The chamber does not stop. On a `Trigger Laser` the laser keeps firing to the last
  pulse while the node reports the execution as stopped.

So `$stop` is only honest for scripts still sitting in the queue. Until the firmware
grows a real abort, anything above this layer that offers "cancel" is lying.

Consequences to keep in mind meanwhile:

- `lumi.experiment.handlers._start_task` (`src/lumi/experiment/handlers.py:209`) discards
  the `asyncio.create_task` handle and the experiment contract has no stop/abort op, so
  a long-running task cannot be cancelled even in software.
- A second `_start_task` overwrites `self._current_task`, and the first task's runner
  then clears it on exit — so two overlapping tasks leave the UI's `current_task` wrong.

## MI completion is a single lossy message

`MiCommandRunner.execute` (`src/lumi/experiment/mi.py:55`) waits for one PUBSUB update on
an exclusive, auto-delete, `no_ack=True` queue (`src/lumi/base/mq/queues.py:158`). Nothing
is durable or redelivered. It can be lost by: a broker reconnect on the experiment node,
`update_queue` being full on the chamber node (`mi_mode.py:329` skips the put silently),
or the chamber node restarting mid-script.

There is no recovery path by polling, because `clean_up_mi_execution` (`mi_mode.py:418`)
pops finished executions out of `executions`, so `list_execution` cannot distinguish
"completed", "aborted" and "never registered". Retaining finished executions keyed by
uuid (bounded) would make a poll-based fallback possible.

## Bound the MI wait by stall rather than by duration

The MI completion wait is `experiment.mi_command_timeout` (or `nodes/experiment.py
--mi-timeout`), and it now **defaults to 0 — no deadline**. That matches the controller:
a command without an explicit `(Nowait)` stays RUNNING until the firmware confirms the
physical goal, so `Temperature Set 700` completes when the substrate reaches 700, and a
superlattice loop runs for hours. Any bounded default fails those.

It is a blunt instrument, though. Unbounded means a *lost* completion update is
indistinguishable from a long script, and the wait can then only be cleared by
restarting the node. The better shape is to bound **stall** instead of duration: a
`Trigger Laser` script is alive while `LaserPuls` advances in the chamber log, so a
script that stops progressing can be failed quickly while one that runs for three hours
is never touched. That is the fix that would let the deadline come back.

Two notes for whoever builds it:

- Poll `chamber_log.log()` rather than subscribing to the log stream. Streaming is gated
  by the `start`/`stop` control verb (`src/lumi/base/mq/server.py:467`) and that toggle
  is global, so a stream-based heartbeat goes silent whenever the last viewer stops the
  feed. The manager already polls this way everywhere else.
- It only covers scripts that move something observable. A script that is deliberately
  idle (a soak, a `Wait`) shows no movement in any column, and for those a duration bound
  is still the only option.

Related: a timeout does **not** stop anything. On expiry `execute` raises, the laser runs
to the last pulse, and the task reports `ok=False` for a growth that actually happened.
Whatever replaces this should not pretend otherwise until there is a real abort.

## ~~`_warm_up_to_pid_limit` cannot reach the PID-engage threshold~~ (resolved)

Resolved by a re-measure, not a code change: the heater calibration anchor moved from
7 A → 160 °C to **7 A → 220 °C** (`[pascal.sim]` in `cfg/settings.toml`). PLDconfig's
`[PIDsettings] LDmin = 8.5`, which `_warm_up_to_pid_limit` ramps to, is now ~271 °C —
clear of `experiment.bounds.temperature_pid_engage_threshold = 220`, so a growth from a
cold chamber gets through the warm-up.

`test_the_configured_warm_up_target_clears_the_pid_engage_threshold` now pins it from
the other side: if a future re-measure moves the anchor back down, the test fails rather
than the stall resurfacing four minutes into a growth.

One consequence to know about: 160–203 °C is a dead band. The pyrometer floor is 160,
but the coldest the diode can hold is the calibration line extended to `ld_min`, ~203 °C.
A setpoint in between drives the current under the lasing minimum, so the diode switches
off and the substrate coasts to the floor — which is what makes `cool_down`'s 160 °C
setpoint behave, but means `HT Temp moni` will never settle on a setpoint in that band.

## A dead chamber-log reader is invisible in the capability's state

Fixed the dangerous half: `LogReader` no longer dies on an unparseable row, and the
`log` op now raises when the reader thread is not alive instead of serving its last row
forever. What is still missing is *observability* — `ChamberLogState` reports
`is_running: True, error: None` regardless of whether the reader thread is alive, so the
monitor and the UI cannot show a degraded chamber until something calls `log` and gets
an exception.

The obvious fix is `reader_alive` / `skipped_rows` fields on `ChamberLogReadout`, which
is why it has not been done: that is a contract change, so it bumps the hash and forces
a frontend redeploy. Worth batching with the next contract change rather than spending a
redeploy on it alone.

Background: this froze a running chamber for 1h46m. The reader caught a partially
written line, `csv.DictReader` padded the missing columns with None,
`ast.literal_eval(None)` raised, and the exception killed the thread. `get_log()` then
served the same row forever — `Motor free` stuck at whatever it had been — so
`is_motor_free()` failed every subsequent mask move with "motor is not free" while the
node reported itself healthy.

## The recipes ignore whether a long-running task succeeded

`recipes._wait_for_task` returns as soon as `current_task` clears, which happens whether
the op finished or raised. `perform_single_deposition` does not look at the result, so a
failed `to_temperature` is followed by the full deposition at whatever temperature the
substrate actually reached — a ruined sample with a provenance row that looks fine.

The reporting half is fixed (`ExperimentSession.last_task_result`, and `_wait_for_task`
now returns it), and `scripts/demo_experiment.py` fails loudly on it. Whether the recipe
itself should abort is a behaviour change for existing notebook users, so it is left
alone deliberately.

Related: nothing calls `initiate_heating_laser`. `perform_single_deposition`'s
`[Manual] Set excimer laser to ON mode` is the *ablation* laser; the heating diode is
never turned on, so `_warm_up_to_pid_limit` drives current into a dark diode and the
substrate never leaves the pyrometer floor. Either the recipe should initiate the heater
or it should document that the operator does it first.

## Simulator numbers that are still guesses

Most of the chamber model is now anchored to a measurement. These are not:

- **`gate_travel_time` = 1.0 s** — the deposition gate does travel, but no figure was
  given, so it is assumed equal to the sample shutter.
- **`tg_z_speed` = 2.0 mm/s** — `TG-Z` is a logged column that **no command in
  `lumi.pascal.command` drives**, so nothing moves this axis today. The speed is a
  placeholder for when something does. (`Set Sample Position` is *not* this axis — it is
  an absolute sample angle.)
- **`mfc_tau` = 1.5 s** — how fast the MFC itself reaches a commanded flow, as distinct
  from `pressure_tau` (26 s), which is the chamber's pump-limited response and *is*
  anchored to the measured ~120 s for 1e-7 → 1e-1 Torr.
- **`process_gauge_2` = 1.04** — held at its idle reading; its relationship to the other
  gauges could not be inferred from the recorded log.

## Viewer control-verb access

Control verbs (`start`/`stop` streaming) are global and reachable by any connected
viewer, so a read-only user can toggle a feed off for everyone. Nuisance-level, but it is
an authorization gap in the direct-to-broker model.
