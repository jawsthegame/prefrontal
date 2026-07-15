#!/bin/bash
#
# Nightly learning pass: recompute the behavioral profile from accumulated
# episodes, then regenerate the prose profile.md via Ollama.
#
# Run on a schedule by deploy/com.prefrontal-learn.plist, or by
# hand / from cron. launchd's ProgramArguments can't chain `learn && summarize`,
# so the two steps live here.
#
# Edit PREFRONTAL_HOME to your install location (the repo root, so the adjacent
# .env is picked up). The venv's `prefrontal` is used directly — no activation
# needed.
# NOTE: deliberately no `-e`. The two steps are independent, and a failure in
# `learn` must NOT abort the pass before `summarize` runs — otherwise a single
# bad recompute silently freezes the cached profile (its generated_at is what the
# dashboard shows as "updated"), which looks like the profile has simply stopped
# changing. We run both steps unconditionally, log each failure loudly, and exit
# non-zero at the end so a monitor/log scan can catch a broken pass.
set -uo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME" || { echo "[FATAL] cannot cd to $PREFRONTAL_HOME" >&2; exit 1; }

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

rc=0

echo "[$(ts)] learn: recomputing patterns"
if ! "$PREFRONTAL_BIN" learn; then
    echo "[$(ts)] learn: FAILED — patterns not recomputed this pass" >&2
    rc=1
fi

# summarize runs regardless of the learn result, so a learn failure never leaves
# the cached profile (and its dashboard "updated" time) frozen. It also falls
# back to the structured profile when Ollama is down, so a model outage still
# refreshes generated_at rather than looking like a stalled pipeline.
echo "[$(ts)] summarize: regenerating profile.md"
if ! "$PREFRONTAL_BIN" summarize; then
    echo "[$(ts)] summarize: FAILED — cached profile not refreshed" >&2
    rc=1
fi

if [ "$rc" -eq 0 ]; then
    echo "[$(ts)] learning pass complete"
else
    echo "[$(ts)] learning pass complete WITH ERRORS (see above)" >&2
fi
exit "$rc"
