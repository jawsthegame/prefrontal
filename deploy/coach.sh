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
# see what would fire). Edit PREFRONTAL_HOME to taste; the venv's `prefrontal` is
# used directly (no activation needed).
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME"

# Arm any live calendar focus block first (zero-tap start): idempotent, so it's
# safe every tick and never stacks sessions. This is the native replacement for
# the focus-arm-check n8n workflow; the coaching tick below then delivers that
# session's interrupts. A failure here must not stop the tick, so it's non-fatal.
"$PREFRONTAL_BIN" focus arm --all-users || true

# --all-users: one job coaches every active user (on a single-user box that's
# just the one user). Delivery is per-user: resolve_route only sends to a user's
# OWN ntfy/Pushover target, so a user without one is computed but never delivered
# to the operator's device (no cross-account leak on a multi-user box).
exec "$PREFRONTAL_BIN" coach --deliver --all-users
