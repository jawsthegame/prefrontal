#!/bin/bash
#
# Weekly feature-usage nudge: the "act on it" half of the usage loop. When a
# coaching module keeps firing but you rarely act on it, this sends one push
# offering a one-tap Mute / Keep (see prefrontal/usage.py, /stats "Feature
# usage").
#
# Native (launchd) twin of the POST /webhooks/usage/check endpoint. The check
# self-gates to at most one nudge per ISO week per user, so running it on a
# coarse interval just means "check often" — it only sends when a week is due
# and there's actually a firing-but-ignored module. Scheduled by
# deploy/com.prefrontal-usage.plist (hourly).
#
# `--deliver` publishes; drop it to only print what would fire (safe by hand).
# `--all-users` fans over every active user (on a single-user box, just the one).
set -uo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME"

exec "$PREFRONTAL_BIN" usage check --deliver --all-users
