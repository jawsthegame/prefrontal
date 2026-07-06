#!/bin/bash
#
# Coaching tick: run every enabled module, apply channel choice + suppression,
# and deliver whatever fired over ntfy/Pushover/TTS. Also runs the proactive
# overwhelm (panic) check on the same tick.
#
# This is the native replacement for the per-module n8n poll workflows
# (coach-check, hyperfocus-check, departure-reminder, panic-check): one command
# fans over hyperfocus, outing/arrival (location_anchor), departure
# (time_blindness), self-care, task-paralysis, impulsivity, and trip-tracking,
# so a single launchd job covers what those four workflows did. Scheduled by
# deploy/com.prefrontal-coach.plist (every 60s).
#
# `--deliver` publishes; without it the tick only prints (safe to run by hand to
# see what would fire). Edit PREFRONTAL_HOME / PREFRONTAL_USER to taste; the
# venv's `prefrontal` is used directly (no activation needed).
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"
# Handle to coach. Leave empty to let the CLI auto-pick when there's one user.
PREFRONTAL_USER="${PREFRONTAL_USER:-}"

cd "$PREFRONTAL_HOME"

args=(coach --deliver)
if [ -n "$PREFRONTAL_USER" ]; then
    args+=(--user "$PREFRONTAL_USER")
fi

exec "$PREFRONTAL_BIN" "${args[@]}"
