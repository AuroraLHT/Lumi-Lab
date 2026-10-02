# Lumi-Lab

`lumi` — the contract-driven control stack for a pulsed-laser-deposition (PLD) lab. Each
piece of equipment runs as a **node** (a process in `nodes/`) that talks to the others
over RabbitMQ. What a node can do is declared once in `src/lumi/contracts/`, and both the
Python clients and the TypeScript client for the web console
([Lumi-Deck](https://github.com/AuroraLHT/Lumi-Deck)) are generated from that
declaration.

This is the platform behind
[*Autonomous epitaxial atomic-layer synthesis via real-time computer vision of electron
diffraction*](https://arxiv.org/abs/2602.20432): the growth driver, chamber and RHEED
nodes, and the closed-loop Bayesian-optimisation notebook here are the ones that ran the
campaigns in that paper. See [Citation](#citation) below.

Nodes:

| node | what it is |
| --- | --- |
| `monitor` | presence registry — who is alive on the bus |
| `storage` | HDF5 recorder for RHEED frames and chamber logs |
| `pascal` | the PLD chamber: growth log, `PLDconfig.ini`, MI mode, chamber camera, fiducial markers |
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
`detection`, `experiment`, `rheedsim`, `mcp` — so a detection box doesn't have to install `pypylon`
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
where a genuinely shared config change belongs.

Machine-local overrides (and anything secret — the JWT signing key, a local `auth`
toggle, the lab broker address) go in `cfg/.secrets.toml`, which is git-ignored.
`dynaconf` loads it after `settings.toml`, so every key there wins. Start from its
template too:

```bash
cp cfg/.secrets.example.toml cfg/.secrets.toml
```

Environment overrides use the `DYNACONF_` prefix, e.g. `DYNACONF_API__WORKERS=1`.

Two settings worth knowing about before you start anything:

- `rabbitmq.host` — `localhost` in the template, so nothing reaches real equipment by
  default. The lab broker's address is machine-local: put it in `cfg/settings.toml` or
  `cfg/.secrets.toml`, or pass `--host` (every node and the MCP server take one).
- `auth.enabled` — keep it `true` in the template. Turn it off for local work in your own
  `cfg/settings.toml` or `cfg/.secrets.toml`.

## Running the stack

### With the simulator (no hardware needed)

`scripts/start_simulation.sh` brings up the whole stack against simulated sources: a
state-model chamber, a RHEED camera showing a real lab frame, a scratch HDF5 directory
and a scratch user database. This is the way to exercise the system end to end before
touching production.

```bash
docker run -d --name lumi-rabbit -p 5672:5672 -p 15672:15672 rabbitmq:3-management
scripts/start_simulation.sh
```

The API comes up on <http://localhost:8000> — `GET /health` for liveness and the
capability list, `WS /ws` for everything else. It serves no HTML: the browser UI lives in
[Lumi-Deck](https://github.com/AuroraLHT/Lumi-Deck), the operator console, and is pointed
at this host. Ctrl-C shuts every node down; logs land in `run/simulation/logs/`.

Options:

```
--host HOST         broker host (default localhost)
--chamber-speed N   simulated seconds per wall second in the chamber (default 1)
--substrate S       what the RHEED camera shows: sto = SrTiO3(001) along [100] (default),
                    ysz = YSZ(111) along [1-10]. Real frames from the lab camera; the
                    simulation node's screen is set to match, so overlays line up
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
# On the server machine -- monitor, storage, detection, experiment, simulation, api.
# The broker lives here.
uv sync --extra api --extra storage --extra detection --extra rheedsim
scripts/install_detection_deps.sh          # detection host only
scripts/start_server_host.sh               # --no-experiment / --no-rheed-sim to skip those

# On the instrument machine -- pascal and rheed. --host is required: the broker is
# on the other machine, and RabbitMQ refuses `guest` off loopback.
uv sync --extra pascal --extra camera
scripts/start_instrument_host.sh --host <server ip> --user <node user> --password <pw> \
    --log "C:/.../chamber_log" --mi "C:/.../mi_mode"
```

Start the **server host first**: its storage node declares the exchanges the instrument
host's producers publish into. Give the instrument host a real broker account (see
[Accounts](#accounts) below).

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
python nodes/simulation.py --host <broker>                   # RHEED simulation, any host

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

### Accounts

There are **two separate credential systems**, and one does not imply the other:

| | who authenticates | where it lives | created with |
| --- | --- | --- | --- |
| **API account** | the browser console, and anything hitting `POST /auth/login` (which mints the JWT the `/ws` bridge and the MCP HTTP transport check) | SQLite at `auth.database_path` (default `cfg/users.db`) | `python -m lumi.api.manage` (or `scripts/create_api_user.py`) |
| **Broker account** | every node, and a notebook that calls `ExperimentSession.open()` (it connects straight to RabbitMQ, no API in the path) | RabbitMQ's own user list | `scripts/apply_broker_permissions.py` |

Both take a role of `viewer` / `operator` / `admin` (the broker script also has `node`,
for equipment processes), and the same `lumi.contracts.policy` predicate gates the broker
and the `/ws` bridge, so a role means the same thing on either side. But the accounts are
independent: an `hliang16` in `cfg/users.db` is not an `hliang16` on the broker, and the
passwords need not match.

**API account** — for logging into the web console, or for the notebook when it goes
through the API rather than the bus. `python -m lumi.api.manage` is the account CLI, and
covers the whole lifecycle:

```bash
python -m lumi.api.manage create-user hliang16 --role admin   # prompts for a password
python -m lumi.api.manage list-users
python -m lumi.api.manage set-password <username>
python -m lumi.api.manage gen-secret                          # a fresh JWT signing key
```

`scripts/create_api_user.py` is a thin wrapper over the same `create-user`, kept next to
`apply_broker_permissions.py` so the two account types are found together. It adds one
thing the module CLI lacks — `--database <path>`, to target a users.db other than the
configured one (the simulation stack's is under `run/simulation/`):

```bash
uv run python scripts/create_api_user.py agent --role operator --password <pw>
uv run python scripts/create_api_user.py alice --admin --database run/simulation/users.db
```

**The simulator has its own accounts.** `scripts/start_simulation.sh` points the stack
(API, and an MCP server started with `scripts/start_mcp_demo.sh`) at
`run/simulation/users.db`, not `cfg/users.db`, so an account made with the plain command
above does not exist there, and its login is refused. The file survives restarts. To
create an account for the simulator, aim any of the commands at that file:

```bash
DYNACONF_AUTH__DATABASE_PATH=run/simulation/users.db \
  uv run python -m lumi.api.manage create-user hliang16 --role admin
# or, after the stack has started once, take its settings:
source run/simulation/env.sh && uv run python -m lumi.api.manage create-user hliang16 --role admin
```

`start_simulation.sh --keep-database` makes the stack use `cfg/users.db` instead, so one
set of accounts serves both, but it also writes recordings to the real HDF5 folder.

**Broker account** — for a node, or a notebook/MCP session that talks to the bus
directly. The permissions are derived from `src/lumi/contracts`, so re-run it after any
contract change:

```bash
# a node account for the instrument host
uv run python scripts/apply_broker_permissions.py --host <broker> \
    --user lumi-node --role node --password <pw>

# an operator account for a notebook driving a growth
uv run python scripts/apply_broker_permissions.py --host <broker> \
    --user hliang16 --role operator --password <pw>
```

It needs the management plugin (`rabbitmq-plugins enable rabbitmq_management`) and an
admin broker login to authenticate with (`--admin-user` / `--admin-pass`, default
`guest`/`guest`, which only works from the broker host itself). `--dry-run` prints the
permissions the role would get without applying them. Once real accounts exist, delete
`guest`.

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

## The MCP server — the lab as tools for an LLM agent

`src/lumi/mcp/` exposes the lab as MCP tools, so an agent (Claude Code, Claude Desktop,
anything speaking MCP) can run a deposition, look back through past growths, and simulate
RHEED patterns. There is no console script; it's a module:

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

> **Point it at a broker only this stack uses.** Every node declares its exchanges as
> durable `topic` exchanges at startup. If the broker already carries exchanges of the
> same name with different settings — e.g. non-durable `direct` ones left by another
> messaging layer — RabbitMQ refuses the declaration:
>
> ```
> PRECONDITION_FAILED - inequivalent arg 'type' for exchange 'RHEED' in vhost '/':
> received 'topic' but current is 'direct'
> ```
>
> Use a dedicated vhost or a fresh broker, and delete any conflicting exchanges first
> (non-durable ones are dropped by a broker restart anyway).

The tools are generated, one per `(contract, capability, op)`, named
`experiment.driver.to_temperature` and so on — 116 of them today. Adding an op to an
exposed capability makes it a tool with no change here. The surface is deliberately
narrower than the browser bridge's, and set by `EXPOSED` in `src/lumi/mcp/server.py`:

| Job | Tools |
|---|---|
| Drive a growth | all of `experiment.driver`; `rheed.camera`; `rheed.integrator.bboxes` / `cache` (the live oscillation of each box the operator drew); `chamber.log`; `chamber.camera`; `system.registry.list_nodes` / `get_node` (which nodes are up) |
| Read the history | the growth database through `experiment.driver` (`list_samples`, `sample_history`, `list_records`, `list_measurements`, `list_snapshots`, …); the recordings through `storage.archive` (frames, RHEED integration traces, the chamber log each file carries) |
| Simulate | all of `simulation.rheed_sim`: structures, spot positions, patterns |

Nothing else: an agent has no business reaching `system.supervisor.spawn`, raw MI script
execution (`chamber.mi_mode`), or rearranging the operator's integration boxes.

- **Read-only hints.** Every tool carries MCP's `readOnlyHint`, from the same
  classification the broker enforces (`lumi.contracts.policy`), so a host can auto-approve
  the reads and ask before anything that changes the chamber.
- **Pictures.** Camera frames, recorded frames and simulated patterns come back as images
  the model can see, with the real pixel range in the text beside them.
  `experiment.driver.take_snapshot` keeps a frame from either camera with the growth,
  labelled by stage (`start`, `heated`, `depo_start`, `depo_mid`, `depo_end`, `cooled`,
  `other`): losslessly as `.npy` and as a JPEG, in a `snapshots` folder beside
  `growth.db`.
- **Big answers.** A result over 40,000 characters (a recording's frame times, a
  4000-point trace) comes back with its long lists shortened and a note saying so;
  `max_points`, `since`/`until` and `limit` get them whole.
- **Uploads.** `experiment.driver.attach_measurement_file` takes the file as
  `content_text` or `content_base64` beside its metadata.
- **The journal.** Steps an agent takes are credited to `mcp` (stdio) or `mcp:<username>`
  (HTTP) in the step journal, so `sample_history` says who did what.

### Adding it to Claude Code

From the project you want to drive it from:

```bash
claude mcp add lumi -- \
  uv run --project /path/to/Lumi-Lab python -m lumi.mcp
```

`--project` matters: without it `uv run` resolves against whatever directory the MCP host
launched from, which is usually not this repo. Then `claude mcp list` should show
`✔ Connected`. Use `--scope project` instead of the default if you want the registration
written to a `.mcp.json` that travels with the repo; `claude mcp remove lumi`
undoes it. For Claude Desktop the same command line goes in `claude_desktop_config.json`
under `mcpServers`.

stdio needs no auth — it's a subprocess only you can spawn, the same trust level as any
other local tool.

The tools say what each op does; the **`lumi-lab` skill** (`skills/lumi-lab/`) says how
the lab is run: the limits, which steps need a person, the growth procedures, when to
stop, how to read RHEED, and how to keep the records honest. Install it in the same
project you registered the server in:

```bash
/path/to/Lumi-Lab/scripts/install_skill.sh            # this project (.claude/skills)
/path/to/Lumi-Lab/scripts/install_skill.sh --user     # every project (~/.claude/skills)
```

It is a symlink, so pulling this repo updates it.

### The HTTP transport

For an agent that is not on the lab machine. It always needs a logged-in **operator or
admin** account (viewers are refused), whatever `auth.enabled` says, so a dev-mode bypass
never opens equipment control to the network.

**There is no separate MCP token to obtain.** The server runs its own login: the agent's
host opens a browser, you sign in with a Lumi account, and the host keeps itself
renewed. A bearer token is only the fallback for clients that cannot open a browser.

**1. An account.** Operator or admin, in the user database the server reads (see
[Accounts](#accounts)):

```bash
uv run python scripts/create_api_user.py agent --role operator
# against the simulator stack, whose users live elsewhere:
uv run python scripts/create_api_user.py agent --role operator --database run/simulation/users.db
```

**2. Start the server.**

```bash
uv run python -m lumi.mcp --transport http --port 8100 --host <broker>
```

Against the simulator stack, run `source run/simulation/env.sh` first, so the server uses
the stack's broker and user database rather than `cfg/`.

**3. Connect: log in (the normal way).** Register it with no token and no header:

```bash
claude mcp add --transport http lumi http://127.0.0.1:8100/mcp
```

The first connection opens a browser at the server's login page. Sign in with the
account from step 1. The host receives an access token and a refresh token and renews by
itself from then on. Any MCP client that supports OAuth (Claude Code, Codex, …) works the
same way.

**Or: a bearer token**, for a client that cannot open a browser (a script, a headless
agent) or a server started with `--no-oauth`. It is the same JWT `POST /auth/login`
issues, it lasts 12 hours, and it cannot be renewed:

```bash
scripts/start_mcp_demo.sh --token-only        # simulator stack, which must run --with-auth
claude mcp add --transport http lumi http://127.0.0.1:8100/mcp \
  --header "Authorization: Bearer $(cat run/simulation/mcp_token.txt)"
```

| symptom | cause |
| --- | --- |
| login page refuses a correct password | the account is a viewer, or lives in a different user database from the server's (step 2) |
| 401 with a token from `/auth/login` | the stack runs with auth off, so the login handed back the inactive anonymous identity; restart it `--with-auth` |
| the client reports an issuer mismatch | `--public-url` does not match the address the client used |

**Off the lab machine.** The server speaks plain HTTP and the login form posts a
password, so beyond loopback put nginx/Caddy in front for TLS and pass its `https://`
address as `--public-url` (it becomes the OAuth issuer and every redirect target).
`--bind-host` defaults to `127.0.0.1`; anything else means your firewall is the only
thing between the internet and the chamber. Access tokens cannot be revoked before they
expire; revoking a login only stops its refresh.

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

## Notebooks

`notebooks/` holds the two operator notebooks, written to run against the simulator
as-is:

| notebook | what it does |
| --- | --- |
| `SingleDeposition.ipynb` | a layered growth on one position -- bottom electrode, interface, functional layer |
| `BODeposition.ipynb` | closed-loop Bayesian optimisation over growth conditions (needs `--extra opt`) |

Edit the `.py` source beside each one and run `uv run notebooks/build_notebooks.py`,
not the `.ipynb` -- see `notebooks/README.md`.

## Sample tracking

Every growth records itself. `sample` rows are created per growable position when a
substrate is registered, and every world-changing op writes a `step` row -- what was
asked, what happened, how long it took and who asked for it -- with no bookkeeping by
hand. A sample's layer stack is derived from the deposition steps that actually
succeeded, and results (RHEED metric, XRD, AFM, transport) attach to the sample rather
than to a CSV beside a notebook.

Reachable over the contract: `list_samples`, `get_sample`, `sample_history`,
`add_measurement`, `list_measurements`.

A measurement is more than its headline `value` (the scalar an optimiser ranks on):
`detail` holds named scalars, `series` holds curves -- an XRD scan, R(T), a loop -- with
named, unit-carrying axes, and `attach_measurement_file` uploads files: the instrument's
raw file, kept byte for byte whether or not anything parses it, and images or maps.
Files live in `measurement_files/` beside growth.db, up to
`experiment.measurement_file_max_bytes` (15 MiB) each. Design notes, the decisions behind it and the
open to-do are in `docs/SAMPLE_TRACKING.md`.

## Growth history

What a past growth left behind is readable over the contract, not only from a notebook
that knows the file layout:

- **samples and steps** -- the experiment node's `list_*` / `get_*` ops and
  `sample_history`, above;
- **RHEED recordings** -- `storage.archive`: `list_recordings`, `recording_info` (frame
  times, integration boxes, log columns), `recording_frame` (lossless NPY),
  `recording_frame_jpeg` (for a browser; contrast fixed per recording),
  `recording_integration` (the oscillation curves) and `recording_log`. A recording is
  named by its file stem, which is `record_name` in growth.db;
- **chamber logs** -- `chamber.log`: `list_log_files`, and `log_window`, which reads a
  time range across however many PASCAL log files it spans.

To have something to look at on the simulator:

```bash
.venv/bin/python scripts/seed_history.py           # ~3 weeks of fake growths, ~400 MB
.venv/bin/python scripts/seed_history.py --purge   # remove exactly what it added
```

It writes growth.db rows, one HDF5 recording per deposition and one chamber-log CSV per
session, all driven off the chamber simulator so the three agree. Safe to run against a
live simulation stack.

## RHEED simulation

`nodes/simulation.py` computes what a RHEED pattern should look like: give it a crystal,
the surface it is cut along, the beam and the screen, and it returns the picture and
every rod and spot on it, labelled. The same request works from a notebook:

```python
from lumi.rheedsim import simulate
from lumi.contracts.payloads.simulation import RheedSimRequest, StructureSpec, SurfaceSpec, BeamSpec

meta, image = simulate(RheedSimRequest(
    structure=StructureSpec(name="SrTiO3"),              # or cif="...", or manual=...
    surface=SurfaceSpec(normal=[0, 0, 1], azimuth=[1, 0, 0], termination="TiO2"),
    beam=BeamSpec(energy_kev=20, incidence_deg=3),
))
```

- **Structures** come from a CIF (symmetry applied), a cell typed in by hand (lattice,
  space group, sites), or the built-ins (SrTiO3, LSMO, LaFeO3 and LaAlO3 pseudocubic,
  LSAT, YSZ, MgO, Al2O3, rutile, Si, GaAs). `save_structure` keeps one by name.
- **Orientation** is the surface plane (hkl) and the beam's direction along it [uvw],
  plus an azimuth offset for off-axis patterns. The simulator finds the primitive
  surface mesh, including a centred lattice's (Si(111) is 3.84 A, not 7.68), and lists
  the atomic planes a surface can end on.
- **Morphology**: terrace size (spots become streaks), a share of 3D islands
  (transmission spots), and reconstructions as supercell matrices -- `[[2,0],[0,1]]` is
  2x1.
- **Screen**: camera length, pixel size on the screen, the shadow-edge origin, roll and
  flips. A request that names none gets the lab camera's, from
  `[simulation.rheed.screen]` (fitted to real recordings).
- **Beam position**: `beam.shift_y_mm` / `shift_z_mm` move the beam off the camera axis
  (+y left looking down the beam, +z up), as the lab's beam deflection does. The whole
  pattern moves with it -- spots, shadow edge, direct beam -- by the same millimetres;
  the result's `origin_px` says where the shadow edge's centre ended up.
- **Energy**: a request that names none gets the lab's `rheed.energy_kev`, 25 keV. The
  same value is stored in every recording (`start_recording`'s `rheed_energy_kev`
  overrides it) and read back by `storage.archive`.

It is kinematic: positions are exact geometry, intensities are single-scattering and
qualitative -- no Kikuchi lines, no refraction, and a specular spot that is often weaker
than the lab's. `rheed_spots` (the spot list alone, tens of ms) is meant for overlaying on
the live camera; `simulate_rheed_jpeg` for showing the pattern; `simulate_rheed` for
analysis. The equations, and where each lives in the code, are in
[docs/RHEED_SIMULATION.md](docs/RHEED_SIMULATION.md).

## Contracts and generated code

`src/lumi/contracts/` is the single source of truth. After changing it, regenerate:

```bash
uv run lumi-codegen              # writes the Python clients, web/src/generated/lumi.ts, schemas/contract.json
uv run lumi-codegen --check      # CI gate: fails if anything on disk has drifted
```

The contract hash printed by those commands gates backend/frontend compatibility — if it
changes, the frontend needs the regenerated `lumi.ts`. See `docs/FRONTEND_MIGRATION.md`
for how the browser-side API maps onto the bridge.

## Citation

If this software is useful in your research, please cite:

```bibtex
@article{liang2026autonomous,
  title   = {Autonomous epitaxial atomic-layer synthesis via real-time computer
             vision of electron diffraction},
  author  = {Liang, Haotong and Sun, Yunlong and Paxson, Ryan and Lee, Chih-Yu and
             Hall, Alex T. and Warecki, Zoey and Cumings, John and Koinuma, Hideomi and
             Kusne, Aaron Gilad and Lippmaa, Mikk and Takeuchi, Ichiro},
  journal = {arXiv preprint arXiv:2602.20432},
  year    = {2026},
  url     = {https://arxiv.org/abs/2602.20432}
}
```

## License

[MIT](LICENSE) © 2026 Haotong Liang and the Lumi-Lab contributors.
