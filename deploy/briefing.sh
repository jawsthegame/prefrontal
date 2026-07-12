#!/bin/bash
#
# Morning briefing: publish today's digest (commitments, double-bookings, what
# slipped, your time bias) as a push through each user's own delivery route.
#
# Native (launchd) twin of the morning-briefing n8n workflow — same
# DeliveryClient + per-user resolve_route the coaching tick uses, so a user with
# no ntfy/Pushover target of their own is skipped, never delivered to the
# operator's device. Scheduled by deploy/com.prefrontal-briefing.plist (daily).
#
# `--deliver` publishes; drop it (run `prefrontal briefing`) to only print what
# would go out. `--all-users` fans over every active user (on a single-user box,
# just the one). Add `--llm` here for Ollama prose (it falls back to the
# structured briefing if Ollama is down).
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME"

exec "$PREFRONTAL_BIN" briefing --deliver --all-users
