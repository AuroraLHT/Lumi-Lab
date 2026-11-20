# Keeping the growth database honest

The database (`growth.db`) is what later optimisation is trained on. A wrong row costs
more than a missing one. Background: [Sample tracking](https://github.com/AuroraLHT/Lumi-Lab/blob/main/docs/SAMPLE_TRACKING.md).

## What is in it

- **sample**: what exists. Substrate → position (→ cleaved piece). Created at
  `register_substrate`, one per position.
- **step**: what happened, recorded automatically by the experiment node, with who
  asked (`actor`). Calls through MCP are credited to `mcp` or `mcp:<user>`. The layer
  stack is *derived* from successful, non-dryrun deposition steps. There is no table to
  edit it.
- **experiment**: one row per growth, written by `finish_experiment_record`.
- **record**: a storage recording (RHEED frames, log), linked to an experiment.
- **measurement**: a result attached to a sample: a RHEED growth metric, XRD, AFM, PFM,
  transport, ...

## Rules

- **[hard]** Never make a row say something that did not happen. To fix a mistake use
  `update_*` (the value was entered wrong) or `retire_*` (the row should not exist:
  a dry run logged as real, a duplicate). Retired rows stay in the database, hidden.
- **[hard]** `finish_experiment_record` gets the **measured** pressure (read back),
  never the requested value, and the laser energy `begin_set_laser_power` set at the
  meter. The driver adds the excimer energy and the day's laser-test ratio it was set
  from, and the pyrometer's emissivity.
- **[hard]** Name the substrate when you register it. If the project has no naming
  convention you know of (from the person, or from earlier substrates in the same
  project via `list_substrates`), **ask the person for the name** before
  `register_substrate`. Do not make one up and do not leave it empty. With a known
  convention, propose the next name (e.g. `HZO-YSZ-03` → `HZO-YSZ-04`) and let them
  confirm. Pass it as `register_substrate(substrate_name=…)`: that names the substrate
  record (what the GUI shows), the root sample and every position sample
  `<name>-p<index>` in one go. Without it the positions fall back to
  `<material>-p<index>` (`YSZ-p0` on every YSZ substrate). Renaming later does **not**
  propagate: `update_substrate(substrate_name)` changes only the substrate record; also
  `update_sample` the root sample and each position sample.
- **[hard]** Do not `finish_current_pixel` / `finish_substrate` on a position that was
  not really used. If it was done by mistake, `reopen_position`.
- **[thumb]** Attach a measurement to the sample (`add_measurement`) as soon as there is
  one, with `source` saying where it came from, and `record_id` / `step_id` when it
  came from a specific recording or step.
- **[thumb]** Attach the instrument's raw file (`attach_measurement_file`) next to the
  number, so the number can be checked later.
- **[thumb]** There is no fixed naming convention for projects, recordings or
  measurement `kind`s. Ask the user what to call a new project, and before inventing a
  measurement `kind`, check `list_measurements` for one already in use for the same
  thing and reuse it.
- **[hard]** A mistake in a row someone else made: propose the `update_*` / `retire_*`
  and wait for a person to agree. Rows you made in this session you may correct
  yourself; say so in your report.
