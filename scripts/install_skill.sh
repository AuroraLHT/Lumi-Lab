#!/usr/bin/env bash
#
# Install the lab skill (skills/lumi-lab) for an agent, next to its `lumi` MCP
# registration. The skill is symlinked, not copied, so a `git pull` here updates every
# agent that uses it.
#
# Usage:
#   scripts/install_skill.sh [PROJECT_DIR]   the agent's project: PROJECT_DIR/.claude/skills
#   scripts/install_skill.sh --user          every project of this user: ~/.claude/skills
#
# With no argument, installs into the current directory's project.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills/lumi-lab"

case "${1:-}" in
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --user)    DEST_DIR="$HOME/.claude/skills" ;;
    "")        DEST_DIR="$PWD/.claude/skills" ;;
    *)         DEST_DIR="$(cd "$1" && pwd)/.claude/skills" ;;
esac

DEST="$DEST_DIR/lumi-lab"
mkdir -p "$DEST_DIR"

if [[ -L "$DEST" ]]; then
    ln -sfn "$SRC" "$DEST"
elif [[ -e "$DEST" ]]; then
    echo "$DEST exists and is not a link; move it away first" >&2
    exit 1
else
    ln -s "$SRC" "$DEST"
fi

echo "lumi-lab skill -> $DEST"
