#!/usr/bin/env bash
#
# Start the server-side half of the production stack on ONE machine:
#
#   monitor    presence registry (see --no-monitor)
#   storage    HDF5 recorder, and the node that declares the exchanges
#   detection  RHEED spot detection (see --no-detection)
#   api        FastAPI + the /ws bridge the browser connects to
#
# The instrument-side half (pascal, rheed) runs on the other machine --
# scripts/start_instrument_host.sh. This script assumes the RabbitMQ broker is on
# THIS machine; pass --host to point it elsewhere.
#
# Usage:
#   scripts/start_server_host.sh [--host HOST] [--user U] [--password P]
#                                [--root DIR] [--workers N]
#                                [--no-detection] [--no-monitor]
#                                [--allow-insecure-auth] [--check]
#
#   --host        broker host (default localhost -- the broker runs here)
#   --user/-p     broker credentials (default guest/guest; guest only works
#                 over loopback, which is exactly the case this script defaults to)
#   --root        HDF5 output directory (default storage.hdf5_recorder.database_path)
#   --workers     uvicorn workers for the api node (default: api.workers in settings)
#   --check       run the preflight checks and exit without starting anything
#
# Unlike start_simulation.sh this writes to the REAL HDF5 root and the REAL user
# database, and it refuses to start with authentication disabled or with the
# placeholder signing key. Ctrl-C shuts every node down.

# Run this directly -- do not `source`/`. ` it. Sourcing makes the nodes jobs of your
# interactive shell, whose SIGINT handling bypasses the trap below, so Ctrl-C orphans
# every node instead of shutting it down. This check must precede `set -euo pipefail`,
# or sourcing has already mutated the caller's shell options by the time we bail.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    echo "error: don't source this script (you ran '. ${BASH_SOURCE[0]}' or 'source ...')." >&2
    echo "  Run it directly instead: scripts/start_server_host.sh $*" >&2
    return 1
fi

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

RABBITMQ_HOST="localhost"
BROKER_USER="guest"
BROKER_PASS="guest"
STORAGE_ROOT=""
API_WORKERS=""
WITH_DETECTION=1
WITH_MONITOR=1
ALLOW_INSECURE_AUTH=0
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) RABBITMQ_HOST="$2"; shift 2 ;;
        --user) BROKER_USER="$2"; shift 2 ;;
        --password|-p) BROKER_PASS="$2"; shift 2 ;;
        --root) STORAGE_ROOT="$2"; shift 2 ;;
        --workers) API_WORKERS="$2"; shift 2 ;;
        --no-detection) WITH_DETECTION=0; shift ;;
        --no-monitor) WITH_MONITOR=0; shift ;;
        --allow-insecure-auth) ALLOW_INSECURE_AUTH=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '3,29p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

RUN_DIR="$PROJECT_ROOT/run/production"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

PYTHON="$PROJECT_ROOT/.venv/bin/python"

fail() { echo "  FAIL  $*" >&2; FAILED=1; }
ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*" >&2; }

FAILED=0
echo "preflight (server host):"

# ---- interpreter and packages ----------------------------------------------
if [[ ! -x "$PYTHON" ]]; then
    echo "  FAIL  no virtualenv at .venv" >&2
    echo "        uv sync --extra api --extra storage --extra detection" >&2
    exit 1
fi
ok "venv at .venv ($("$PYTHON" -V 2>&1))"

# The api node needs `api`, the storage node needs `storage`, the detection node
# needs `detection` plus the out-of-band torch/mmdet stack. Name the missing extra
# rather than letting the node die on an ImportError in its own log file.
check_import() {
    local module="$1" what="$2" fix="$3"
    if "$PYTHON" -c "import $module" 2>/dev/null; then
        ok "$what"
    else
        fail "$what missing -- $fix"
    fi
}
check_import fastapi   "api extra (fastapi)"   "uv sync --extra api"
check_import uvicorn   "api extra (uvicorn)"   "uv sync --extra api"
check_import aiosqlite "api extra (aiosqlite)" "uv sync --extra api"
check_import h5py      "storage extra (h5py)"  "uv sync --extra storage"

# ---- contract hash ----------------------------------------------------------
# The browser talks the contract its generated client was built from. A mismatch is
# not a startup failure -- it is silently wrong ops at runtime -- so surface it here.
CONTRACT_HASH="$("$PYTHON" -c 'from lumi.contracts import contract_hash; print(contract_hash())' 2>/dev/null || echo "?")"
ok "contract hash $CONTRACT_HASH (the frontend's generated client must match)"

# ---- broker ------------------------------------------------------------------
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$RABBITMQ_HOST/5672" 2>/dev/null; then
    ok "broker reachable at $RABBITMQ_HOST:5672"
else
    fail "broker unreachable at $RABBITMQ_HOST:5672 -- start RabbitMQ first"
fi

# RabbitMQ's default `loopback_users` blocks guest from anywhere but loopback, so a
# remote broker with guest credentials fails at connect time on every node at once.
case "$RABBITMQ_HOST" in
    localhost|127.0.0.1|::1) ;;
    *)
        if [[ "$BROKER_USER" == "guest" ]]; then
            fail "broker is remote but user is 'guest' -- RabbitMQ refuses guest off loopback."
            echo "        Create a node account first:" >&2
            echo "        uv run python scripts/apply_broker_permissions.py --host $RABBITMQ_HOST \\" >&2
            echo "            --user lumi-node --role node --password <pw>" >&2
        fi ;;
esac

# ---- auth --------------------------------------------------------------------
# This host serves the browser. Wide-open auth or the placeholder signing key would
# let anyone who can reach it drive the chamber, so both are refusals by default.
AUTH_REPORT="$("$PYTHON" - <<'PY' 2>/dev/null || echo "error"
from lumi.config import settings
from lumi.api.security import DEV_SECRET_KEY

enabled = bool(settings.get("auth.enabled", True))
dev_mode = bool(settings.get("auth.dev_mode", False))
placeholder = settings.get("auth.secret_key", DEV_SECRET_KEY) == DEV_SECRET_KEY
print(f"{enabled}|{dev_mode}|{placeholder}|{settings.get('auth.database_path', 'cfg/users.db')}")
PY
)"
if [[ "$AUTH_REPORT" == "error" ]]; then
    fail "could not read the auth settings"
else
    IFS='|' read -r AUTH_ENABLED AUTH_DEV AUTH_PLACEHOLDER AUTH_DB <<< "$AUTH_REPORT"
    if [[ "$AUTH_ENABLED" == "True" ]]; then
        ok "auth enabled (user database: $AUTH_DB)"
    elif [[ $ALLOW_INSECURE_AUTH -eq 1 ]]; then
        warn "auth DISABLED -- every connection is an administrator (--allow-insecure-auth)"
    else
        fail "auth.enabled is false: anyone who reaches this host could drive the chamber."
        echo "        Remove the override from cfg/.secrets.toml, or pass --allow-insecure-auth" >&2
        echo "        if this really is an isolated bench network." >&2
    fi
    if [[ "$AUTH_PLACEHOLDER" == "True" ]]; then
        if [[ $ALLOW_INSECURE_AUTH -eq 1 ]]; then
            warn "signing key is still the shipped placeholder"
        else
            fail "auth.secret_key is still the shipped placeholder -- tokens are forgeable."
            echo "        $PYTHON -m lumi.api.manage gen-secret   # then put it in cfg/.secrets.toml" >&2
            echo "        with dev_mode = false" >&2
        fi
    elif [[ "$AUTH_DEV" == "True" ]]; then
        warn "auth.dev_mode is true (a real key is set, so this only relaxes the placeholder check)"
    else
        ok "real signing key, dev_mode off"
    fi

    # Two ways the user database makes the API unusable while still starting cleanly:
    # no accounts at all (every login fails), or a `users` table left over from the
    # pre-contract backend. db.SCHEMA is CREATE TABLE IF NOT EXISTS with no migration,
    # so an old table without the `role` column survives untouched and every read of a
    # user then raises IndexError inside a request.
    if [[ "$AUTH_ENABLED" == "True" ]]; then
        DB_REPORT="$("$PYTHON" - "$AUTH_DB" <<'DBPY' 2>&1 || true
import sqlite3, sys
from pathlib import Path
from lumi.path import PROJECT_ROOT

path = Path(sys.argv[1])
if not path.is_absolute():
    path = PROJECT_ROOT / path
if not path.exists():
    print("missing")
    raise SystemExit(0)
db = sqlite3.connect(path)
tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
if "users" not in tables:
    print("missing")
    raise SystemExit(0)
columns = {r[1] for r in db.execute("PRAGMA table_info(users)")}
if "role" not in columns:
    print("stale-schema")
    raise SystemExit(0)
print("ok", db.execute("select count(*) from users").fetchone()[0])
DBPY
)"
        case "$DB_REPORT" in
            "stale-schema")
                fail "user database $AUTH_DB predates the contract backend (no 'role' column)."
                echo "        There is no migration, so the API starts and then fails every" >&2
                echo "        request that touches a user. Move it aside and recreate accounts:" >&2
                echo "          mv $AUTH_DB $AUTH_DB.pre-contract" >&2
                echo "          $PYTHON -m lumi.api.manage create-user <name> --role admin" >&2 ;;
            "missing"|"ok 0")
                fail "no accounts exist -- nobody can log in."
                echo "        $PYTHON -m lumi.api.manage create-user <name> --role admin" >&2 ;;
            "ok "*)
                ok "${DB_REPORT#ok } account(s) in $AUTH_DB" ;;
            *)
                fail "could not read the user database $AUTH_DB" ;;
        esac
    fi
fi

# ---- CORS --------------------------------------------------------------------
# The bridge rejects a browser origin that is not listed, and the failure shows up in
# the browser console rather than in any log on this host.
ORIGINS="$("$PYTHON" -c 'from lumi.config import settings; print(",".join(settings.get("api", {}).get("allow_origins", ["*"])))' 2>/dev/null || echo "*")"
if [[ "$ORIGINS" == "*" ]]; then
    warn "api.allow_origins is \"*\" -- tighten it to the frontend's origin for production"
else
    ok "api.allow_origins = $ORIGINS"
fi

# ---- HDF5 root ---------------------------------------------------------------
# The tracked `database` symlink points at the lab share. If it is not mounted the
# recorder starts anyway and every growth is silently lost, so check it up front.
RESOLVED_ROOT="$("$PYTHON" - "$STORAGE_ROOT" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path
from lumi.config import settings
from lumi.path import PROJECT_ROOT

arg = sys.argv[1]
root = Path(arg) if arg else Path(settings.storage.hdf5_recorder.database_path)
if not root.is_absolute():
    root = PROJECT_ROOT / root
print(root)
PY
)"
if [[ -z "$RESOLVED_ROOT" ]]; then
    fail "could not resolve the HDF5 output directory"
elif [[ ! -d "$RESOLVED_ROOT" ]]; then
    fail "HDF5 root does not exist: $RESOLVED_ROOT"
    echo "        The tracked 'database' symlink points at the lab share; mount it," >&2
    echo "        or pass --root <dir>. Recordings are lost silently without it." >&2
elif [[ ! -w "$RESOLVED_ROOT" ]]; then
    fail "HDF5 root is not writable: $RESOLVED_ROOT"
else
    ok "HDF5 root $RESOLVED_ROOT"
fi

# ---- detection ---------------------------------------------------------------
if [[ $WITH_DETECTION -eq 1 ]]; then
    DETECTION_OK=1
    for module in torch mmdet rhana; do
        if ! "$PYTHON" -c "import $module" 2>/dev/null; then
            fail "detection: '$module' is not importable -- scripts/install_detection_deps.sh"
            DETECTION_OK=0
            break
        fi
    done
    if [[ $DETECTION_OK -eq 1 ]]; then
        # The model weights are absolute paths in settings.toml and belong to whoever
        # trained them; they are routinely absent on a fresh host.
        MISSING_MODELS="$("$PYTHON" - <<'PY' 2>/dev/null || echo "error"
from pathlib import Path
from lumi.config import settings
d = settings.detection.detector
keys = (
    "detector_model_path", "detector_model_config_path", "classifier_model_path",
    "classifier_label_mapper_path", "classifier_transforms_path",
)
print(",".join(k for k in keys if not Path(str(d[k])).exists()))
PY
)"
        if [[ -n "$MISSING_MODELS" && "$MISSING_MODELS" != "error" ]]; then
            fail "detection: model files not found ($MISSING_MODELS)"
            echo "        Fix the paths under [detection.detector] in cfg/settings.toml," >&2
            echo "        or start without it: --no-detection" >&2
        else
            ok "detection deps and model files present"
        fi
    fi
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

# The api node takes no --host for the bus: it reads LUMI_AMQP_URL first, and falls
# back to rabbitmq.host in settings. Set it explicitly so this script's --host/--user
# govern the bridge exactly as they govern every other node.
export LUMI_AMQP_URL="amqp://$BROKER_USER:$BROKER_PASS@$RABBITMQ_HOST:5672/"
if [[ -n "$API_WORKERS" ]]; then
    export DYNACONF_API__WORKERS="$API_WORKERS"
fi

STORAGE_ARGS=(--host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS")
if [[ -n "$STORAGE_ROOT" ]]; then
    STORAGE_ARGS+=(--root "$STORAGE_ROOT")
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

# Presence registry first: it should be listening before anything heartbeats.
if [[ $WITH_MONITOR -eq 1 ]]; then
    start_node monitor --host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS"
fi

# Storage next: it declares the exchanges the instrument host's producers publish
# into, so nothing on the other machine races a missing exchange.
start_node storage "${STORAGE_ARGS[@]}"
sleep 2

if [[ $WITH_DETECTION -eq 1 ]]; then
    start_node detection --host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS"
fi

# The bridge last, so the browser only reaches a stack whose consumers are up.
sleep 2
start_node api

echo
echo "nodes running (Ctrl-C to stop):"
for i in "${!NAMES[@]}"; do
    printf "  %-10s pid %s\n" "${NAMES[$i]}" "${PIDS[$i]}"
done
[[ $WITH_MONITOR   -eq 0 ]] && echo "  monitor    skipped (--no-monitor: the UI's presence panel stays empty)"
[[ $WITH_DETECTION -eq 0 ]] && echo "  detection  skipped (--no-detection)"
echo
API_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "API on http://${API_IP:-<this host>}:8000 -- contract $CONTRACT_HASH"
echo "logs in $LOG_DIR"
echo
echo "now start the instrument host (pascal + rheed) against this broker:"
echo "  scripts/start_instrument_host.sh --host <this machine's IP> --user <node user> --password <pw> \\"
echo "      --log <chamber log folder> --mi <MI mode folder>"

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
