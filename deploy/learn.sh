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
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"

cd "$PREFRONTAL_HOME"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(ts)] learn: recomputing patterns"
"$PREFRONTAL_BIN" learn

# summarize falls back to the structured profile when Ollama is down, so it
# still writes profile.md and exits 0; the `|| true` guards the rare hard
# failure (e.g. --no-fallback) without aborting the whole pass.
echo "[$(ts)] summarize: regenerating profile.md"
"$PREFRONTAL_BIN" summarize || echo "[$(ts)] summarize: failed (non-fatal)"

echo "[$(ts)] learning pass complete"
