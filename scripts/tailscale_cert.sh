#!/usr/bin/env bash
#
# Fetch (or renew) the HTTPS certificate for this machine's Tailscale name, for the
# API and MCP servers ([tls] in settings). It is a real Let's Encrypt certificate, so
# browsers, phones and Claude Code trust it with nothing installed on the client --
# they only need to be on the tailnet and use the name, not an IP.
#
# Usage:
#   scripts/tailscale_cert.sh [--reload] [--install-cron] [--name NAME] [--out DIR]
#
#   --reload        after a renewal, SIGHUP the running API and MCP servers so they
#                   start serving the new certificate (no restart, no dropped /ws)
#   --install-cron  add a daily `--reload` run to this user's crontab (idempotent),
#                   then fetch now. This is the "never think about it again" switch
#   --name          the certificate name (default: this machine's MagicDNS name)
#   --out           where the files go (default cfg/tls, git-ignored)
#
# One-time setup, outside this repo:
#   1. Tailscale admin console -> DNS: MagicDNS on, "HTTPS Certificates" enabled
#   2. sudo tailscale set --operator=$USER    (lets this user fetch certificates)
#
# Then in cfg/.secrets.toml:
#
#   [tls]
#   enabled = true
#   certfile = "cfg/tls/tailscale.crt"
#   keyfile = "cfg/tls/tailscale.key"
#
# Certificates last 90 days; tailscaled renews its copy a month ahead, and the daily
# run copies the renewal out and signals the servers. Run it with nothing running and
# it just updates the files.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

RELOAD=0
INSTALL_CRON=0
NAME=""
OUT="cfg/tls"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --reload) RELOAD=1; shift ;;
        --install-cron) INSTALL_CRON=1; shift ;;
        --name) NAME="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        -h|--help) sed -n '3,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

stamp() { echo "$(date '+%F %T') $*"; }

command -v tailscale >/dev/null || { stamp "error: tailscale is not installed" >&2; exit 1; }

STATUS="$(tailscale status --self --json 2>/dev/null)" || {
    stamp "error: tailscale is not running (tailscale up)" >&2; exit 1; }
read -r SELF_NAME CERT_DOMAINS < <(python3 -c '
import json, sys
d = json.load(sys.stdin)
print((d.get("Self") or {}).get("DNSName", "").rstrip("."), ",".join(d.get("CertDomains") or []) or "-")
' <<< "$STATUS")
NAME="${NAME:-$SELF_NAME}"

if [[ -z "$NAME" ]]; then
    stamp "error: this machine has no MagicDNS name -- turn MagicDNS on in the admin console" >&2
    exit 1
fi
if [[ "$CERT_DOMAINS" == "-" ]]; then
    stamp "error: HTTPS certificates are not enabled for this tailnet." >&2
    echo "  Tailscale admin console -> DNS -> HTTPS Certificates -> Enable" >&2
    exit 1
fi

if [[ $INSTALL_CRON -eq 1 ]]; then
    LOG="$PROJECT_ROOT/run/production/logs/tailscale_cert.log"
    mkdir -p "$(dirname "$LOG")"
    LINE="17 4 * * * $PROJECT_ROOT/scripts/tailscale_cert.sh --reload >> $LOG 2>&1"
    if crontab -l 2>/dev/null | grep -Fq "scripts/tailscale_cert.sh"; then
        stamp "crontab already renews the certificate"
    else
        { crontab -l 2>/dev/null || true; echo "$LINE"; } | crontab -
        stamp "crontab: daily renewal at 04:17, log $LOG"
    fi
    RELOAD=1
fi

umask 077
mkdir -p "$OUT"
CRT="$OUT/tailscale.crt"
KEY="$OUT/tailscale.key"
TMP="$(mktemp -d "$OUT/.renew.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

if ! ERR="$(tailscale cert --cert-file "$TMP/cert" --key-file "$TMP/key" "$NAME" 2>&1)"; then
    stamp "error: tailscale cert failed: $ERR" >&2
    if grep -qi "access denied\|operator" <<< "$ERR"; then
        echo "  Let this user fetch certificates: sudo tailscale set --operator=$USER" >&2
    fi
    exit 1
fi

if [[ -f "$CRT" ]] && cmp -s "$TMP/cert" "$CRT" && cmp -s "$TMP/key" "$KEY"; then
    stamp "certificate for $NAME unchanged ($(openssl x509 -in "$CRT" -noout -enddate))"
    exit 0
fi

# Key first, then cert, each by rename: a server reading mid-update sees either the
# old pair or the new key with the old cert, which load_cert_chain rejects (and keeps
# serving the old one) rather than a half-written file.
mv "$TMP/key" "$KEY"
mv "$TMP/cert" "$CRT"
chmod 600 "$KEY"
chmod 644 "$CRT"
stamp "certificate for $NAME written to $CRT ($(openssl x509 -in "$CRT" -noout -enddate))"

if [[ $RELOAD -eq 1 ]]; then
    # The API supervisor (multi-worker: restarts its workers) or the single API
    # process, and the MCP HTTP server -- both reload the certificate on SIGHUP.
    for pattern in 'nodes/api\.py' 'lumi\.mcp .*--transport http'; do
        for pid in $(pgrep -f "$pattern" || true); do
            kill -HUP "$pid" && stamp "sent SIGHUP to $pid ($(ps -o args= -p "$pid" | cut -c1-60))"
        done
    done
fi
