# Autonomous-Servers

`lumi` — the contract-driven control stack for the PLD lab. Each piece of equipment runs
as a **node** (a process in `nodes/`) that talks to the others over RabbitMQ. What a node
can do is declared once in `src/lumi/contracts/`, and both the Python clients and the
frontend's TypeScript client are generated from that declaration.

Nodes:

| node | what it is |
| --- | --- |
| `monitor` | presence registry — who is alive on the bus |
| `storage` | HDF5 recorder for RHEED frames and chamber logs |
| `pascal` | the PLD chamber: growth log, `PLDconfig.ini`, MI mode, chamber camera |
| `rheed` | RHEED camera acquisition (Basler/Pylon, webcam, or a canned video) |
| `detection` | RHEED spot detection (needs the OpenMMLab stack — see below) |
| `experiment` | the growth driver: plans and runs a deposition end to end |
| `agent` | per-host supervisor that can spawn/kill the other nodes |
| `api` | FastAPI: auth plus one `/ws` bridge from the browser to the bus |

## Requirements

- **Python 3.11** (`>=3.11,<3.12` — capped because OpenMMLab ships no `mmcv<2.2` wheel
  for 3.12; see the `detection` extra's comment in `pyproject.toml`)
- **RabbitMQ** reachable from every host running a node
- [`uv`](https://docs.astral.sh/uv/) for dependency management

### Install

```bash
uv sync --all-extras          # everything; or pick per-host extras, e.g. --extra pascal
```

The extras are deliberately split by role — `camera`, `pascal`, `storage`, `api`,
`detection`, `experiment`, `mcp` — so a detection box doesn't have to install `pypylon`
and the camera host doesn't have to install CUDA.

On the **detection** host only, after `uv sync`:

```bash
scripts/install_detection_deps.sh
```

That installs torch/mmcv/mmdet/rhana, which can't be pinned portably in
`pyproject.toml`. **Any later `uv sync` re-resolves numpy and breaks torch again — re-run
this script after every sync on that host.**

### Configuration

All settings live in `cfg/settings.toml`, loaded by `dynaconf`. Machine-local overrides
(and anything secret) go in `cfg/.secrets.toml`, which is git-ignored. Environment
overrides use the `DYNACONF_` prefix, e.g. `DYNACONF_API__WORKERS=1`.

Two settings worth knowing about before you start anything:

- `rabbitmq.host` — the lab broker. Override with `--host` on any node.
- `auth.enabled` — keep it `true` in the tracked file. Turn it off for local work in
  `cfg/.secrets.toml`, not here.

## Running the stack

### With the simulator (no hardware needed)

`scripts/start_simulation.sh` brings up the whole stack against simulated sources: a
state-model chamber, a canned RHEED video, a scratch HDF5 directory and a scratch user
database. This is the way to exercise the system end to end before touching production.

```bash
docker run -d --name lumi-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management
scripts/start_simulation.sh
```

The API comes up on <http://localhost:8000> — `GET /health` for liveness and the
capability list, `WS /ws` for everything else. It serves no HTML: the browser UI lives in
the sister frontend repo and is pointed at this host. Ctrl-C shuts every node down; logs
land in `run/simulation/logs/`.

Options:

```
--host HOST         broker host (default localhost)
--chamber-speed N   simulated seconds per wall second in the chamber (default 1)
--with-experiment   also start the experiment node
--with-detection    also start the detection node (needs the deps above)
--with-agent        also start the supervisor
--with-auth         require login (bootstraps admin / simulation)
--keep-database     use the real HDF5 root and user database instead of scratch ones
--no-reset-broker   skip the stale-exchange cleanup
```

Run it **directly**, never `source`d — sourcing breaks the Ctrl-C cleanup and orphans
every node (the script explains why and refuses). If you do end up with orphans:

```bash
scripts/stop_nodes.sh --dry-run     # see what's still running
scripts/stop_nodes.sh
```

`--chamber-speed` is worth turning up. At the default `1`, a 700 °C ramp at 20 °C/min
takes the full ~27 minutes of wall clock; at `30` a whole growth is about a minute:

```bash
scripts/start_simulation.sh --chamber-speed 30
```

### Against real hardware

There is no single start script for production — nodes run on different machines. Start
them in this order, so consumers exist before the producers they feed:

```bash
python nodes/monitor.py  --host <broker>
python nodes/storage.py  --host <broker>                      # declares the exchanges

python nodes/pascal.py   --host <broker> --src path \
    --log "C:/.../chamber_log" --mi "C:/.../mi_mode"          # on the PASCAL PC
python nodes/rheed.py    --host <broker> --src pylon          # on the camera host
python nodes/detection.py --host <broker>                     # on the GPU box
python nodes/experiment.py --host <broker>

python nodes/api.py                                           # on the web host
```

Every node takes `--host/--user/--password`, `--instance` (override the instance id on
the bus) and `-v`. The chamber's `--src` selects where the data comes from:

| `--src` | chamber log | MI mode |
| --- | --- | --- |
| `path` | the folder PASCAL writes into (`--log`) | the real MI folder (`--mi`) |
| `sim` | rendered from the chamber state model | executed by the state model |
| `test` | replay of a recorded log (idle chamber) | a stub |

`--src test` is a **recording of an idle chamber**: `Motor Stat` is `0x0000` for every
row, so `is_motor_free()` never returns true and no growth can complete against it. Use
`--src sim` for anything behavioural.

The API server reads its broker URL from `LUMI_AMQP_URL` first, falling back to
`rabbitmq.host`:

```bash
LUMI_AMQP_URL="amqp://guest:guest@<broker>:5672/" python nodes/api.py
```

## Driving the simulated chamber

Two demo scripts, at two different layers. Both need the stack already running, and both
go in a second terminal alongside it.

| script | talks to | use it to |
| --- | --- | --- |
| `scripts/demo_growth.py` | the **chamber node**, raw MI commands | watch the simulated chamber move and check UI panels against known numbers |
| `scripts/demo_experiment.py` | the **experiment node**, via `ExperimentSession` | exercise the production client path: bookkeeping, tasks, gates, storage, provenance |

### `demo_growth.py` — the chamber, directly

`scripts/demo_growth.py` runs a complete growth against the simulator — heat-up, gas,
target select, preablation, deposition, cool-down — and prints the chamber log as it
goes. Every number it prints is a number the frontend is being pushed at the same
moment, so the console is the ground truth to check the UI panels against. It bypasses
the experiment node entirely, which is what makes it useful for isolating the chamber.

Start the stack first, then run this **alongside** it in a second terminal:

```bash
scripts/start_simulation.sh --chamber-speed 30      # terminal 1
uv run scripts/demo_growth.py --speed 30            # terminal 2
```

`--speed` must match the `--chamber-speed` the node was started with. It doesn't change
the node's behaviour — it's how the script sizes its timeouts and predicts the runtime,
and it measures the real speed-up during deposition and warns if the two disagree.

For a quick look (300 °C, fast ramp, few pulses — about a minute at `--speed 30`):

```bash
uv run scripts/demo_growth.py --speed 30 --quick
```

(`uv run` because the script's shebang is a bare `python`; drop it if you've activated
`.venv` yourself.)

Growth parameters: `--temperature` (°C), `--ramp-rate` (°C/min), `--pressure` (mTorr),
`--target` (carousel slot `A`–`F`, `Clear`, `Monitor`), `--pulses`, `--rate` (laser Hz),
`--preablation-pulses` (`0` skips), `--mask-block` (mm), `--skip-cooldown`, and
`--interval` for how often the log table is printed.

One thing to watch out for: don't leave a second `nodes/pascal.py` running against the
same broker. Two chamber nodes are competing consumers on the same queue and will
round-robin the RPCs between them, which looks like random lag and interleaved readings.

### `demo_experiment.py` — through the experiment node

`scripts/demo_experiment.py` drives the same growth one layer up, the way a notebook
would: `lumi.experiment.client.ExperimentSession` plus
`lumi.experiment.recipes.perform_single_deposition`. That covers everything
`demo_growth.py` skips — project and substrate registration, the long-running-task state
machine (`current_task` → `task_result` on the driver's update channel), the human-gated
steps, the storage record, and the provenance row written at the end. It prints the
node's task transitions next to the chamber log, so you can see both halves at once.

It needs the experiment node, which `start_simulation.sh` does **not** start by default:

```bash
scripts/start_simulation.sh --chamber-speed 30 --with-experiment   # terminal 1
uv run scripts/demo_experiment.py --speed 30                       # terminal 2
```

**Two clocks, not one.** `--chamber-speed` compresses the chamber's physics, but the
experiment node paces part of a growth in wall time of its own — the warm-up ramp sleeps
`warm_up_step / warm_up_current_ramp_rate` per current step and then waits up to
`warm_up_max_waittime`, about 280 s at the shipped values, no matter how fast the chamber
is running. `start_simulation.sh` scales those bounds by the same factor
(`--experiment-speed N` to set it independently), so at `--chamber-speed 30` the warm-up
is ~9 s instead of ~280 s.

`--speed` on `demo_experiment.py` does **not** speed anything up — it only sizes this
script's RPC timeouts. The two knobs that change run time are `nodes/pascal.py --speed`
for the chamber and `--experiment-speed` for the node's own pacing.

The recipe's `[Manual]` steps (set the pressure, read the laser power meter) are answered
automatically so the run is unattended; `--interactive` hands them back to you. Other
flags: `--temperature`, `--ramp-rate`, `--pressure` (Torr here, not mTorr), `--target`,
`--target-material`, `--pulses`, `--rate`, `--preablation-pulses`, `--laser-power`,
`--project`, `--substrate`, `--quick`, and `--dryrun` to run all the bookkeeping and gates
while skipping the ramp and the laser.

`--dryrun` is the fast smoke test — a few seconds, and it still writes a real storage
record and provenance row.

A **non-dryrun** run additionally needs the detection node: `manager.start_storage` always
requests `save_ai`, and the storage node refuses to record when any requested source is
off the bus. Start the stack with `--with-detection` (its deps come from
`scripts/install_detection_deps.sh`) or stick to `--dryrun`. Preflight checks this and
says so before the growth starts rather than failing at `start_storage` minutes in.

Two behaviours worth knowing before you Ctrl-C it: killing the client does not stop the
node — a `to_temperature` you abandon keeps ramping, and its stale `current_task` will
still be there on your next run. And nothing stops the chamber: a script already handed to
the controller runs to the last pulse (`docs/TODO.md`).

## Tests

```bash
uv run pytest -q -m "not broker"     # fast: no broker, no hardware
uv run pytest -q                     # also the broker tests (testcontainers spins up RabbitMQ)
```

Three tiers, by marker:

- **unmarked** — pure logic, no broker, no hardware. Always runnable.
- **`broker`** — needs a live RabbitMQ; `testcontainers` starts one. Slow.
- **`hardware`** — needs the real camera/chamber. Never runs in CI.

The chamber simulator has its own suite in `tests/pascal/test_chamber_sim.py`: the log
column schema against a recorded asset, the MI script decoder, and the physics
(temperature ramps, pressure control, carousel rotation, laser pulse counting, mask
travel limits). It runs entirely in-process with simulated time, so it's fast.

## Contracts and generated code

`src/lumi/contracts/` is the single source of truth. After changing it, regenerate:

```bash
uv run lumi-codegen              # writes the Python clients, web/src/generated/lumi.ts, schemas/contract.json
uv run lumi-codegen --check      # CI gate: fails if anything on disk has drifted
```

The contract hash printed by those commands gates backend/frontend compatibility — if it
changes, the frontend needs the regenerated `lumi.ts`. See `docs/FRONTEND_MIGRATION.md`
for how the browser-side API maps onto the bridge.
