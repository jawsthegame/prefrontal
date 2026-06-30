#!/usr/bin/env bash
# Fetch + triage mail for one or more accounts (the no-n8n ingestion path).
#
# Driven by the com.morningstatic.prefrontal-mail launchd agent, which passes
# the account names as arguments. Each account is fetched independently; a
# failure on one (e.g. a transient IMAP timeout) is logged and does not stop the
# others. Run from the repo root so `.env` (with MAIL_IMAP_*_<ACCOUNT> and
# PREFRONTAL_MAIL_ACCOUNTS) is loaded by the CLI.
#
# Usage: deploy/mail-fetch.sh <account> [<account> ...]

set -u

# Resolve the repo root from this script's location, so the job works no matter
# what WorkingDirectory launchd uses.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFRONTAL="${REPO_ROOT}/.venv/bin/prefrontal"

if [[ "$#" -eq 0 ]]; then
    echo "usage: mail-fetch.sh <account> [<account> ...]" >&2
    exit 2
fi

cd "${REPO_ROOT}" || exit 1

status=0
for account in "$@"; do
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] fetching mail for '${account}'"
    if ! "${PREFRONTAL}" mail fetch --account "${account}"; then
        echo "[$(date '+%Y-%m-%dT%H:%M:%S')] WARN: fetch failed for '${account}'" >&2
        status=1
    fi
done
exit "${status}"
