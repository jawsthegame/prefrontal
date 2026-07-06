#!/bin/bash
#
# Install the Prefrontal launchd agents from the committed plist templates.
#
# The deploy/com.prefrontal*.plist files ship as templates with three
# placeholders instead of machine-specific paths, so the repo carries no
# personal username or install location:
#
#   __PREFRONTAL_HOME__   the repo root (auto-detected from this script)
#   __HOME__              your home directory ($HOME)
#   __PREFRONTAL_USER__   the handle to coach / resolve the household from
#
# This script fills them in and writes the result to ~/Library/LaunchAgents/,
# then (re)loads each agent. Run it after every `git pull` that touches the
# templates — it is idempotent (it unloads any existing copy before loading).
#
# Usage:
#   bash deploy/install-launchd.sh                 # install all agents
#   PREFRONTAL_USER=alex bash deploy/install-launchd.sh   # set the coach handle
#   bash deploy/install-launchd.sh com.prefrontal com.prefrontal-coach  # subset
#
# Leave PREFRONTAL_USER unset to let the CLI auto-pick when there's one user.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
HOME_DIR="$HOME"
USER_HANDLE="${PREFRONTAL_USER:-}"

AGENTS_DIR="$HOME_DIR/Library/LaunchAgents"
LOGS_DIR="$HOME_DIR/Library/Logs"
mkdir -p "$AGENTS_DIR" "$LOGS_DIR"

# Which agents to install — default to every template in deploy/.
if [ "$#" -gt 0 ]; then
    labels=("$@")
else
    labels=()
    for f in "$REPO_ROOT"/deploy/com.prefrontal*.plist; do
        labels+=("$(basename "$f" .plist)")
    done
fi

for label in "${labels[@]}"; do
    src="$REPO_ROOT/deploy/${label}.plist"
    dst="$AGENTS_DIR/${label}.plist"
    if [ ! -f "$src" ]; then
        echo "skip: no template for $label ($src)" >&2
        continue
    fi

    # Fill placeholders. Use a bar delimiter so paths with slashes are fine.
    sed -e "s|__PREFRONTAL_HOME__|$REPO_ROOT|g" \
        -e "s|__HOME__|$HOME_DIR|g" \
        -e "s|__PREFRONTAL_USER__|$USER_HANDLE|g" \
        "$src" > "$dst"

    # Reload: unload any existing copy first so a changed template takes effect.
    launchctl unload "$dst" 2>/dev/null || true
    launchctl load -w "$dst"
    echo "installed: $dst"
done

echo "done. Filled __PREFRONTAL_HOME__=$REPO_ROOT __HOME__=$HOME_DIR __PREFRONTAL_USER__=${USER_HANDLE:-<auto>}"
