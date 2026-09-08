# Notebooks

Ported from the v1.0 notebooks in `~/HZO_PLD/OpCode/`, onto the current contract-driven
API. Both are written **sim-first**: they run end to end against
`scripts/start_simulation.sh` as written, with the real-hardware numbers kept in one
clearly marked config cell each.

| notebook | what it does | ported from |
| --- | --- | --- |
| `SingleDeposition.ipynb` | a layered growth on one position — bottom electrode → interface → functional layer | `UMDSingleDepoExperiment.ipynb` |
| `BODeposition.ipynb` | closed-loop Bayesian optimisation over growth conditions, one position per iteration | `UMDAutonomousExperiment.ipynb` |

## Running them

```bash
docker run -d --name lumi-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management
scripts/start_simulation.sh --chamber-speed 30 --with-experiment
```

Both default to `DRYRUN = True`, which exercises all the bookkeeping, gates and
journaling without firing the laser or ramping. A **non-dryrun** run additionally
needs the detection node (`--with-detection`): `start_storage` always requests
`save_ai`, and the storage node refuses to record when a requested source is off the
bus.

Both notebooks plot with matplotlib, so either one needs the `notebooks` extra:

```bash
uv sync --extra notebooks          # matplotlib
```

`BODeposition.ipynb` additionally needs the `opt` extra:

```bash
uv sync --extra opt          # torch, gpytorch
```

To switch either notebook to real hardware, edit `HOST`, set `DRYRUN = False`, and
replace the values in the marked parameters cell — the production numbers are in the
comment on each line.

## Editing them

**Edit the `.py`, not the `.ipynb`.** Each notebook is generated from a plain Python
source file beside it:

```
_single_deposition.py  ->  SingleDeposition.ipynb
_bo_deposition.py      ->  BODeposition.ipynb
```

```bash
uv run notebooks/build_notebooks.py
```

Notebooks are JSON, so a one-line change to a cell shows up in review as a rewritten
blob and merges badly. The sources are ordinary lists of `(kind, text)` cells, and the
generated `.ipynb` carries no outputs or execution counts — a committed notebook is a
diff of its source and nothing else.

## What is verified

`SingleDeposition.ipynb` has been executed end to end against the simulator: three
layers grown, every step journaled with its actor, the derived layer stack read back.

`BODeposition.ipynb` has also been executed end to end: 2 random-seed growths then 5
GP-proposed ones, all positions on the substrate used, no exception. That confirms the
loop's mechanics (bookkeeping, gates, journaling, GP wiring) but **not** its
optimisation behaviour under `DRYRUN = True` -- `to_temperature` is skipped outright in
dryrun and pressure is always a printed manual instruction with nothing to act on it in
the simulator, so the conditions the GP trains on don't match what was proposed. See
`docs/SAMPLE_TRACKING.md` for the detail and the remaining to-do.

## `analyze_rheed_video`

The BO notebook's `score_growth` is where the real RHEED analysis goes. It needs the
detection host's `rhana`/`mmdet` install and a recorded HDF5, neither of which the
simulator has, so against the simulator it falls back to a synthetic landscape and
says so in its output. Swap it for `analyze_rheed_video(...)` on the detection host.
