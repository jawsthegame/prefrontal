#!/bin/bash
#
# Household sweeps: the shared-household nudges that aren't part of the per-user
# coaching tick — chores due, the weekly mental-load check-in, the daily delta
# digest, and the ⭐ star-agreement prompts.
#
# Native replacement for the n8n household workflows (chores-check,
# checkin-check, digest-check, star-prompt-check). Each subcommand is the CLI
# twin of its /webhooks/household/*/check endpoint and self-gates on due-ness
# (off, wrong day/time, already sent, nothing new) — so it's safe to run this on
# a coarse interval and let each one decide whether to actually send. Scheduled
# by deploy/com.morningstatic.prefrontal-household.plist (every 15 min).
#
# A single-parent household is a clean no-op for the shared checks. Edit
# PREFRONTAL_HOME / PREFRONTAL_USER to taste; each check runs independently so
# one being not-due (or erroring) never blocks the others.
set -uo pipefail

PREFRONTAL_HOME="${PREFRONTAL_HOME:-/Users/tom/prefrontal}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"
# Any member of the household; the sweep resolves the household from this user.
PREFRONTAL_USER="${PREFRONTAL_USER:-}"

cd "$PREFRONTAL_HOME"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

run() {  # run() <household-action> — one sweep, never abort the rest on failure
    local action="$1"
    local args=(household "$action")
    if [ -n "$PREFRONTAL_USER" ]; then
        args+=(--user "$PREFRONTAL_USER")
    fi
    echo "[$(ts)] household $action"
    "$PREFRONTAL_BIN" "${args[@]}" || echo "[$(ts)] household $action: failed (non-fatal)"
}

run chores-check
run checkin-check
run digest-check
run prompt-check
