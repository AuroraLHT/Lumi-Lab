# Lumi-Lab

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

Settings are loaded by `dynaconf` from `cfg/settings.toml`, which is **not** tracked.
Copy the template on first checkout and edit your copy:

```bash
cp cfg/settings.example.toml cfg/settings.toml
```

`cfg/settings.example.toml` is the tracked template — carrying the safe defaults, and
where a genuinely shared config change belongs. Machine-local overrides (and anything
secret) go in `cfg/.secrets.toml`, which is also git-ignored. Environment overrides use
the `DYNACONF_` prefix, e.g. `DYNACONF_API__WORKERS=1`.

Two settings worth knowing about before you start anything:

- `rabbitmq.host` — `localhost` in the template, so nothing reaches real equipment by
  default. The lab broker's address is machine-local: put it in `cfg/settings.toml` or
  `cfg/.secrets.toml`, or pass `--host` (every node and the MCP server take one).
- `auth.enabled` — keep it `true` in the template. Turn it off for local work in your own
  `cfg/settings.toml` or `cfg/.secrets.toml`.

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

Nodes run on different machines, so there are two launchers — one per host. Both run a
preflight first (`--check` runs only the preflight) and refuse to start anything if it
fails, rather than leaving a half-dead stack behind:

```bash
# On the server machine -- monitor, storage, detection, api. The broker lives here.
uv sync --extra api --extra storage --extra detection
scripts/install_detection_deps.sh          # detection host only
scripts/start_server_host.sh

# On the instrument machine -- pascal and rheed. --host is required: the broker is
# on the other machine, and RabbitMQ refuses `guest` off loopback.
uv sync --extra pascal --extra camera
scripts/start_instrument_host.sh --host <server ip> --user <node user> --password <pw> \
    --log "C:/.../chamber_log" --mi "C:/.../mi_mode"
```

Start the **server host first**: its storage node declares the exchanges the instrument
host's producers publish into. Give the instrument host a real broker account:

```bash
uv run python scripts/apply_broker_permissions.py --host <broker> \
    --user lumi-node --role node --password <pw>
```

`start_server_host.sh` writes to the real HDF5 root and the real user database, and
refuses to start with `auth.enabled = false` or the placeholder signing key
(`--allow-insecure-auth` overrides, for an isolated bench network).

To run a node by hand instead, in the order consumers-before-producers:

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

## The MCP server — driving a growth from an LLM agent

`src/lumi/mcp/` exposes the experiment node's ops as MCP tools, so an agent (Claude Code,
Claude Desktop, anything speaking MCP) can run a deposition. There is no console script;
it's a module:

```bash
uv run python -m lumi.mcp                              # stdio, for a local MCP host
uv run python -m lumi.mcp --transport http --port 8100 # streamable HTTP, for a remote agent
```

It needs the `mcp` extra (`uv sync --extra mcp`) and a reachable broker. The experiment
node does **not** have to be up to start it — tools are built from the contract, not from
who is on the bus — but every call will time out until it is.

### Which broker to point it at

`--host` defaults to `settings.rabbitmq.host`, the same as every node — `localhost` in
the template config, so nothing reaches real equipment unless you ask it to:

```bash
uv run python -m lumi.mcp                         # the simulator on this machine
uv run python -m lumi.mcp --host some-other-box   # a simulator, or the lab broker, elsewhere
```

`--user` / `--password` go with it if that broker isn't using `guest`/`guest`. On the
host that really does talk to the chamber, set `rabbitmq.host` in your own
`cfg/settings.toml` or `cfg/.secrets.toml` — never in the tracked
`cfg/settings.example.toml`. The broker address is a machine-local fact, and having it in
the shared template is what made `python -m lumi.mcp` reach for the lab by default.

> **The lab broker is not ready for this yet.** It still holds the pre-refactor
> messaging layer's exchanges — `CHAMBER`, `RHEED` and `STORAGE` exist there as
> non-durable **`direct`** exchanges, with live bindings from the old-style nodes. Any
> contract-era client that declares them as `topic` is refused at startup:
>
> ```
> PRECONDITION_FAILED - inequivalent arg 'type' for exchange 'RHEED' in vhost '/':
> received 'topic' but current is 'direct'
> ```
>
> This is not specific to the MCP server — it will happen to any refactored node pointed
> at that broker. Clearing it means deleting those three exchanges (they are non-durable,
> so a broker restart drops them anyway) once the old nodes are no longer using them.
> Until then, use a local broker.

The tools are generated, one per `(contract, capability, op)`, named
`experiment.driver.to_temperature` and so on — 49 of them today. Adding an op to
`contracts/experiment.py` makes it a tool with no change here. The surface is
deliberately narrower than the browser bridge's: all of `experiment`, plus read-only
`rheed.camera` and `chamber.log` for situational awareness, and nothing else — an agent
has no business reaching `system.supervisor.spawn` or raw MI script execution.

### Adding it to Claude Code

From the project you want to drive it from:

```bash
claude mcp add lumi-experiment -- \
  uv run --project /path/to/Lumi-Lab python -m lumi.mcp
```

`--project` matters: without it `uv run` resolves against whatever directory the MCP host
launched from, which is usually not this repo. Then `claude mcp list` should show
`✔ Connected`. Use `--scope project` instead of the default if you want the registration
written to a `.mcp.json` that travels with the repo; `claude mcp remove lumi-experiment`
undoes it. For Claude Desktop the same command line goes in `claude_desktop_config.json`
under `mcpServers`.

stdio needs no auth — it's a subprocess only you can spawn, the same trust level as any
other local tool.

### The HTTP transport

For a remote agent. It always requires an **operator or admin** bearer token — the same
JWT `POST /auth/login` issues for the browser — and unlike the browser side this is *not*
gated by `auth.enabled`, so a dev-mode bypass never opens real equipment control to the
network. Viewer tokens are refused at the door.

```bash
uv run python -m lumi.api.manage create-user agent --role operator
```

It serves plain HTTP; put nginx/Caddy in front for TLS (`docs/TODO.md`). `--bind-host`
defaults to `127.0.0.1` — anything else means your firewall is the only thing between the
internet and the chamber.

### Before you let an agent run a growth

`to_temperature`, `cool_down`, `perform_preablation`, `perform_deposition` and `anneal`
return a `task_id` immediately. The server's instructions tell the agent to watch
`current_task` clear before continuing, but nothing enforces it, and `current_task`
clearing does not mean the step *succeeded* — so an agent that skips the check will
happily deposit at whatever temperature the substrate actually reached. Same failure mode
as the recipe issue in `docs/TODO.md`, with an LLM driving. Ops needing a human (laser
power, mask alignment, RHEED gain) come back with `pending_confirmation` and block until
the matching `confirm_*` tool resolves them.

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
