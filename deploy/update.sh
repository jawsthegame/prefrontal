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

PREFRONTAL_HOME="${PREFRONTAL_HOME:-/Users/tom/prefrontal}"
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

echo "[$(ts)] update: complete"
