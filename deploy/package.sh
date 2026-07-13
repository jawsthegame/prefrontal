#!/bin/bash
#
# Re-package the built artifacts after a pull: a Python wheel + sdist, the
# client handout PDFs, and the macOS desktop (Electron) app. Run this explicitly,
# out of band, whenever you actually want fresh artifacts:
#
#     bash deploy/package.sh
#
# It is intentionally NOT called by deploy/update.sh. The desktop build (npm +
# electron-builder + signing) takes minutes — long enough to blow the update's
# UPDATE_TIMEOUT_SECONDS (see prefrontal.selfupdate), which then *skips the
# restart* of an otherwise-good update, leaving the box serving old code after a
# "successful" pull. The dashboard server needs only pull + restart to pick up a
# code change; rebuilding artifacts is this separate, explicit step (or CI).
#
# BEST-EFFORT AND NON-FATAL by design: each step is skipped (with a log line)
# when its toolchain or source isn't present, and a failing step never aborts the
# run — the same "a down/absent dependency must not break the deploy" stance as
# the n8n sync in update.sh. So this is safe on a headless server (no npm → no
# desktop build; no Chrome → no PDFs) and on the Mac mini (builds everything).
#
# Outputs land in gitignored dirs (dist/, desktop/dist/) — NEVER overwriting a
# tracked file — so re-packaging keeps the working tree clean and the next
# update's `git pull --ff-only` can't choke on a dirty checkout. Override the
# toolchain paths via the env vars below.
set -uo pipefail

# Default to the repo root (this script lives in <repo>/deploy/), so no path is
# hard-coded; override PREFRONTAL_HOME to run against a different checkout.
PREFRONTAL_HOME="${PREFRONTAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
PREFRONTAL_PY="${PREFRONTAL_PY:-$PREFRONTAL_HOME/.venv/bin/python}"

cd "$PREFRONTAL_HOME"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] package: $*"; }

# 1) Python wheel + sdist. Needs the `build` module (pip install build); the
#    editable install stays the runtime, this just produces a distributable.
if "$PREFRONTAL_PY" -c "import build" >/dev/null 2>&1; then
    log "building wheel + sdist -> dist/"
    "$PREFRONTAL_PY" -m build --outdir dist . || log "wheel build failed (non-fatal)"
else
    log "skip wheel - python 'build' module not installed (pip install build)"
fi

# 2) Client handout PDFs, rendered from their HTML sources with headless Chrome
#    so the printable one-sheet / parent-pack stay current with the code. Written
#    to the gitignored dist/handouts/ (NOT over the tracked docs/*.pdf) so the
#    checkout stays clean for the next `git pull`.
CHROME="${PREFRONTAL_CHROME:-}"
if [ -z "$CHROME" ]; then
    for candidate in \
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        "/Applications/Chromium.app/Contents/MacOS/Chromium" \
        "$(command -v google-chrome 2>/dev/null || true)" \
        "$(command -v chromium 2>/dev/null || true)" \
        "$(command -v chromium-browser 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            CHROME="$candidate"
            break
        fi
    done
fi
if [ -n "$CHROME" ]; then
    mkdir -p dist/handouts
    for sheet in one-sheet parent-pack; do
        src="docs/$sheet.html"
        [ -f "$src" ] || continue
        log "rendering $src -> dist/handouts/$sheet.pdf"
        "$CHROME" --headless=new --disable-gpu --no-sandbox \
            --print-to-pdf="dist/handouts/$sheet.pdf" "$src" >/dev/null 2>&1 \
            || log "PDF render failed for $sheet (non-fatal)"
    done
else
    log "skip PDFs - no Chrome/Chromium found (set PREFRONTAL_CHROME)"
fi

# 3) macOS desktop app (Electron -> .dmg installers). Needs npm; a headless
#    server without it just skips. Heaviest step, so it runs last.
if [ -f "desktop/package.json" ] && command -v npm >/dev/null 2>&1; then
    log "building desktop app (electron-builder) -> desktop/dist/"
    ( cd desktop && { npm ci || npm install; } && npm run dist ) \
        || log "desktop build failed/skipped (non-fatal)"
else
    log "skip desktop - desktop/package.json or npm not present"
fi

# 4) Deploy artifacts: the n8n workflows are pushed by update.sh's `n8n push`,
#    and the iOS Shortcut is a static file (nothing to build), so there's
#    nothing to re-package here.

log "complete"
