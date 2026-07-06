#!/usr/bin/env bash
# Fetch + triage every user's mail (the no-n8n ingestion path).
#
# Driven by the com.prefrontal-mail launchd agent. Runs `prefrontal mail fetch
# --all-users`: one job fans over every active user, fetching each user's own
# accounts from the per-user `sources` registry (`prefrontal mail add-source`).
# A user with no connected source is skipped — the global MAIL_IMAP_* env is one
# mailbox and is NOT inherited across users (that would be a cross-account leak).
# Run from the repo root so `.env` (PREFRONTAL_SECRET_KEY, which opens the sealed
# credentials) is loaded by the CLI.
#
# Usage: deploy/mail-fetch.sh
set -u

# Resolve the repo root from this script's location, so the job works no matter
# what WorkingDirectory launchd uses.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFRONTAL="${REPO_ROOT}/.venv/bin/prefrontal"

cd "${REPO_ROOT}" || exit 1

echo "[$(date '+%Y-%m-%dT%H:%M:%S')] fetching mail for all users"
exec "${PREFRONTAL}" mail fetch --all-users
