# Sample tracking

Status as of 2026-09-03. Branch `sample-tracking`. Both notebooks are now confirmed
end to end against the simulator — see [Where this stopped](#where-this-stopped) for
what that run found and the remaining to-do.

---

## The idea

The growth database recorded what was **deposited**. It had no row for what
**exists**, and nothing recorded the process itself. The SingleDepo run puts SRO,
then LSMO, then HZO on one substrate position — one specimen with a three-layer
stack — and the database held that as three unrelated `experiment` rows sharing a
`substrate_id`. Everything downstream (which positions are spent, what is on each
one, what it measured) lived in a Python object that died with the kernel, or in a
pickle beside the notebook.

The schema was flat because it was trying to be three different things at once.
Pulled apart, each has an obvious shape:

| | what it is | who maintains it |
| --- | --- | --- |
| **`sample`** | what exists — a tree: substrate → position → cleaved piece | you |
| **`step`** | what happened — a chain per chamber session | the system |
| **`measurement`** | what it measured — the BO metric, XRD, AFM, transport | you, via one op |

One correction that shaped the design: **matter cannot branch.** You cannot
un-deposit a layer, so per specimen the history is strictly a chain. Two attempts
that share `SRO → LSMO` and diverge at HZO pressure are on *different* pixels. A
tree over *plans* would be real, but a tree over what physically happened is not —
so `step.parent_step_id` chains, and nothing branches.

### Decisions taken (2026-09-02/03)

| question | decision |
| --- | --- |
| Journal every op, or declare which? | Declare, via `Op.journal`. Log polling at ~1/s is fine for throughput but would bury the rows that describe a growth. |
| Where does the journal live? | Same `growth.db`. One writer; splitting is easy later. |
| Chain steps per sample or per session? | **Per chamber session** — alignment, gas changes and warm-ups are real steps belonging to no specimen. `sample_id` tags the subset that touched one. |
| A protocol/plan tree? | **Dropped.** Not necessary. |
| Record who did what? | **Yes** — `actor` on the request envelope. |
| Order of work | Infrastructure first, then rewrite the notebooks on top of it. |

---

## What was built

### Phase 0 — resume no longer rewinds (`1153d43`)

`resume_substrate` rebuilt a `Substrate` with `current_position_id = 0` and an empty
accessed set, so a campaign resumed after a node restart handed back an
already-grown pixel and deposited on top of it. The history was always in
`experiment.pixel_location`; `GrowthDB.get_used_pixel_indices` reads it back and
`Substrate.restore_progress` re-applies it.

The pre-refactor notebook driver had the same omission (`OpCode/src/manager.py:377`),
which is why it carried a `collector` pickle and a hand-managed
`local_experiment_counter` beside the database. That workaround is now unnecessary.

### Phase 1 — the step journal (`1153d43`)

`ExperimentHandler._start_task` already minted a task id, stamped a start time, held
the typed request as `detail`, and built the `{"ok": ...}` outcome — then pushed it
to subscribers and dropped it. That is a step record, discarded. Two hooks persist it:

- **`MqServer._handle_request`** (`src/lumi/base/mq/server.py`) — ops that finish
  inside one RPC: heater on, gas in, target select, alignment, a human answering a
  gate. One row opened and closed around the handler call.
- **`ExperimentHandler._start_task`** (`src/lumi/experiment/handlers.py`) — the
  long-running ones, so a ramp's row spans the ramp and not the ack.

A long-running op that raises *before* its task starts (`to_temperature` with the
heating laser off) is journaled by the dispatch, since the handler opened no row for
it — otherwise a refused ramp left no trace at all.

`Op.journal` is deliberately **not** in `_op_json`, so toggling it leaves the contract
hash — and the generated frontend client — untouched. It is server-side policy, not
protocol.

Only the experiment node gets a journal: `growth.db` is on the server host while
`pascal` and `rheed` run on the instrument PC. The hook is generic, so shipping step
events to a collector later needs no change to the base server.

### Phase 2 — the sample tree (`1153d43`)

One row per specimen, materialised at `register_substrate` rather than at first
growth, so "which positions are spent" is a query and a step has something to attach
to before any deposition.

The layer stack is **not** a table. `GrowthDB.get_layer_stack` derives it from the
deposition steps that succeeded and were not dryruns, so it cannot drift from what
was actually run.

### Phase 3/4 — reachable over the contract (`3b99f81`)

`growth.db` lives with the experiment node, so a notebook elsewhere needs the bus.
Five ops, replacing the CSV and the pickle:

```
list_samples       which positions exist and which are spent
get_sample         one sample plus its derived layer stack
sample_history     the step journal, by sample or by session
add_measurement    attach a result
list_measurements  the GP training set, in one call
```

`list_measurements` resolves each sample's growth conditions server-side, so building
a training set is one round trip rather than one per point.

Contract hash moved to `983c936b3e71f4fb`; `web/src/generated/lumi.ts` and
`schemas/contract.json` were regenerated. **The frontend needs the regenerated
`lumi.ts`.**

### `lumi.opt` — the BO layer (uncommitted at pause)

Ported from `~/HZO_PLD/OpCode/src/{gp,preprocess}.py`, the code that ran the real
autonomous campaigns. The maths is unchanged — changing it while porting would have
invalidated the campaign history it produced.

`GrowthCampaign` replaces `GPManager`. The one structural change: observations come
from `list_measurements` over the contract, not from `gp_db/<project>.csv`. A
campaign can now be resumed on a different machine, and the GP is fitted on what the
lab actually recorded.

Needs the new `opt` extra (`uv sync --extra opt` — torch, gpytorch).

### Notebooks (uncommitted at pause)

`notebooks/SingleDeposition.ipynb` and `notebooks/BODeposition.ipynb`, both written
sim-first with real-hardware values in marked config cells.

They are **generated** from `_single_deposition.py` / `_bo_deposition.py` by
`build_notebooks.py`. Notebooks are JSON and miserable to review or merge; the plain
Python source is the thing to edit. After changing a source file:

```bash
uv run notebooks/build_notebooks.py
```

---

## Bugs found by running it

Each of these was caught by driving the simulator, not by the unit tests, and each is
fixed:

1. **`resume_substrate` rewound to pixel 0.** Phase 0 above.
2. **A refused long-running op left no journal row.** The handler never reached
   `_start_task`, and the dispatch was skipping handler-owned ops entirely.
3. **`actor` was lost on long-running ops.** The dispatch reads it off the headers,
   but `_start_task` journals several frames deeper. Carried by a contextvar
   (`src/lumi/base/mq/context.py`).
4. **A deposition step recorded `target_id: "C"` and no material.** The carousel's
   slot→material map is chamber config that changes when targets are swapped, so read
   back after the next swap that step names the wrong material. Resolved at journal
   time now, which freezes it.
5. **A dryrun deposition counted as a layer.** It fires no laser: the step happened,
   the film did not.
6. **`growth_conditions` returned the step params *or* the experiment row.** Neither
   is sufficient — the row holds the measured pressure and laser power a GP regresses
   on, the step holds the material and rate. The row is the base, the step overlays it.
7. **A deposition step did not record the temperature and pressure it ran at.** Those
   are chamber state, not request fields, so in a dryrun (no
   `finish_experiment_record`) the GP had *no* conditions and skipped every point —
   the BO loop silently never left its random-seed phase. Read from the chamber when
   the step is journaled now, so the step stands on its own.
8. **`to_current_pixel` failed almost every time it was called unattended.** It issues
   a mask move and a RHEED move back to back. The mask move waits for MI completion,
   but `Motor free` comes from the *chamber log*, which PASCAL rewrites about once a
   second — so for up to a log tick after the controller says the move finished, the
   log still reports the motor busy, and the second move was refused.
   `BaseExperimentManager._await_motor_free` waits (bounded) instead of refusing.
9. **`ExperimentSession`'s docstring had `image()`'s return backwards** — it is
   `(meta, frame)`, not `(frame, meta)`. A pre-existing doc bug in the example
   written for notebook authors.

---

## Where this stopped

**The last thing attempted was executing `BODeposition.ipynb` end to end against the
simulator, and WSL crashed during it — twice.** The crash is *not* understood and is
not attributed to this work; it took down the whole VM, not just the notebook.

Verification status:

| | status |
| --- | --- |
| `pytest -q -m "not broker"` (399 tests) | ✅ passing |
| `lumi-codegen --check` | ✅ in sync |
| Journal recording a full dryrun growth | ✅ verified — 10 chained steps, real durations, actor, zero rows from 40 read polls |
| Refused ramp journaled / actor on a 16.4 s ramp | ✅ verified live |
| The five new contract ops over the bus | ✅ verified live |
| `SingleDeposition.ipynb` end to end | ✅ executed cleanly — 3 layers, all steps journaled |
| Motor fix across six consecutive pixel moves | ✅ verified — ~0.2 s each, previously failed on the first |
| `BODeposition.ipynb` end to end | ✅ **confirmed 2026-09-03**, on a different machine. All 7 positions on a `pixel_spacing=1.0` substrate grown, journaled and measured; session closed cleanly. See caveat below. |

### `BODeposition.ipynb` completion run (2026-09-03)

Executed headlessly (`jupyter nbconvert --execute`) against the simulator, `DRYRUN =
True` as shipped. 2 random-seed growths then 5 GP-proposed ones, all 7 of the
substrate's positions used, no exception, `session closed` printed. Uncovered one
real gap on the way (fixed, see below) and one thing to know before trusting the
proposals it made:

- **`matplotlib` was an undeclared dependency.** Both notebooks `import
  matplotlib.pyplot` but nothing in `pyproject.toml` installed it — the previous
  run only worked because a stale environment had it transitively. Fixed: new
  `notebooks` extra (`uv sync --extra notebooks`), `notebooks/README.md` updated.
- **Caveat, not a bug: under `DRYRUN = True`, the GP's x and the score's x are
  different points.** `score_growth` computes the metric from the *proposed*
  conditions. But `to_temperature` is skipped outright when `is_dryrun` (by
  design — see `notebooks/README.md`), and pressure is *always* a printed
  `"[Manual] Set Pressure..."` instruction with no simulator-side actuation (the
  contract has a real `set_pressure` op; `recipes.perform_pixel_deposition` never
  calls it — on real hardware a human enacts the printed instruction, in the
  simulator nothing does). So `growth_conditions()` — which is what
  `list_measurements` feeds the GP — records the chamber's undisturbed idle state
  (~160 °C, ~5e-9 Torr in this run) for every point, regardless of what was
  proposed. The run in the executed notebook shows exactly this: `campaign.db`'s
  recorded `temperature`/`pressure` columns are nearly constant and far outside
  `AXES`'s configured range, while the metric varies with what was actually
  proposed. The loop's proposals after seeding collapsed toward one corner of the
  search box — consistent with the GP fitting a near-single training point in
  x-space with scattered y, not with a fitting bug.
  This means **the run confirms the loop's mechanics (bookkeeping, gates,
  journaling, GP wiring, convergence check) but not its optimization behaviour**
  — that needs either `DRYRUN = False` (which drives temperature for real; pressure
  would still need a human, or an `auto_input` hook added to call `set_pressure`
  for an unattended simulator run) or accepting dryrun's x/y decoupling as
  out of scope for judging the proposals themselves.

### To do

- [x] ~~Run `BODeposition.ipynb` to completion~~ — done 2026-09-03, see above.
- [ ] Decide whether the BO loop should catch a per-iteration failure and stop
      cleanly rather than raising. Measurements are already durable in the database,
      so nothing is lost either way — but an autonomous loop that dies on iteration 7
      of 20 should say so, not traceback.
- [ ] Wire the real `analyze_rheed_video` into `score_growth` on the detection host.
      The notebook currently falls back to a synthetic landscape and says so.
- [ ] `finish_experiment_record` is skipped in dryruns, so a dryrun growth has no
      `experiment` row. Fine now that steps carry their own conditions, but worth
      deciding whether that is intended.
- [ ] Consider `journal=True` on the chamber/rheed contracts once step events can
      reach a collector across hosts (see the note in `server.py`).
- [ ] The `sample` table has `parent_sample_id` and `state` but nothing yet cleaves a
      sample or moves it past `grown`. Ops for that when dicing needs tracking.
- [ ] Regenerate the frontend against contract `983c936b3e71f4fb`.

### Known pre-existing issues this ran into

- **MI completion is a single lossy message** (`docs/TODO.md`). Unbounded waits mean
  a lost completion update parks the op until the node restarts. Hit once during BO
  verification as a 120 s client timeout. Not caused by this work, but it is the main
  reliability limit for a long unattended loop.
- `tests/api/test_bridge.py` — 6 errors ("rheed node never..."). **Also fails on
  `main`**, unrelated to this branch.

---

## Picking it up elsewhere

```bash
git fetch origin && git checkout sample-tracking
cp cfg/settings.example.toml cfg/settings.toml     # machine-local, not tracked
cp cfg/.secrets.example.toml cfg/.secrets.toml

uv sync --all-extras          # or: --extra experiment --extra opt --extra api
uv run pytest -q -m "not broker"

docker run -d --name lumi-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management
scripts/start_simulation.sh --chamber-speed 30 --with-experiment
```

Then in a second terminal, `notebooks/SingleDeposition.ipynb` first (it is the
verified one), then `BODeposition.ipynb`.

To point the growth database somewhere scratch rather than `cfg/growth.db`:

```bash
export DYNACONF_EXPERIMENT__GROWTH_DB_PATH=/tmp/sim-growth.db
```

Inspecting what the journal recorded:

```sql
SELECT step_id, parent_step_id, sample_id, kind, ok,
       ended_at - started_at AS dur, actor
FROM step ORDER BY started_at;
```
