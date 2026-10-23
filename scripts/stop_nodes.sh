#!/usr/bin/env bash
#
# Kill any lumi node processes (monitor, storage, pascal, rheed, detection, api,
# agent, experiment, simulation) still running, whether they belong to a live start_simulation.sh session or
# are orphans left behind by one that got killed without cleanup (e.g. it was
# `source`d into an interactive shell -- see the comment in start_simulation.sh
# about why that breaks its Ctrl-C handler).
#
# The MCP server (`python -m lumi.mcp`, from start_mcp_server.sh or
# start_mcp_demo.sh) is stopped too: it is not a node, but one left running keeps
# port 8100 and the next start_mcp_server.sh cannot bind it.
#
# Usage:
#   scripts/stop_nodes.sh [--force] [--dry-run] [--keep-mcp]
#
#   --dry-run   list the matching processes without killing anything
#   --force     skip the SIGTERM grace period and go straight to SIGKILL
#   --keep-mcp  leave the MCP server running (only the nodes are stopped)

set -euo pipefail

FORCE=0
DRY_RUN=0
KEEP_MCP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-mcp) KEEP_MCP=1; shift ;;
        -h|--help) sed -n '3,18p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

PATTERN='nodes/(monitor|storage|pascal|rheed|detection|api|agent|experiment|simulation)\.py'
# `-m lumi.mcp` followed by a space or the end, so lumi.mcp.<anything> run as a
# module (a test, a one-off) is not caught.
[[ $KEEP_MCP -eq 0 ]] && PATTERN="$PATTERN|-m lumi\.mcp( |\$)"

mapfile -t MATCHES < <(pgrep -af "$PATTERN" || true)

if [[ ${#MATCHES[@]} -eq 0 ]]; then
    echo "no lumi node processes running"
    exit 0
fi

echo "found ${#MATCHES[@]} lumi process(es):"
printf '  %s\n' "${MATCHES[@]}"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry run, nothing killed)"
    exit 0
fi

PIDS=()
for line in "${MATCHES[@]}"; do
    PIDS+=("${line%% *}")
done

if [[ $FORCE -eq 1 ]]; then
    echo "sending SIGKILL..."
    kill -KILL "${PIDS[@]}" 2>/dev/null || true
else
    echo "sending SIGTERM..."
    kill -TERM "${PIDS[@]}" 2>/dev/null || true

    for _ in $(seq 1 10); do
        sleep 0.5
        still_alive=0
        for pid in "${PIDS[@]}"; do
            kill -0 "$pid" 2>/dev/null && still_alive=1
        done
        [[ $still_alive -eq 0 ]] && break
    done

    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "pid $pid did not exit after SIGTERM, sending SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
fi

sleep 0.5
mapfile -t REMAINING < <(pgrep -af "$PATTERN" || true)
if [[ ${#REMAINING[@]} -eq 0 ]]; then
    echo "all node processes stopped"
else
    echo "still running (could not kill):" >&2
    printf '  %s\n' "${REMAINING[@]}" >&2
    exit 1
fi
