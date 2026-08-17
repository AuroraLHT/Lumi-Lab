#!/usr/bin/env bash
#
# Serve the experiment MCP server over streamable HTTP, with a token you can actually
# use. This is "terminal 2" of the --http flow in scripts/demo_mcp.py: it makes sure an
# operator account exists, logs in for a JWT, writes it where the demo can find it, and
# then runs the server in the foreground.
#
# Usage:
#   scripts/start_mcp_http.sh [--port N] [--user NAME] [--password PW] [--host HOST]
#                             [--api URL] [--db PATH] [--public-url URL] [--token-only]
#
#   --token-only   mint the token and exit, without starting the server
#   --db PATH      override the user store. Note the login in step 2 goes to the *api
#                  node's* store, so overriding this to something the stack is not
#                  using creates the account in one database and authenticates against
#                  another -- normally you want the stack's own, which is the default.
#   --public-url   the address clients reach the server on, when that is not
#                  http://127.0.0.1:$PORT -- i.e. the https:// address of a TLS proxy
#                  in front. It becomes the OAuth issuer and every redirect target.
#
# A REAL MCP CLIENT DOES NOT NEED THE TOKEN THIS SCRIPT PRINTS. The server is its own
# OAuth authorization server (lumi.mcp.oauth), so registering it with no credentials --
#
#     claude mcp add --transport http lumi-experiment http://127.0.0.1:8100/mcp
#
# -- opens a browser login the first time it connects, and renews itself afterwards.
# The token below is for scripts/demo_mcp.py, which cannot walk a browser flow, and for
# anything else driving the transport by hand. The two paths end at the same JWT.
#
# The HTTP transport always demands an operator-or-admin bearer token, whatever
# settings.auth.enabled says -- the whole point of it is letting a remote agent reach
# real equipment control. Three things have to line up for the *token* path, and each
# fails differently (the login flow above sidesteps 2 and 3: it checks the password
# itself, against the same store, so it is never handed an anonymous identity):
#
#   1. The account has to live in the store the *server* reads. start_simulation.sh
#      points the stack at run/simulation/users.db while `manage` defaults to
#      cfg/users.db, so this sources run/simulation/env.sh -- the stack's own record of
#      what it exported -- rather than choosing a path of its own. --db overrides.
#   2. The stack has to be running --with-auth. With auth off, POST /auth/login skips
#      the password check and hands back the *anonymous* identity, whose row is
#      deliberately seeded inactive; lumi.mcp.auth rejects inactive users, so that
#      token 401s here even though the browser accepts it. Checked for below.
#   3. --with-auth's admin/simulation bootstrap only fires on an *empty* database
#      (bootstrap_default_user returns early once count_users() > 0, and the anonymous
#      row counts). Once the stack has run wide-open even once, that account is never
#      created -- which is why this script makes its own instead of relying on it.
#
# Plain HTTP, no TLS: put a reverse proxy in front for that (see lumi.mcp.__main__).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PORT=8100
USERNAME=agent
PASSWORD=agent-sim
API_URL=http://localhost:8000
TOKEN_ONLY=0
# Empty means "whatever the stack chose"; a flag fills these in as an override.
BROKER_HOST=
DB_PATH=
PUBLIC_URL=

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --user) USERNAME="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --host) BROKER_HOST="$2"; shift 2 ;;
        --api) API_URL="$2"; shift 2 ;;
        --db) DB_PATH="$2"; shift 2 ;;
        --public-url) PUBLIC_URL="$2"; shift 2 ;;
        --token-only) TOKEN_ONLY=1; shift ;;
        -h|--help) sed -n '3,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

PYTHON="uv run --frozen python"

# Adopt the running stack's environment instead of picking one here. start_simulation.sh
# writes run/simulation/env.sh with the settings it exported into its own nodes -- which
# user database, which broker -- and those exports reach only its children, not this
# terminal. Sourcing it is what keeps the account created below in the same store the
# server reads; deciding it independently is how the two silently drift apart.
ENV_FILE="run/simulation/env.sh"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    echo "adopted the stack's environment from $ENV_FILE"
else
    echo "note: no $ENV_FILE -- is the stack running? falling back to settings.toml" >&2
fi

# Explicit flags win over the stack's choices.
[[ -n "$DB_PATH" ]] && export DYNACONF_AUTH__DATABASE_PATH="$DB_PATH"
DB_PATH="${DYNACONF_AUTH__DATABASE_PATH:-cfg/users.db}"
BROKER_HOST="${BROKER_HOST:-${LUMI_SIM_BROKER_HOST:-localhost}}"

RUN_DIR="${LUMI_SIM_RUN_DIR:-run/simulation}"
mkdir -p "$RUN_DIR"
TOKEN_FILE="$RUN_DIR/mcp_token.txt"

# Fail here rather than with a connection refused three steps later.
if ! curl -sf -o /dev/null "$API_URL/docs" 2>/dev/null && ! curl -s -o /dev/null "$API_URL"; then
    echo "error: no API at $API_URL -- start the stack first:" >&2
    echo "  scripts/start_simulation.sh --chamber-speed 30 --with-experiment --with-auth" >&2
    exit 1
fi

# Idempotent: create-user exits 1 with "already exists" on a rerun, which is fine.
if $PYTHON -m lumi.api.manage create-user "$USERNAME" --role operator --password "$PASSWORD" 2>/dev/null; then
    echo "created operator '$USERNAME' in $DB_PATH"
else
    echo "operator '$USERNAME' already exists in $DB_PATH (reusing it)"
fi

echo "logging in at $API_URL/auth/login ..."
RESPONSE="$(curl -s -X POST "$API_URL/auth/login" \
    --data-urlencode "username=$USERNAME" --data-urlencode "password=$PASSWORD")"

TOKEN="$($PYTHON - "$RESPONSE" <<'PY'
import json, sys
try:
    print(json.loads(sys.argv[1])["access_token"])
except Exception:
    pass
PY
)"

if [[ -z "$TOKEN" ]]; then
    echo "error: login failed: $RESPONSE" >&2
    echo "  If this says 'Incorrect username or password', the account exists with a" >&2
    echo "  different password -- pass --password, or reset it with:" >&2
    echo "    DYNACONF_AUTH__DATABASE_PATH=$DB_PATH \\" >&2
    echo "      uv run python -m lumi.api.manage set-password $USERNAME" >&2
    exit 1
fi

# Trap 2: an anonymous token looks like a success and 401s at the server.
CLAIMED_USER="$($PYTHON - "$TOKEN" <<'PY'
import sys, jwt
print(jwt.decode(sys.argv[1], options={"verify_signature": False}).get("username", ""))
PY
)"
if [[ "$CLAIMED_USER" == "anonymous" ]]; then
    echo "error: /auth/login returned the anonymous identity, not '$USERNAME'." >&2
    echo "  The stack is running with auth OFF, so login does not check credentials." >&2
    echo "  That user row is inactive by design and lumi.mcp.auth will reject it (401)." >&2
    echo "  Restart the stack with --with-auth and run this again." >&2
    exit 1
fi

umask 077
printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
echo "token for '$CLAIMED_USER' written to $TOKEN_FILE"
echo
echo "drive it from another terminal with:"
echo "  uv run scripts/demo_mcp.py --http http://127.0.0.1:$PORT/mcp --token \"\$(cat $TOKEN_FILE)\""
echo
echo "or register it with a real client and let it log in as '$USERNAME' -- no token:"
echo "  claude mcp add --transport http lumi-experiment http://127.0.0.1:$PORT/mcp"
echo

if [[ $TOKEN_ONLY -eq 1 ]]; then
    exit 0
fi

PUBLIC_URL="${PUBLIC_URL:-http://127.0.0.1:$PORT}"
echo "serving MCP over $PUBLIC_URL/mcp (Ctrl-C to stop); sign-in page at $PUBLIC_URL/login"
exec $PYTHON -m lumi.mcp --transport http --port "$PORT" --host "$BROKER_HOST" \
    --public-url "$PUBLIC_URL"
