"""Source for SingleDeposition.ipynb. See build_notebooks.py."""

from __future__ import annotations

MD = "markdown"
CODE = "code"


def cells() -> list[tuple[str, str]]:
    return [
        (MD, """
# Single Deposition

A layered growth on one substrate position: **bottom electrode &rarr; interface &rarr;
functional layer**. Ported from `UMDSingleDepoExperiment.ipynb`, which drove the real
HZO stack (SRO / LSMO / HZO) against the v1.0 API.

What changed in the port:

| v1.0 | now |
| --- | --- |
| `MQCommunication()` + eleven clients | `ExperimentSession` |
| `SingleDepoExperimentManager.perform_experiment` | `recipes.perform_single_deposition` |
| `ainput("...")` blocking the kernel | `input_provider` / `value_provider` hooks |
| `src.db.GrowthDB` opened locally | the experiment node owns it; reached over the contract |
| `collector` pickle + `local_experiment_counter` | `list_samples` / `sample_history` |
| conditions typed into a dict | recorded automatically as `step` rows |

**This notebook runs against the simulator as written.** Bring the stack up first:

```bash
docker run -d --name lumi-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management
scripts/start_simulation.sh --chamber-speed 30 --with-experiment
```

Then run every cell. The real-hardware numbers are in the **Growth parameters** cell,
marked, so switching to the lab is editing one cell and `HOST`.
"""),
        (CODE, """
import asyncio
import uuid
from pathlib import Path

import matplotlib.pyplot as plt

from lumi.contracts.payloads.experiment import (
    ListSamples,
    ListSteps,
    RegisterProject,
    RegisterSubstrate,
    SampleId,
    TargetId,
    ToTemperature,
)
from lumi.experiment import recipes
from lumi.experiment.client import ExperimentSession
"""),
        (MD, """
## Connect

`ExperimentSession` spans the three exchanges a growth needs (EXPERIMENT + RHEED +
CHAMBER) and owns the connection. It replaces the eleven separate clients the v1.0
notebook opened by hand.

`actor` is who to credit in the step journal. Every op you run below is recorded
against this name, so the history says *who* ramped the chamber rather than just that
a notebook did.
"""),
        (CODE, """
HOST = "localhost"     # the lab broker's address on the instrument machine
ACTOR = "hliang16"     # your name, for the step journal
DRYRUN = True          # True: bookkeeping and gates, no laser and no ramp

exp = await ExperimentSession.open(host=HOST, actor=ACTOR)

state = await exp.driver.get_state()
print("deps:", state.deps_available)
print("mode:", state.mode, "| pending:", state.pending_confirmation)
"""),
        (MD, """
A non-dryrun run additionally needs the **detection** node: `start_storage` always
requests `save_ai`, and the storage node refuses to record when a requested source is
off the bus. Start the stack with `--with-detection`, or keep `DRYRUN = True`.
"""),
        (CODE, """
print("chamber:", (await exp.driver.get_current_temperature()).temperature, "degC")
print("pressure:", (await exp.driver.get_current_pressure()).pressure, "Torr")
print("targets:", (await exp.driver.show_available_targets()).targets)
"""),
        (MD, """
## Look at the RHEED pattern

The v1.0 notebook reached for `comm.camera_client.get_image()`. The session holds the
RHEED camera directly, for exactly this.
"""),
        (CODE, """
# NB: (meta, frame), not (frame, meta) -- the array never passes through JSON,
# so the model describes the headers and the payload follows it.
meta, frame = await exp.rheed.image()
plt.figure(figsize=(8, 5))
plt.imshow(frame, cmap="gray")
plt.title(f"RHEED frame {frame.shape}")
plt.axis("off")
plt.show()
"""),
        (MD, """
## Growth parameters

**&larr; This is the cell to edit for a real run.** The values below are the
simulator's; the real HZO stack's numbers from the production notebook are in the
comment on each line.
"""),
        (CODE, """
PROJECT = "UMD_AI_HZO_SingleDepo"

# Substrate
SUBSTRATE = dict(
    materials="SrTiO3",
    orientation="001",
    width=5.0, height=5.0, thickness=0.5,   # 10 x 10 for the real HZO runs
)

LASER_POWER = 1.2        # real: 88 mJ, measured at the exit view port
RAMP_RATE = 20.0         # real: 10 for >= 75 mm^2 substrates, 20 otherwise

# The three layers, bottom-up. `target` is the carousel slot; the material name is
# resolved from chamber config and recorded in the journal at deposition time.
LAYERS = [
    dict(name="bottom electrode", target="C", temperature=300.0, pressure=100e-3,
         rate=1.0,  pulses=60,  preablation=0),      # real: SRO,  600 degC, 300 pulses
    dict(name="interface",        target="C", temperature=300.0, pressure=100e-3,
         rate=1.0,  pulses=40,  preablation=0),      # real: LSMO, 600 degC, 114 pulses
    dict(name="functional",       target="D", temperature=320.0, pressure=100e-3,
         rate=2.78, pulses=120, preablation=60),     # real: HZO,  700 degC, 500 pulses
]
"""),
        (MD, """
## Answering the recipe's prompts

`perform_single_deposition` pauses at every point a person has to do or read something
physical. The v1.0 notebook used `ainput`, which blocks the kernel on stdin -- awkward
in Jupyter and impossible to leave unattended.

The recipe takes two hooks instead. Below they answer themselves so the notebook runs
end to end against the simulator; swap in `recipes.default_input_provider` and
`recipes.default_value_provider` to be prompted for real.
"""),
        (CODE, """
async def auto_input(message: str) -> None:
    print(f"  [auto] {message}")


async def auto_value(prompt: str) -> str:
    # The recipe asks two kinds of question: a measured laser power, and yes/no
    # alignment checks.
    answer = str(LASER_POWER) if "measured" in prompt.lower() else "y"
    print(f"  [auto] {prompt} -> {answer}")
    return answer


# For a real run, hand the prompts back to yourself:
# auto_input, auto_value = recipes.default_input_provider, recipes.default_value_provider
"""),
        (MD, """
## Register the project and substrate

`register_substrate` now also materialises one **sample** row per growable position,
so "which positions are spent" is a query rather than something the notebook has to
remember. The v1.0 notebook kept that in a pickle.
"""),
        (CODE, """
project = await exp.driver.register_project(RegisterProject(
    project_name=PROJECT,
    description="HZO single deposition on LSMO / SRO / STO (001).",
))

substrate = await exp.driver.register_substrate(RegisterSubstrate(
    **SUBSTRATE, substrate_name=f"STO-{uuid.uuid4().hex[:6]}",
))

print(f"project {project.project_name} (id {project.project_id})")
print(f"substrate id {substrate.substrate_id}, {len(substrate.positions)} position(s)")

samples = await exp.driver.list_samples(ListSamples(substrate_id=substrate.substrate_id))
for s in samples.samples:
    print(f"  sample {s.sample_id}: {s.kind:<9} {s.sample_name}  [{s.state}]")
"""),
        (MD, """
### Resuming instead

If the node restarted mid-campaign, resume rather than register -- `resume_substrate`
now restores which positions are already grown on, so it will not hand you a spent
one. (It used to rewind to position 0 and deposit on top of an existing film.)

```python
substrate = await exp.driver.resume_substrate(ResumeSubstrate(substrate_id=<id>))
print("next free position index:", substrate.current_pixel_index)
```
"""),
        (MD, """
## Warm up

Neither `to_temperature` nor the recipe turns the heating diode on -- the recipe's
"excimer laser ON" is the *ablation* laser, a different device. With the diode off a
ramp sets a setpoint no current can reach, so the node refuses; that refusal is itself
journaled.
"""),
        (CODE, """
if not DRYRUN:
    await exp.driver.initiate_heating_laser()
    ack = await exp.driver.to_temperature(ToTemperature(temperature=350.0, ramp_rate=RAMP_RATE))
    await recipes._wait_for_task(exp, ack.task_id)
    print("warm-up result:", exp.last_task_result)
else:
    print("dryrun: skipping the warm-up")
"""),
        (MD, """
## Grow the stack

One `perform_single_deposition` per layer. The v1.0 notebook had this copy-pasted
three times with a `collector` dict assembled by hand after each; the loop below is the
same sequence, and the bookkeeping it was doing manually is now the journal's job.

`finish_substrate=False` on every layer but the last: all three go on the *same*
position, and retiring it after the first would leave nothing to grow on.
"""),
        (CODE, """
results = []

for i, layer in enumerate(LAYERS):
    # The slot -> material map is chamber config and changes when targets are
    # swapped, so resolve it now; the journal records the name alongside the slot.
    material = (await exp.driver.get_target_name_by_id(
        TargetId(target_id=layer["target"]))).target_name

    print(f"\\n=== layer {i}: {layer['name']} = {material} "
          f"({layer['pulses']} pulses, {layer['temperature']:.0f} degC) ===")

    ok, (storage_name, conditions) = await recipes.perform_single_deposition(
        exp,
        project_name=PROJECT,
        pressure=layer["pressure"],
        temperature=layer["temperature"],
        laser_power=LASER_POWER,
        laser_repetition_rate=layer["rate"],
        target_id=layer["target"],
        target_material=material,
        num_pulse=layer["pulses"],
        do_preablation=layer["preablation"] > 0,
        preablation_pulse=layer["preablation"],
        preablation_frequency=layer["rate"],
        ramp_rate=RAMP_RATE,
        is_dryrun=DRYRUN,
        # Only the last layer retires the position -- all three share it.
        finish_substrate=(i == len(LAYERS) - 1),
        input_provider=auto_input,
        value_provider=auto_value,
    )

    results.append({"layer": layer["name"], "ok": ok, "storage": storage_name,
                    "conditions": conditions})
    print(f"  -> ok={ok} record={storage_name}")

    if not ok:
        print("  stopping: the layer did not complete")
        break
"""),
        (MD, """
## What the system recorded

Nothing below was typed by hand. `sample_history` is the step journal; the layer stack
is derived from the deposition steps that actually succeeded, so a failed or dryrun
layer is not on the sample.
"""),
        (CODE, """
samples = await exp.driver.list_samples(ListSamples(substrate_id=substrate.substrate_id))
grown = [s for s in samples.samples if s.kind == "position"][0]

detail = await exp.driver.get_sample(SampleId(sample_id=grown.sample_id))
print(f"sample {grown.sample_id} ({grown.sample_name}) -- state {detail.sample.state}")
print("\\nlayer stack, bottom-up:")
if detail.layers:
    for layer in detail.layers:
        print(f"  {layer.seq}: {layer.material} x{layer.num_pulse} pulses (step {layer.step_id})")
else:
    print("  (empty -- a dryrun deposits nothing, so it is not a layer)")
"""),
        (CODE, """
history = await exp.driver.sample_history(ListSteps(sample_id=grown.sample_id))
print(f"{'step':<28} {'ok':>5} {'dur':>8}  actor")
for s in history.steps:
    dur = f"{s.ended_at - s.started_at:.2f}s" if s.ended_at else "-"
    print(f"{s.kind:<28} {str(s.ok):>5} {dur:>8}  {s.actor}")
"""),
        (MD, """
### Attaching a measurement

Once a growth has been analysed -- a RHEED metric, or an ex-situ XRD/AFM/transport
result -- it belongs on the sample rather than in a CSV beside the notebook. This is
the same call `BODeposition.ipynb` uses to feed its GP.

```python
from lumi.contracts.payloads.experiment import AddMeasurement

await exp.driver.add_measurement(AddMeasurement(
    sample_id=grown.sample_id,
    kind="rheed_metric",
    value=metrics["metric"],
    detail=metrics,
    source="analyze_rheed_video v2",
))
```
"""),
        (CODE, """
await exp.close()
print("session closed")
"""),
    ]
