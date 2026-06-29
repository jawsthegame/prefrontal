# Roadmap

Prefrontal is in early development. This file tracks what's intentionally left as
a stub and what's planned next, so the gap between "scaffolded" and "finished"
stays visible to contributors. (See also `prefrontal modules -v` for per-module
intervention status.)

## Recently shipped

- **Pattern-computation pass** ✅ — `prefrontal/memory/patterns.py` derives
  `time_estimation`, `channel_response`, and `drift` patterns from `episodes`
  (confidence = `n/(n+k)`) and recomputes the `time_estimation_bias` multiplier.
  Run via `prefrontal learn`. *(Next: schedule it periodically; add finer
  `context_key` bucketing than episode type; derive `context_switch` once switch
  events are captured.)*

## Known stubs in the current code

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
- **Feed outings into learning** ✅ — outing returns log `task` episodes that the
  pattern pass now folds into `time_estimation` and the bias multiplier. (Next:
  give outings their own `context_key` so coffee runs calibrate separately from
  other tasks.)

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
