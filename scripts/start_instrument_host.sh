#!/usr/bin/env bash
#
# Start the instrument-side half of the production stack on ONE machine:
#
#   pascal   the PLD chamber -- growth log, PLDconfig.ini, MI mode, chamber webcam
#   rheed    RHEED camera acquisition (Basler/Pylon)
#
# The server-side half (monitor, storage, detection, api) runs on the other machine
# -- scripts/start_server_host.sh. Start that one FIRST: its storage node declares
# the exchanges these two publish into.
#
# The broker is NOT on this machine, so --host is required.
#
# Usage:
#   scripts/start_instrument_host.sh --host BROKER [--user U] [--password P]
#                                    [--log DIR] [--mi DIR] [--pld-config FILE]
#                                    [--src path|sim|test] [--rheed-src pylon|webcam|simcam]
#                                    [--speed N] [--no-rheed] [--no-pascal] [--check]
#
#   --host        broker host (required -- it lives on the server machine)
#   --user/-p     broker credentials. RabbitMQ refuses 'guest' off loopback, so a
#                 real account is required here; see scripts/apply_broker_permissions.py
#   --log         folder PASCAL writes the chamber log into   (required for --src path)
#   --mi          the real MI mode folder                     (required for --src path)
#   --pld-config  PLDconfig.ini (default: the Windows path in cfg/settings.toml)
#   --src         chamber source; 'path' is the real chamber and is the default here
#   --rheed-src   RHEED camera source (default pylon)
#   --speed       simulated-time multiplier, --src sim only
#   --check       run the preflight checks and exit without starting anything
#
# Ctrl-C shuts both nodes down.

# Run this directly -- do not `source`/`. ` it. Sourcing makes the nodes jobs of your
# interactive shell, whose SIGINT handling bypasses the trap below, so Ctrl-C orphans
# every node instead of shutting it down. This check must precede `set -euo pipefail`,
# or sourcing has already mutated the caller's shell options by the time we bail.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    echo "error: don't source this script (you ran '. ${BASH_SOURCE[0]}' or 'source ...')." >&2
    echo "  Run it directly instead: scripts/start_instrument_host.sh $*" >&2
    return 1
fi

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

RABBITMQ_HOST=""
BROKER_USER="guest"
BROKER_PASS="guest"
CHAMBER_LOG=""
MI_FOLDER=""
PLD_CONFIG=""
CHAMBER_SRC="path"
RHEED_SRC="pylon"
CHAMBER_SPEED="1"
WITH_RHEED=1
WITH_PASCAL=1
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) RABBITMQ_HOST="$2"; shift 2 ;;
        --user) BROKER_USER="$2"; shift 2 ;;
        --password|-p) BROKER_PASS="$2"; shift 2 ;;
        --log) CHAMBER_LOG="$2"; shift 2 ;;
        --mi) MI_FOLDER="$2"; shift 2 ;;
        --pld-config) PLD_CONFIG="$2"; shift 2 ;;
        --src) CHAMBER_SRC="$2"; shift 2 ;;
        --rheed-src) RHEED_SRC="$2"; shift 2 ;;
        --speed) CHAMBER_SPEED="$2"; shift 2 ;;
        --no-rheed) WITH_RHEED=0; shift ;;
        --no-pascal) WITH_PASCAL=0; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '3,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$RABBITMQ_HOST" ]]; then
    echo "error: --host is required -- the broker runs on the server machine, not here." >&2
    echo "  scripts/start_instrument_host.sh --host <server ip> --user <node user> --password <pw> \\" >&2
    echo "      --log <chamber log folder> --mi <MI mode folder>" >&2
    exit 1
fi

RUN_DIR="$PROJECT_ROOT/run/production"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

PYTHON="$PROJECT_ROOT/.venv/bin/python"

fail() { echo "  FAIL  $*" >&2; FAILED=1; }
ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*" >&2; }

FAILED=0
echo "preflight (instrument host):"

# ---- interpreter and packages ----------------------------------------------
if [[ ! -x "$PYTHON" ]]; then
    echo "  FAIL  no virtualenv at .venv" >&2
    echo "        uv sync --extra pascal --extra camera" >&2
    exit 1
fi
ok "venv at .venv ($("$PYTHON" -V 2>&1))"

check_import() {
    local module="$1" what="$2" fix="$3"
    if "$PYTHON" -c "import $module" 2>/dev/null; then
        ok "$what"
    else
        fail "$what missing -- $fix"
    fi
}
if [[ $WITH_PASCAL -eq 1 ]]; then
    check_import watchdog "pascal extra (watchdog)" "uv sync --extra pascal"
    check_import pandas   "pascal extra (pandas)"   "uv sync --extra pascal"
fi
if [[ $WITH_RHEED -eq 1 || $WITH_PASCAL -eq 1 ]]; then
    check_import cv2 "camera extra (opencv)" "uv sync --extra camera"
    check_import av  "camera extra (av)"     "uv sync --extra camera"
fi

# ---- contract hash ----------------------------------------------------------
# Both halves must be built from the same contract; a drift here shows up as ops the
# server host does not recognise, not as a startup error.
CONTRACT_HASH="$("$PYTHON" -c 'from lumi.contracts import contract_hash; print(contract_hash())' 2>/dev/null || echo "?")"
ok "contract hash $CONTRACT_HASH (must match the server host)"

# ---- broker ------------------------------------------------------------------
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$RABBITMQ_HOST/5672" 2>/dev/null; then
    ok "broker reachable at $RABBITMQ_HOST:5672"
else
    fail "broker unreachable at $RABBITMQ_HOST:5672"
    echo "        Is scripts/start_server_host.sh running on that machine, and is 5672 open?" >&2
fi

# RabbitMQ's default `loopback_users` blocks guest from anywhere but loopback. This
# host is by definition remote from the broker, so guest can never work here.
case "$RABBITMQ_HOST" in
    localhost|127.0.0.1|::1)
        warn "--host is loopback: this script is meant for the machine the broker is NOT on" ;;
    *)
        if [[ "$BROKER_USER" == "guest" ]]; then
            fail "RabbitMQ refuses the 'guest' account off loopback -- pass --user/--password."
            echo "        On the server host, create a node account:" >&2
            echo "        uv run python scripts/apply_broker_permissions.py --host $RABBITMQ_HOST \\" >&2
            echo "            --user lumi-node --role node --password <pw>" >&2
        else
            ok "broker user '$BROKER_USER'"
        fi ;;
esac

# ---- chamber -----------------------------------------------------------------
if [[ $WITH_PASCAL -eq 1 ]]; then
    case "$CHAMBER_SRC" in
        path|sim|test) ;;
        *) fail "--src must be one of path, sim, test (got '$CHAMBER_SRC')" ;;
    esac

    if [[ "$CHAMBER_SRC" == "path" ]]; then
        # The node raises FileNotFoundError on a missing log and ValueError on a missing
        # --mi, both after it has already connected. Catch them here instead.
        if [[ -z "$CHAMBER_LOG" ]]; then
            fail "--log is required with --src path (the folder PASCAL writes the growth log into)"
        elif [[ ! -e "$CHAMBER_LOG" ]]; then
            fail "chamber log path does not exist: $CHAMBER_LOG"
        else
            ok "chamber log $CHAMBER_LOG"
        fi

        if [[ -z "$MI_FOLDER" ]]; then
            fail "--mi is required with --src path (the MI mode folder the controller reads)"
        elif [[ ! -d "$MI_FOLDER" ]]; then
            fail "MI mode folder does not exist: $MI_FOLDER"
        elif [[ ! -w "$MI_FOLDER" ]]; then
            fail "MI mode folder is not writable: $MI_FOLDER"
            echo "        The node writes the script and assist files there; read-only means" >&2
            echo "        every command silently fails to reach the controller." >&2
        else
            ok "MI mode folder $MI_FOLDER (writable)"
        fi

        # PLDconfig.ini gives the carousel geometry, so `Select Target C` resolves to an
        # angle. Its default is a Windows path that will not exist on a Linux host.
        RESOLVED_PLD="${PLD_CONFIG:-$("$PYTHON" -c 'from lumi.config import settings; print(settings.pascal.config_reader.config_path)' 2>/dev/null || true)}"
        if [[ -z "$RESOLVED_PLD" ]]; then
            fail "could not resolve the PLDconfig.ini path"
        elif [[ ! -r "$RESOLVED_PLD" ]]; then
            fail "PLDconfig.ini not readable: $RESOLVED_PLD"
            echo "        Pass --pld-config <file>, or fix [pascal.config_reader] in cfg/settings.toml." >&2
        else
            ok "PLDconfig.ini $RESOLVED_PLD"
        fi
    else
        warn "--src $CHAMBER_SRC: this is NOT the real chamber (use --src path on the instrument)"
    fi
fi

# ---- RHEED camera ------------------------------------------------------------
if [[ $WITH_RHEED -eq 1 ]]; then
    case "$RHEED_SRC" in
        pylon)
            if ! "$PYTHON" -c "import pypylon.pylon" 2>/dev/null; then
                fail "pypylon is not importable -- uv sync --extra camera"
            else
                # The node raises "no Basler camera found" at startup; enumerating here
                # turns that into a preflight line instead of a crashed node.
                DEVICES="$("$PYTHON" -c 'from lumi.base.camera.pylon_camera import list_devices; print(len(list_devices()))' 2>/dev/null || echo "error")"
                if [[ "$DEVICES" == "error" ]]; then
                    fail "could not enumerate Basler devices"
                elif [[ "$DEVICES" == "0" ]]; then
                    fail "no Basler camera found -- check the network cable and the camera's IP"
                else
                    ok "$DEVICES Basler camera(s) found"
                fi
            fi ;;
        webcam|simcam)
            warn "--rheed-src $RHEED_SRC: not the Basler camera (use pylon on the instrument)" ;;
        *) fail "--rheed-src must be one of pylon, webcam, simcam (got '$RHEED_SRC')" ;;
    esac
fi

# ---- competing consumers -----------------------------------------------------
# Two chamber nodes on one broker are competing consumers on the same queue and
# round-robin the RPCs between them, which reads as random lag in the UI.
RUNNING="$(pgrep -af 'nodes/(pascal|rheed)\.py' 2>/dev/null || true)"
if [[ -n "$RUNNING" ]]; then
    warn "pascal/rheed already running on this host -- two nodes on one broker round-robin"
    warn "the RPCs between them. Stop them first: scripts/stop_nodes.sh"
fi

if [[ $FAILED -ne 0 ]]; then
    echo >&2
    echo "preflight failed -- nothing started." >&2
    exit 1
fi

echo "preflight passed."
if [[ $CHECK_ONLY -eq 1 ]]; then
    exit 0
fi
echo

# config_reader has no CLI flag, so --pld-config has to reach it through dynaconf.
if [[ -n "$PLD_CONFIG" ]]; then
    export DYNACONF_PASCAL__CONFIG_READER__CONFIG_PATH="$PLD_CONFIG"
fi

PIDS=()
NAMES=()

shutdown() {
    trap - INT TERM EXIT
    echo
    echo "shutting down..."
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
        if kill -0 "${PIDS[$i]}" 2>/dev/null; then
            kill -TERM "${PIDS[$i]}" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    echo "all nodes stopped"
}

# Exit straight from the handler: otherwise the monitor loop below gets one more
# iteration and reports the nodes we just killed as crashed.
on_signal() { shutdown; exit 0; }
trap on_signal INT TERM
trap shutdown EXIT

start_node() {
    local name="$1"; shift
    echo "starting $name -> $LOG_DIR/$name.log"
    "$PYTHON" "nodes/$name.py" "$@" >> "$LOG_DIR/$name.log" 2>&1 &
    PIDS+=($!)
    NAMES+=("$name")
}

if [[ $WITH_PASCAL -eq 1 ]]; then
    PASCAL_ARGS=(--host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS" --src "$CHAMBER_SRC")
    [[ -n "$CHAMBER_LOG" ]] && PASCAL_ARGS+=(--log "$CHAMBER_LOG")
    [[ -n "$MI_FOLDER"   ]] && PASCAL_ARGS+=(--mi "$MI_FOLDER")
    [[ "$CHAMBER_SRC" == "sim" ]] && PASCAL_ARGS+=(--speed "$CHAMBER_SPEED")
    start_node pascal "${PASCAL_ARGS[@]}"
fi

if [[ $WITH_RHEED -eq 1 ]]; then
    start_node rheed --host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS" --src "$RHEED_SRC"
fi

echo
echo "nodes running (Ctrl-C to stop):"
for i in "${!NAMES[@]}"; do
    printf "  %-10s pid %s\n" "${NAMES[$i]}" "${PIDS[$i]}"
done
[[ $WITH_PASCAL -eq 0 ]] && echo "  pascal     skipped (--no-pascal)"
[[ $WITH_RHEED  -eq 0 ]] && echo "  rheed      skipped (--no-rheed)"
echo
echo "publishing to $RABBITMQ_HOST:5672 as '$BROKER_USER' -- contract $CONTRACT_HASH"
echo "logs in $LOG_DIR"

# Exit as soon as either node dies, rather than sitting on a half-dead stack.
while true; do
    for i in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
            echo "node '${NAMES[$i]}' exited -- see $LOG_DIR/${NAMES[$i]}.log" >&2
            exit 1
        fi
    done
    sleep 2
done
