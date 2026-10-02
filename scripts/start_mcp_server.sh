#!/usr/bin/env bash
#
# Serve the experiment MCP server over streamable HTTP for the REAL chamber, on the
# server host, reachable from the network. Run it alongside scripts/start_server_host.sh
# (the experiment node it drives is started there).
#
# Usage:
#   scripts/start_mcp_server.sh [--bind-host ADDR] [--port N] [--public-url URL]
#                               [--host HOST] [--user U] [--password P] [--check]
#
#   --bind-host   address to listen on (default 0.0.0.0 -- every interface)
#   --port        port (default 8100)
#   --public-url  the address clients reach this server on (default
#                 https://<the certificate's name, e.g. the Tailscale one>:<port>). It becomes the OAuth issuer
#                 and every login redirect, so it must match what clients actually type,
#                 and the certificate must cover its host. Must be https:// unless it is
#                 loopback -- the MCP SDK rejects a plain-HTTP issuer anywhere else
#   --host        broker host (default localhost -- the broker runs on the server host)
#   --user/-p     broker credentials (default guest/guest)
#   --check       run the preflight checks and exit without starting anything
#
# Clients sign in through the browser with an existing operator or admin account:
#
#     claude mcp add --transport http lumi <public-url>/mcp
#
# HTTPS comes from [tls] in settings, the same certificate the API serves. With
# scripts/tailscale_cert.sh it is publicly trusted and clients need nothing but the
# tailnet; with scripts/make_lab_cert.sh they must trust its CA by hand.
#
# Unlike scripts/start_mcp_demo.sh (simulation/demo only) this creates no account and
# writes no token. It refuses to start with the placeholder signing key, with no
# operator/admin account to log in as, or with a certificate that does not cover the
# public URL.

set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    echo "error: do not source this script -- run it directly: scripts/start_mcp_server.sh" >&2
    return 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

BIND_HOST="0.0.0.0"
PORT=8100
PUBLIC_URL=""
RABBITMQ_HOST="localhost"
BROKER_USER="guest"
BROKER_PASS="guest"
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bind-host) BIND_HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --public-url) PUBLIC_URL="$2"; shift 2 ;;
        --host) RABBITMQ_HOST="$2"; shift 2 ;;
        --user) BROKER_USER="$2"; shift 2 ;;
        --password|-p) BROKER_PASS="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '3,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

PYTHON="$PROJECT_ROOT/.venv/bin/python"

fail() { echo "  FAIL  $*" >&2; FAILED=1; }
ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*" >&2; }

FAILED=0
echo "preflight (MCP server):"

if [[ ! -x "$PYTHON" ]]; then
    echo "  FAIL  no virtualenv at .venv -- uv sync --extra api" >&2
    exit 1
fi
ok "venv at .venv ($("$PYTHON" -V 2>&1))"

for module in mcp uvicorn aiosqlite; do
    if "$PYTHON" -c "import $module" 2>/dev/null; then
        ok "$module importable"
    else
        fail "$module missing -- uv sync --extra api"
    fi
done

# ---- address -----------------------------------------------------------------
# The public URL is published as the OAuth issuer; a client that reached the server
# under any other spelling rejects the login. 0.0.0.0 is never a valid one.
TLS_REPORT="$("$PYTHON" -m lumi.tls 2>&1 || echo "fail|could not run the TLS check")"
SCHEME="https"
[[ "$TLS_REPORT" == off\|* ]] && SCHEME="http"
if [[ -z "$PUBLIC_URL" ]]; then
    case "$BIND_HOST" in
        # The certificate's own name (a Tailscale cert is valid for nothing else), else
        # this host's LAN address.
        0.0.0.0|::) HOST_IP="$("$PYTHON" -m lumi.tls --default-host 2>/dev/null || true)"
                    HOST_IP="${HOST_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
                    PUBLIC_URL="$SCHEME://${HOST_IP:-127.0.0.1}:$PORT" ;;
        *)          PUBLIC_URL="$SCHEME://$BIND_HOST:$PORT" ;;
    esac
    warn "no --public-url; advertising $PUBLIC_URL -- clients must use exactly this address"
else
    ok "public URL $PUBLIC_URL"
fi
case "$PUBLIC_URL" in
    *://0.0.0.0*|*://\[::\]*) fail "--public-url $PUBLIC_URL is a bind address, not one clients can reach" ;;
    # The MCP SDK refuses an OAuth issuer that is plain HTTP anywhere but loopback
    # (RFC 8414), raising "Issuer URL must be HTTPS" at startup -- say so here instead.
    https://*|http://localhost[:/]*|http://localhost|http://127.0.0.1[:/]*|http://127.0.0.1|http://\[::1\]*) ;;
    *) fail "--public-url $PUBLIC_URL is plain HTTP off loopback; the OAuth login requires https."
       echo "        scripts/tailscale_cert.sh --install-cron, then [tls] in cfg/.secrets.toml" >&2
       echo "        (or bind --bind-host 127.0.0.1 and have clients reach it over an SSH tunnel)." >&2 ;;
esac

# ---- TLS ---------------------------------------------------------------------
# This process serves the certificate itself, so it must cover the public URL's host.
# TLS off with an https:// public URL means a proxy in front terminates it instead.
PUBLIC_HOST="$(sed -E 's#^[a-z]+://##; s#/.*$##; s#:[0-9]+$##' <<< "$PUBLIC_URL")"
while IFS='|' read -r level message; do
    case "$level" in
        ok)   ok "$message" ;;
        warn) warn "$message" ;;
        fail) fail "$message" ;;
        off)  [[ "$PUBLIC_URL" == https://* ]] && warn "$message -- assuming a TLS proxy serves $PUBLIC_URL" ;;
        *)    fail "TLS check: $level $message" ;;
    esac
done < <("$PYTHON" -m lumi.tls "$PUBLIC_HOST" 2>&1 || echo "fail|could not run the TLS check")

# ---- broker ------------------------------------------------------------------
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$RABBITMQ_HOST/5672" 2>/dev/null; then
    ok "broker reachable at $RABBITMQ_HOST:5672"
else
    fail "broker unreachable at $RABBITMQ_HOST:5672 -- start the server host first"
fi
case "$RABBITMQ_HOST" in
    localhost|127.0.0.1|::1) ;;
    *) [[ "$BROKER_USER" == "guest" ]] && \
           fail "broker is remote but user is 'guest' -- RabbitMQ refuses guest off loopback" ;;
esac

# ---- auth --------------------------------------------------------------------
# The MCP server verifies the same JWTs the api node signs, so a placeholder key
# makes every token forgeable. And with OAuth login the only way in is an existing
# active operator/admin account -- check there is one.
AUTH_REPORT="$("$PYTHON" - <<'PY' 2>/dev/null || echo "error"
import sqlite3
from lumi.config import settings
from lumi.api.security import DEV_SECRET_KEY
from lumi.api.db import get_database_path

placeholder = settings.get("auth.secret_key", DEV_SECRET_KEY) == DEV_SECRET_KEY
path = get_database_path()
count = "missing"
if path.exists():
    try:
        count = sqlite3.connect(path).execute(
            "select count(*) from users where is_active = 1 and role in ('operator', 'admin')"
        ).fetchone()[0]
    except sqlite3.Error:
        count = "unreadable"
print(f"{placeholder}|{path}|{count}")
PY
)"
if [[ "$AUTH_REPORT" == "error" ]]; then
    fail "could not read the auth settings"
else
    IFS='|' read -r AUTH_PLACEHOLDER AUTH_DB AUTH_COUNT <<< "$AUTH_REPORT"
    if [[ "$AUTH_PLACEHOLDER" == "True" ]]; then
        fail "auth.secret_key is still the shipped placeholder -- tokens are forgeable."
        echo "        $PYTHON -m lumi.api.manage gen-secret   # then put it in cfg/.secrets.toml" >&2
    else
        ok "real signing key"
    fi
    case "$AUTH_COUNT" in
        missing|unreadable|0)
            fail "no active operator/admin account in $AUTH_DB ($AUTH_COUNT) -- nobody can log in."
            echo "        $PYTHON -m lumi.api.manage create-user <name> --role operator" >&2 ;;
        *)  ok "$AUTH_COUNT operator/admin account(s) in $AUTH_DB" ;;
    esac
fi

if [[ $FAILED -ne 0 ]]; then
    echo >&2
    echo "preflight failed -- nothing started." >&2
    exit 1
fi
echo "preflight passed."
[[ $CHECK_ONLY -eq 1 ]] && exit 0

echo
echo "serving MCP on $BIND_HOST:$PORT, advertised as $PUBLIC_URL/mcp (Ctrl-C to stop)"
echo "register a client with:"
echo "  claude mcp add --transport http lumi $PUBLIC_URL/mcp"
[[ "$SCHEME" == "https" && "$PUBLIC_URL" != *.ts.net* ]] && \
    echo "  (lab CA certificate: clients need NODE_EXTRA_CA_CERTS=/path/to/cfg/tls/ca.crt)"
echo
exec "$PYTHON" -m lumi.mcp --transport http \
    --bind-host "$BIND_HOST" --port "$PORT" --public-url "$PUBLIC_URL" \
    --host "$RABBITMQ_HOST" --user "$BROKER_USER" --password "$BROKER_PASS"
