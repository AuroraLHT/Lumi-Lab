"""Source for BODeposition.ipynb. See build_notebooks.py."""

from __future__ import annotations

MD = "markdown"
CODE = "code"


def cells() -> list[tuple[str, str]]:
    return [
        (MD, """
# BO Deposition

Closed-loop Bayesian optimisation over growth conditions: propose &rarr; grow &rarr;
measure &rarr; refit, one substrate position per iteration. Ported from
`UMDAutonomousExperiment.ipynb`.

The loop itself is unchanged -- same GP, same UCB/max-uncertainty acquisition
functions, same convergence check, ported in `lumi.opt` from the code that ran the real
campaigns. What changed is where the campaign's memory lives:

| v1.0 | now |
| --- | --- |
| `GPManager` + `gp_db/<project>.csv` | `GrowthCampaign`, reading `list_measurements` |
| `collector` pickle, `local_experiment_counter` | `list_samples` -- positions are rows |
| `substrate.has_lsmo` monkeypatched on | the derived layer stack |
| metric kept beside the notebook | a `measurement` row on the sample |
| `PixelExperimentManager.perform_experiment` | `recipes.perform_pixel_deposition` |

That the training set comes from the growth database and not a local CSV is the
substantive change: a campaign can now be resumed from a different machine, and the GP
is fitted on what the lab actually recorded.

**Runs against the simulator as written:**

```bash
scripts/start_simulation.sh --chamber-speed 30 --with-experiment
```

Needs the `opt` extra: `uv sync --extra opt`.
"""),
        (CODE, """
import asyncio
import uuid

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lumi.contracts.payloads.experiment import (
    AddMeasurement,
    ListMeasurements,
    ListSamples,
    RegisterProject,
    RegisterSubstrate,
    SampleId,
    TargetId,
    ToTemperature,
)
from lumi.experiment import recipes
from lumi.experiment.client import ExperimentSession
from lumi.opt import (
    AcquisitionFunctionMaximumUncertainty,
    AcquisitionFunctionUCB,
    ActiveLearningWrapper,
    ConvergenceChecker,
    GrowthCampaign,
)
"""),
        (MD, """
## Connect
"""),
        (CODE, """
HOST = "localhost"
ACTOR = "hliang16"
DRYRUN = True          # True: the loop runs, the laser does not

exp = await ExperimentSession.open(host=HOST, actor=ACTOR)
print("deps:", (await exp.driver.get_state()).deps_available)
"""),
        (MD, """
## The search space

**&larr; Edit this cell for a real campaign.** Each axis is a growth condition, named
exactly as it comes back in `list_measurements(...).conditions` -- which is how what
the GP regresses on and what the chamber was actually asked for are kept from drifting
apart.

`do_log10` matters on pressure: it spans nearly two decades, and scaling it linearly
gives a length scale dominated by the top of the range.
"""),
        (CODE, """
PROJECT = "UMD_AI_HZO_BO"
TARGET_SLOT = "D"          # the functional layer's carousel slot
N_RANDOM = 2               # random seed points before the GP takes over
NUM_PULSE = 120            # real: 500 (~9 nm of HZO)

AXES = {
    # simulator-friendly ranges; the real campaign's are in the comments
    "temperature":           {"min": 280.0, "max": 340.0, "do_log10": False, "num": 31},
    "pressure":              {"min": 50e-3, "max": 150e-3, "do_log10": True,  "num": 31},
    "laser_repetition_rate": {"min": 1.0,   "max": 5.0,   "do_log10": False, "num": 31},
    # real: temperature 500-700 degC, pressure 3e-3 - 1e-1 Torr (log10),
    #       laser_repetition_rate 0.5-10 Hz, num=101 per axis
}

LASER_POWER = 1.2          # real: 88 mJ
RAMP_RATE = 20.0
"""),
        (MD, """
## The optimiser

Two acquisition functions, exactly as the production campaign ran them: UCB with
`beta=3` to exploit, and maximum-uncertainty to break out when UCB converges on a local
maximum. The constant mean at 0.5 and the length-scale interval are the values those
runs used -- a shorter length scale under-fits and yields no useful uncertainty.
"""),
        (CODE, """
import gpytorch

mean_module = gpytorch.means.ConstantMean()
mean_module.constant = 0.5

al = ActiveLearningWrapper(
    acq_funs={
        "exploitation": AcquisitionFunctionUCB(beta=3),
        "exploration": AcquisitionFunctionMaximumUncertainty(),
    },
    gp_fit_iter=300,           # real: 2000
    gp_lr=0.1,
    gp_lengthscale=0.2,
    gp_lengthscale_constraints=gpytorch.constraints.Interval(0.05, 0.5),
    gp_mean_module=mean_module,
    is_percentage=False,
)

campaign = GrowthCampaign(
    al, AXES,
    metric_kind="rheed_metric",
    snapshot_folder=f"../run/bo_snapshots/{PROJECT}",
)

converged = ConvergenceChecker(
    x_columns=campaign.x_columns, y_column=campaign.y_column,
    patience=3, delta_x_propotional=0.02,
)

print(f"search grid: {len(campaign._test_x_raw)} points over {campaign.x_columns}")
"""),
        (MD, """
A band of one axis can be excluded -- the production campaign ruled out a pressure
window the chamber could not hold stably.
"""),
        (CODE, """
removed = campaign.forbid("pressure", 1e-4, 2e-3)
print(f"excluded {removed} grid points")
"""),
        (MD, """
## Register a multi-position substrate

One position per iteration, so `pixel_spacing` sets how many growths this substrate
can carry. Each becomes a `sample` row at registration.
"""),
        (CODE, """
project = await exp.driver.register_project(RegisterProject(
    project_name=PROJECT, description="Autonomous HZO growth-condition search.",
))

substrate = await exp.driver.register_substrate(RegisterSubstrate(
    materials="SrTiO3", orientation="001",
    width=10.0, height=10.0, thickness=0.5,
    pixel_spacing=1.0,   # more positions than N_RANDOM, or the GP never gets a turn
    substrate_name=f"STO-BO-{uuid.uuid4().hex[:6]}",
))
SUBSTRATE_ID = substrate.substrate_id

print(f"substrate {SUBSTRATE_ID}: {len(substrate.positions)} growable positions")
for p in substrate.positions:
    print(f"  index {p.index}: {p.position:+.1f} mm  accessible={p.accessible}")
"""),
        (MD, """
## Answering the recipe's prompts

`perform_pixel_deposition` has more human gates than the single-deposition recipe: as
well as the laser power meter it runs the mask-centre check and the RHEED gain loop,
each of which asks a different *kind* of question. Answering everything "y" would fail
the moment the gain loop asks for a number, so the provider below dispatches on the
prompt.

Swap in `recipes.default_value_provider` to answer them yourself.
"""),
        (CODE, """
RHEED_GAIN = 100.0     # what the gain loop should set, when unattended


async def auto_input(message: str) -> None:
    print(f"  [auto] {message}")


async def auto_value(prompt: str) -> str:
    lowered = prompt.lower()
    if "measured" in lowered:
        answer = str(LASER_POWER)          # the power meter reading
    elif "gain" in lowered:
        answer = str(RHEED_GAIN)           # a number, not a yes/no
    else:
        answer = "y"                       # aligned? / image good?
    print(f"  [auto] {prompt} -> {answer}")
    return answer
"""),
        (MD, """
## Seeding the campaign

The GP needs observations before it can propose. The first `N_RANDOM` iterations draw
uniformly from the search grid; after that `campaign.propose()` takes over.
"""),
        (CODE, """
async def wait_for_motor(timeout: float = 120.0) -> None:
    \"\"\"Block until the chamber's motors report free.

    `to_current_pixel` moves the mask and the RHEED gun, and refuses outright while a
    previous move is still running. The production notebook covered this with a manual
    "check motor free" gate before each growth; an unattended loop has to wait for it
    itself.
    \"\"\"
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if (await exp.driver.is_motor_free()).free:
            return
        await asyncio.sleep(1.0)
    raise TimeoutError(f"motors still busy after {timeout:.0f}s")


rng = np.random.default_rng(0)


def random_conditions() -> pd.Series:
    index = int(rng.integers(len(campaign._test_x_raw)))
    return pd.Series({
        c: float(campaign._test_x_raw[index, i])
        for i, c in enumerate(campaign.x_columns)
    })
"""),
        (MD, """
## Scoring a growth

In the lab this is `analyze_rheed_video` from the RHEED analysis stack, which needs the
detection host's `rhana`/`mmdet` install and a real recorded HDF5. It returns growth,
speed, quality and roughness components plus the combined `metric`.

Against the simulator there is no film to score, so the fallback below stands in a
smooth synthetic landscape -- enough to watch the loop converge on something. Swap
`score_growth` for the real analysis on the detection host.
"""),
        (CODE, """
try:
    from src.analysis import analyze_rheed_video   # the lab's RHEED analysis stack
    HAVE_ANALYSIS = True
except ImportError:
    HAVE_ANALYSIS = False

print("real RHEED analysis available:", HAVE_ANALYSIS)


async def score_growth(storage_name: str, conditions: pd.Series) -> dict:
    \"\"\"Return the metric dict for a finished growth.

    Replace the fallback with analyze_rheed_video(...) on the detection host:

        metrics, _ = await to_async(
            analyze_rheed_video,
            storage_path=STORAGE_ROOT / f"{storage_name}.hdf5",
            based_periodicity=90, crops=CROPS, version="v2", ...
        )
        return metrics
    \"\"\"
    if HAVE_ANALYSIS:
        raise NotImplementedError(
            "wire analyze_rheed_video in here -- it needs the recorded HDF5 and the "
            "crop/periodicity setup from the production notebook"
        )

    # Synthetic stand-in: a smooth optimum inside the search box, plus noise.
    t = (conditions["temperature"] - 310.0) / 30.0
    p = (np.log10(conditions["pressure"]) - np.log10(90e-3)) / 0.5
    r = (conditions["laser_repetition_rate"] - 2.8) / 2.0
    metric = float(np.exp(-(t**2 + p**2 + r**2)) + rng.normal(0, 0.02))
    return {"metric": metric, "growth": metric, "speed": 0.3,
            "quality": -0.1, "roughness": 0.7, "synthetic": True}
"""),
        (MD, """
## The loop

Propose &rarr; grow &rarr; score &rarr; record &rarr; refit, until the substrate runs
out of positions or the GP converges.

Two differences from the v1.0 loop worth pointing at. `campaign.refresh(exp.driver)`
reloads the training set from the growth database each iteration, so the GP sees every
measurement the lab has -- including ones added from another machine, or from a
re-analysis. And there is no `collector` to keep in step: the position that was grown,
what went on it and what it scored are all rows the system wrote itself.
"""),
        (CODE, """
history = []

for iteration in range(len(substrate.positions)):
    # --- 1. choose conditions
    await campaign.refresh(exp.driver, substrate_id=SUBSTRATE_ID)
    seeding = len(campaign.db) < N_RANDOM

    if seeding:
        conditions = random_conditions()
        mode = f"random seed {len(campaign.db) + 1}/{N_RANDOM}"
    else:
        conditions = campaign.propose("exploitation")
        is_converged, satisfied = converged(campaign.db, conditions[campaign.x_columns])
        if is_converged and satisfied:
            print(f"\\nconverged after {iteration} growths -- stopping")
            break
        if is_converged:
            # A local maximum: one round of pure exploration to break out. Same
            # escape the production campaign used.
            conditions = campaign.propose("exploration")
            mode = "exploration (escaping a local maximum)"
        else:
            mode = f"exploitation (GP mean {conditions['gp_mean']:.3f} "\\
                   f"+/- {conditions['gp_std']:.3f})"

    print(f"\\n=== iteration {iteration} -- {mode} ===")
    for c in campaign.x_columns:
        print(f"  {c:<24} {conditions[c]:.4g}")

    # --- 2. grow at the next free position
    await wait_for_motor()
    material = (await exp.driver.get_target_name_by_id(
        TargetId(target_id=TARGET_SLOT))).target_name

    ok, terminated, (storage_name, final) = await recipes.perform_pixel_deposition(
        exp,
        project_name=PROJECT,
        pressure=float(conditions["pressure"]),
        temperature=float(conditions["temperature"]),
        laser_power=LASER_POWER,
        laser_repetition_rate=float(conditions["laser_repetition_rate"]),
        target_id=TARGET_SLOT,
        target_material=material,
        num_pulse=NUM_PULSE,
        ramp_rate=RAMP_RATE,
        is_dryrun=DRYRUN,
        input_provider=auto_input,
        value_provider=auto_value,
    )

    if terminated:
        print("  no positions left on this substrate")
        break
    if not ok:
        print("  growth failed; stopping")
        break

    # --- 3. score it and record the result on the sample
    samples = await exp.driver.list_samples(ListSamples(substrate_id=SUBSTRATE_ID))
    grown = [s for s in samples.samples if s.kind == "position" and s.state == "grown"]
    sample = sorted(grown, key=lambda s: s.sample_id)[-1]

    metrics = await score_growth(storage_name, conditions)
    await exp.driver.add_measurement(AddMeasurement(
        sample_id=sample.sample_id,
        kind="rheed_metric",
        value=float(metrics["metric"]),
        detail=metrics,
        source="analyze_rheed_video v2" if HAVE_ANALYSIS else "synthetic landscape",
    ))

    print(f"  sample {sample.sample_id} ({sample.sample_name}) "
          f"scored {metrics['metric']:.3f}")
    history.append({"iteration": iteration, "sample_id": sample.sample_id,
                    "metric": metrics["metric"], **{c: conditions[c] for c in campaign.x_columns}})

    campaign.save_snapshot(iteration)

print(f"\\nfinished after {len(history)} growths")
"""),
        (MD, """
## What the campaign learned

The training set is read back from the growth database, not from anything this
notebook kept in memory -- re-running this cell after restarting the kernel gives the
same answer.
"""),
        (CODE, """
await campaign.refresh(exp.driver, substrate_id=SUBSTRATE_ID)
display(campaign.db)

best = campaign.best()
if best is not None:
    print("\\nbest growth so far:")
    for c in campaign.x_columns:
        print(f"  {c:<24} {best[c]:.4g}")
    print(f"  {'metric':<24} {best[campaign.y_column]:.3f}  (sample {int(best['sample_id'])})")
"""),
        (CODE, """
if history:
    frame = pd.DataFrame(history)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))

    axes[0].plot(frame["iteration"], frame["metric"], "o-", color="#1a6b66")
    axes[0].axvline(N_RANDOM - 0.5, ls="--", c="grey", lw=1)
    axes[0].annotate("GP takes over", (N_RANDOM - 0.4, frame["metric"].min()),
                     fontsize=8, color="grey")
    axes[0].set_xlabel("iteration")
    axes[0].set_ylabel("metric")
    axes[0].set_title("score per growth")

    sc = axes[1].scatter(frame["temperature"], frame["pressure"],
                         c=frame["metric"], cmap="viridis", s=70)
    axes[1].set_yscale("log")
    axes[1].set_xlabel("temperature / degC")
    axes[1].set_ylabel("pressure / Torr")
    axes[1].set_title("where it looked")
    fig.colorbar(sc, ax=axes[1], label="metric")
    plt.tight_layout()
    plt.show()
"""),
        (MD, """
## The provenance of any one point

Every point in the GP above is a real sample with a recorded history -- the conditions
it was grown at, the steps that grew it, who ran them and how long each took.
"""),
        (CODE, """
from lumi.contracts.payloads.experiment import ListSteps

if history:
    sample_id = int(history[-1]["sample_id"])
    detail = await exp.driver.get_sample(SampleId(sample_id=sample_id))
    print(f"sample {sample_id}: {detail.sample.sample_name} [{detail.sample.state}]")
    print("layers:", [(l.seq, l.material, l.num_pulse) for l in detail.layers] or "(dryrun: none)")

    steps = await exp.driver.sample_history(ListSteps(sample_id=sample_id))
    print(f"\\n{'step':<28} {'ok':>5} {'dur':>8}  actor")
    for s in steps.steps:
        dur = f"{s.ended_at - s.started_at:.2f}s" if s.ended_at else "-"
        print(f"{s.kind:<28} {str(s.ok):>5} {dur:>8}  {s.actor}")
"""),
        (CODE, """
await exp.close()
print("session closed")
"""),
    ]
