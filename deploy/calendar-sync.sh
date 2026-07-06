#!/bin/bash
#
# Calendar sync tick: fetch every user's private ICS feeds and upsert their
# events into commitments (recurrence-expanded, TZID->UTC, conflict-checked).
#
# This is the native, no-n8n replacement for the n8n `calendar-sync` workflow:
# instead of one workflow with hard-coded ICS URLs feeding a single user, each
# user's feeds live in the `sources` registry (`prefrontal calendar add-source`)
# and this one job fans over all of them with `--all-users`. Scheduled by
# deploy/com.prefrontal-calendar.plist (every 15 min).
#
# Before enabling it, DEACTIVATE the n8n `calendar-sync` workflow so events
# aren't ingested twice. Run this wrapper by hand once to confirm it syncs.
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME"

# --all-users: one job covers every active user's own feeds. Each user's events
# land only in their own scope (feed URLs + declined-filter live per user).
exec "$PREFRONTAL_BIN" calendar sync --all-users
