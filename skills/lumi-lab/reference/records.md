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
- **[hard]** `finish_experiment_record` gets the **measured** pressure and laser power
  (read back / reported by the person), never the requested values.
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
