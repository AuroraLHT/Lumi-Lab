#!/usr/bin/env bash
#
# Start every node in simulation mode on the contract-nodes stack: no Pylon
# camera, no webcam, no real chamber log folder, no MI mode backend. Every feed
# comes from the simulated sources bundled under src/lumi/*/assets. The monitor
# node tracks presence; the API bridges the browser to the bus over /ws.
#
# The chamber runs the state-model simulator (`nodes/pascal.py --src sim`): MI
# commands drive a chamber model and the growth log is rendered from it, so a
# deposition really does advance LaserPuls and a temperature set really does ramp.
# --chamber-speed compresses simulated time for impatient end-to-end runs.
#
# Usage:
#   scripts/start_simulation.sh [--host HOST] [--with-detection] [--with-agent]
#                               [--with-auth] [--with-experiment] [--keep-database]
#                               [--no-reset-broker] [--chamber-speed N]
#                               [--experiment-speed N]
#
# --chamber-speed compresses the chamber's own clock. --experiment-speed (defaults to
# the same value) compresses the experiment node's wall-clock warm-up pacing, which the
# chamber's clock cannot reach -- without it a growth waits ~280s before it starts, no
# matter how fast the chamber is.
#
# On a localhost broker it first deletes any contract exchange whose type has
# drifted (the old stack left RHEED/CHAMBER/STORAGE as `direct`; the contract now
# wants `topic`), so the nodes can redeclare them cleanly. --no-reset-broker
# skips that. Ctrl-C shuts every node down.

# Run this directly (scripts/start_simulation.sh ...) -- do not `source`/`. ` it. In an
# interactive shell, sourcing runs the script's background jobs and trap as jobs of
# your own shell's job control, and bash's interactive SIGINT handling for that case
# silently bypasses this script's own `trap ... INT` -- Ctrl-C then just aborts back to
# your prompt without ever running the shutdown/kill-TERM loop below, leaving every node
# process orphaned. Confirmed empirically: identical script, `bash script.sh` + Ctrl-C
# runs the trap and cleans up; `. script.sh` + Ctrl-C does not, even though `kill -INT
# $$` on the same sourced shell does trigger it -- so it is specifically about how the
# terminal's SIGINT interacts with an interactive shell's foreground job control, not
# the trap logic itself. This check has to run before `set -euo pipefail` below, or
# sourcing has already mutated the caller's shell options by the time we bail out.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    echo "error: don't source this script (you ran '. ${BASH_SOURCE[0]}' or 'source ...')." >&2
    echo "  Run it directly instead: scripts/start_simulation.sh $*" >&2
    echo "  Sourcing breaks Ctrl-C cleanup -- see the comment above this check." >&2
    return 1
fi

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

RABBITMQ_HOST="localhost"
WITH_DETECTION=0
WITH_AGENT=0
WITH_AUTH=0
WITH_EXPERIMENT=0
KEEP_DATABASE=0
RESET_BROKER=1
CHAMBER_SPEED=1
EXPERIMENT_SPEED=

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) RABBITMQ_HOST="$2"; shift 2 ;;
        --with-detection) WITH_DETECTION=1; shift ;;
        --with-agent) WITH_AGENT=1; shift ;;
        --with-auth) WITH_AUTH=1; shift ;;
        --with-experiment) WITH_EXPERIMENT=1; shift ;;
        --keep-database) KEEP_DATABASE=1; shift ;;
        --no-reset-broker) RESET_BROKER=0; shift ;;
        --chamber-speed) CHAMBER_SPEED="$2"; shift 2 ;;
        --experiment-speed) EXPERIMENT_SPEED="$2"; shift 2 ;;
        -h|--help) sed -n '3,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

RUN_DIR="$PROJECT_ROOT/run/simulation"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "no virtualenv at .venv -- run 'uv sync --extra api' first" >&2
    exit 1
fi

# The API bridge reads its broker URL from LUMI_AMQP_URL first (see
# lumi/api/main.py:_amqp_url), so point it explicitly at the simulation broker
# rather than the lab IP baked into settings.toml.
export LUMI_AMQP_URL="amqp://guest:guest@$RABBITMQ_HOST:5672/"
# One uvicorn worker: the sim is single-machine, and the default (10) just
# multiplies broker connections and log noise.
export DYNACONF_API__WORKERS=1

# Keep the simulation off the real user database: point auth at a scratch sqlite
# file under the run dir unless asked to keep it.
if [[ $KEEP_DATABASE -eq 0 ]]; then
    export DYNACONF_AUTH__DATABASE_PATH="$RUN_DIR/users.db"
fi

if [[ $WITH_AUTH -eq 1 ]]; then
    # Exercise the real login path: bootstrap a throwaway admin so the UI is
    # reachable. dev_mode stays on so the placeholder secret_key is accepted.
    export DYNACONF_AUTH__ENABLED=true
    export DYNACONF_AUTH__DEFAULT_ADMIN_USERNAME=admin
    export DYNACONF_AUTH__DEFAULT_ADMIN_PASSWORD=simulation
    AUTH_NOTE="auth ON -- log in as admin / simulation"
else
    # Default: wide open, so the browser reaches /ws without a login flow.
    export DYNACONF_AUTH__ENABLED=false
    AUTH_NOTE="auth OFF (wide open) -- pass --with-auth to require login"
fi

# HDF5 recorder output. The checked-in `database` symlink points at the lab share
# (/mnt/FastTerp), which is not mounted outside the lab, and we do not want
# simulated runs landing in the real records anyway -- send it at a scratch dir.
STORAGE_ARGS=(--host "$RABBITMQ_HOST")
if [[ $KEEP_DATABASE -eq 0 ]]; then
    mkdir -p "$RUN_DIR/database"
    STORAGE_ARGS+=(--root "$RUN_DIR/database")
fi

if ! timeout 3 bash -c "cat < /dev/null > /dev/tcp/$RABBITMQ_HOST/5672" 2>/dev/null; then
    echo "RabbitMQ is not reachable at $RABBITMQ_HOST:5672" >&2
    echo "start it first, e.g. 'docker run -p 5672:5672 -p 15672:15672 rabbitmq:3-management'" >&2
    exit 1
fi

# The old messaging layer declared its exchanges as `direct`; the contract now
# declares them `topic`, and RabbitMQ refuses to redeclare an exchange with a
# different type -- the storage node dies on startup with PRECONDITION_FAILED.
# Delete only the contract exchanges whose type has actually drifted, so the
# nodes can recreate them; correctly-typed exchanges are left alone. Localhost
# only, since this is destructive on a shared broker.
reset_stale_exchanges() {
    "$PYTHON" - "$LUMI_AMQP_URL" <<'PY'
import asyncio
import sys

from aio_pika import ExchangeType, connect_robust
from aiormq.exceptions import ChannelPreconditionFailed

from lumi.contracts.registry import REGISTRY


async def main(url: str) -> None:
    conn = await connect_robust(url, timeout=5.0)
    try:
        wanted = {c.exchange: c.exchange_type for c in REGISTRY.values()}
        for name, xtype in wanted.items():
            channel = await conn.channel()
            try:
                # A normal (non-passive) declare enforces type/durability
                # equivalence: it creates the exchange if absent, is a no-op if it
                # already matches, and raises if an existing one has a different
                # type. (A *passive* declare would only check existence and ignore
                # the type, so it cannot detect the drift we are fixing.)
                await channel.declare_exchange(name, ExchangeType(xtype), durable=True)
                await channel.close()
            except ChannelPreconditionFailed:
                # Left over from the old stack with a different type: delete it and
                # recreate with the type the contract expects.
                fresh = await conn.channel()
                exchange = await fresh.get_exchange(name, ensure=False)
                await exchange.delete(if_unused=False)
                await fresh.declare_exchange(name, ExchangeType(xtype), durable=True)
                await fresh.close()
                print(f"  reset exchange {name} (type drift) -> {xtype}")
    finally:
        await conn.close()


asyncio.run(main(sys.argv[1]))
PY
}

if [[ $RESET_BROKER -eq 1 ]]; then
    case "$RABBITMQ_HOST" in
        localhost|127.0.0.1|::1)
            echo "checking for exchanges left over from the old (direct) stack..."
            reset_stale_exchanges || echo "  (skipped: could not reach the broker to reset)" ;;
        *)
            echo "note: --host is not localhost; skipping exchange reset" >&2 ;;
    esac
fi

PIDS=()
NAMES=()

shutdown() {
    trap - INT TERM EXIT
    echo
    echo "shutting down..."
    # Reverse order so producers stop before the consumers they feed.
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
        if kill -0 "${PIDS[$i]}" 2>/dev/null; then
            kill -TERM "${PIDS[$i]}" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    echo "all nodes stopped"
}

# On a signal, exit straight from the handler: otherwise the monitor loop below
# gets one more iteration and reports the nodes we just killed as crashed.
on_signal() { shutdown; exit 0; }
trap on_signal INT TERM
trap shutdown EXIT

start_node() {
    local name="$1"; shift
    echo "starting $name -> $LOG_DIR/$name.log"
    "$PYTHON" "nodes/$name.py" "$@" > "$LOG_DIR/$name.log" 2>&1 &
    PIDS+=($!)
    NAMES+=("$name")
}

# Monitor first: it owns the SYSTEM presence registry and should be listening
# before any node publishes its first heartbeat.
start_node monitor --host "$RABBITMQ_HOST"

# Storage next: it declares the RHEED / CHAMBER / SYSTEM exchanges the producers
# publish into and the recorder binds to, so nothing races on a missing exchange.
start_node storage "${STORAGE_ARGS[@]}"
sleep 2

start_node pascal --host "$RABBITMQ_HOST" --src sim --speed "$CHAMBER_SPEED"
start_node rheed  --host "$RABBITMQ_HOST" --src simcam

if [[ $WITH_AGENT -eq 1 ]]; then
    # Per-host supervisor (spawn/kill). Only does anything with an [agent.nodes]
    # allowlist in settings; harmless otherwise.
    start_node agent --host "$RABBITMQ_HOST"
fi

if [[ $WITH_DETECTION -eq 1 ]]; then
    # Needs torch/mmcv/mmdet/rhana, which pyproject.toml deliberately leaves out
    # (no mmcv wheel for 3.12, rhana is not on PyPI). Run
    # scripts/install_detection_deps.sh first.
    start_node detection --host "$RABBITMQ_HOST"
fi

if [[ $WITH_EXPERIMENT -eq 1 ]]; then
    # The experiment node paces parts of a growth in *wall* time of its own, which
    # --chamber-speed cannot touch: the warm-up ramp sleeps
    # `warm_up_step / warm_up_current_ramp_rate` per current step, and then waits up to
    # `warm_up_max_waittime` for the substrate to reach the PID-engage threshold. At the
    # shipped values that is ~280s before a growth even starts, however fast the chamber
    # is running. Scale those bounds by the same factor so the whole simulated stack
    # runs on one clock -- the chamber's thermal lag is accelerated by the same amount,
    # so the relationship between them is preserved.
    #
    # Simulation only. These are real machine limits on the lab node: the ramp rate is
    # how fast the heater may be driven, not a polling interval.
    : "${EXPERIMENT_SPEED:=$CHAMBER_SPEED}"
    if awk "BEGIN{exit !($EXPERIMENT_SPEED > 1)}"; then
        DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_CURRENT_RAMP_RATE=$(
            awk "BEGIN{printf \"%.6f\", 0.015 * $EXPERIMENT_SPEED}")
        DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_MAX_WAITTIME=$(
            awk "BEGIN{printf \"%.3f\", 180.0 / $EXPERIMENT_SPEED}")
        # Floored: below ~50ms the poll costs more than it saves, and it is an RPC.
        DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_WAIT_INTERVAL=$(
            awk "BEGIN{v = 0.2 / $EXPERIMENT_SPEED; printf \"%.3f\", (v < 0.05 ? 0.05 : v)}")
        export DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_CURRENT_RAMP_RATE
        export DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_MAX_WAITTIME
        export DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_WAIT_INTERVAL
        echo "experiment node: warm-up bounds scaled ${EXPERIMENT_SPEED}x for simulation"
        echo "  ramp rate ${DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_CURRENT_RAMP_RATE} A/s," \
             "max wait ${DYNACONF_EXPERIMENT__BOUNDS__WARM_UP_MAX_WAITTIME}s"
    fi
    # A consumer of chamber/rheed/storage, like storage is of rheed/chamber -- needs
    # them already up, which they are by this point in the script.
    start_node experiment --host "$RABBITMQ_HOST"
fi

sleep 2
start_node api

echo
echo "nodes running (Ctrl-C to stop):"
for i in "${!NAMES[@]}"; do
    printf "  %-10s pid %s\n" "${NAMES[$i]}" "${PIDS[$i]}"
done
[[ $WITH_AGENT -eq 0 ]] && echo "  agent      skipped (pass --with-agent)"
[[ $WITH_DETECTION -eq 0 ]] && echo "  detection  skipped (pass --with-detection once its deps are installed)"
[[ $WITH_EXPERIMENT -eq 0 ]] && echo "  experiment skipped (pass --with-experiment)"
echo
echo "API on http://localhost:8000 -- $AUTH_NOTE"
echo "logs in $LOG_DIR"
echo
echo "to make the chamber actually do something, run a growth alongside this:"
echo "  uv run scripts/demo_growth.py --speed $CHAMBER_SPEED       # chamber node, raw MI"
if [[ $WITH_EXPERIMENT -eq 1 ]]; then
    echo "  uv run scripts/demo_experiment.py --speed $CHAMBER_SPEED   # experiment node, as a notebook would"
else
    echo "  (restart with --with-experiment for scripts/demo_experiment.py, which drives"
    echo "   the same growth through the experiment node instead)"
fi

# Exit as soon as any node dies, rather than sitting on a half-dead stack.
while true; do
    for i in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
            echo "node '${NAMES[$i]}' exited -- see $LOG_DIR/${NAMES[$i]}.log" >&2
            exit 1
        fi
    done
    sleep 2
done
