#!/bin/bash
#
# Full deploy: pull the latest code, reinstall deps, and apply the (idempotent)
# schema so the DB matches the new code. This is the "update" half of remote
# self-update — the *restart* is issued separately (launchd kickstart) by
# `prefrontal update` / `prefrontal restart` or POST /admin/update, once this
# script exits 0. A non-zero exit here means the restart is skipped, so a broken
# pull never bounces a working service.
#
# Invoked by prefrontal.selfupdate (the default update_command) and safe to run
# by hand. Edit PREFRONTAL_HOME to your install location (the repo root, so the
# adjacent .env is picked up). The venv's tools are used directly.
set -euo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_BIN="${PREFRONTAL_BIN:-$PREFRONTAL_HOME/.venv/bin/prefrontal}"
PREFRONTAL_PIP="${PREFRONTAL_PIP:-$PREFRONTAL_HOME/.venv/bin/pip}"

cd "$PREFRONTAL_HOME"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(ts)] update: git pull --ff-only"
git pull --ff-only

echo "[$(ts)] update: pip install -e ."
"$PREFRONTAL_PIP" install -e . --quiet

# init-db is idempotent: it CREATE-TABLE-IF-NOT-EXISTS and back-fills any new
# columns, so on an existing DB it just applies pending schema changes. The
# service also applies the schema on startup, so this is belt-and-suspenders.
echo "[$(ts)] update: apply schema (init-db)"
"$PREFRONTAL_BIN" init-db

# Push the latest workflow templates into the running n8n so a repo change
# becomes live workflows without a manual editor import. Best-effort by design:
# a no-op unless N8N_API_URL + N8N_API_KEY are set, and never fatal — a down or
# unconfigured n8n must not block the restart of an otherwise-good update (the
# same "a down n8n never breaks a path" stance as the outbound client). See
# docs/n8n-sync.md.
echo "[$(ts)] update: sync n8n workflows"
"$PREFRONTAL_BIN" n8n push || echo "[$(ts)] update: n8n sync skipped/failed (non-fatal)"

# Re-package the built artifacts (Python wheel, client handout PDFs, macOS
# desktop app) from the freshly-pulled code. Best-effort and non-fatal by
# design — each step self-skips when its toolchain/source is absent, and a
# packaging failure must never block the restart of an otherwise-good update
# (same stance as the n8n sync above). See deploy/package.sh.
echo "[$(ts)] update: re-package artifacts"
PREFRONTAL_HOME="$PREFRONTAL_HOME" bash "$PREFRONTAL_HOME/deploy/package.sh" \
    || echo "[$(ts)] update: packaging skipped/failed (non-fatal)"

echo "[$(ts)] update: complete"
