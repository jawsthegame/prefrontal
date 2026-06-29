# Roadmap

Prefrontal is in early development. This file tracks what's intentionally left as
a stub and what's planned next, so the gap between "scaffolded" and "finished"
stays visible to contributors. (See also `prefrontal modules -v` for per-module
intervention status.)

## Known stubs in the current code

- **Pattern-computation pass** — the job that turns accumulated `episodes` into
  calibrated `patterns` (variance, confidence, sample size) is not written yet.
  Until it exists, `profile.md` reflects the seeded coaching defaults plus any
  patterns written directly. This is the heart of "how it learns."
  *(`prefrontal/memory/` — new summarizer/aggregation step.)*
- **LLM-backed summarizer** — `build_profile()` is a deterministic, templated
  heuristic. A local-model (Ollama) version that synthesizes nuanced, prioritized
  guidance is planned, with the heuristic as a fallback.
  *(`prefrontal/memory/summarizer.py`.)*
- **n8n inbound handlers** — `POST /webhooks/n8n` classifies events via
  `parse_inbound_event()` but routes none of them to real handlers yet.
  *(`prefrontal/integrations/n8n.py`, `prefrontal/webhooks/app.py`.)*
- **Module interventions** — most declared interventions are `status="planned"`.
  Module 1 (Location-Aware Task Anchor) is the exception: its soft/firm/call
  escalation is wired end-to-end.
- **n8n departure-reminder template** — ships with a hardcoded calendar
  placeholder; swap in a real Google Calendar / CalDAV node.
  *(`deploy/n8n/departure-reminder.workflow.json`.)*

## Module 1 — Location-Aware Task Anchor: follow-ups

- **Location-gating the escalation** — only fire the 150% voice call if the user
  is still away from home (use `distance_m`, already computed in
  `/webhooks/outing/check`), so a quiet early return suppresses the call.
- **Abandoned auto-close** — auto-close outings that are never returned (e.g. far
  past the window with no `/return`) as `status="abandoned"` so they don't nudge
  forever; record them for pattern tracking.
- **Feed outings into learning** — once the pattern-computation pass exists,
  outing `miss` episodes should sharpen future time-window predictions
  (`time_estimation` patterns).

## Beyond v1 (from the README architecture)

- **Triage agent** — classify/prioritize/route inbound signals.
- **Coaching agent** — generate briefings, reminders, check-ins from the profile.
- **Delivery layer** — first-class Pushover / Ntfy / TTS integrations in Python
  (today delivery is handled in n8n).
- **Ingestion** — mail monitoring (Google Apps Script digest), calendar sync.
- **Morning briefing** — daily digest calibrated to coaching preferences.
- **Inference providers** — Ollama client (and optional Anthropic API) wired in
  Python for the hybrid architecture.

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
