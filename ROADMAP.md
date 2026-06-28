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
- **LLM-backed summarizer** ✅ — `summarize_profile()` feeds the structured
  profile to a local Ollama model (`prefrontal/integrations/ollama.py`) and
  returns prioritized coaching prose, falling back to the heuristic when the
  model is down. Run via `prefrontal summarize`. *(Next: optional Anthropic
  provider for higher-quality summaries; cache/serve the narrative from
  `GET /profile`.)*

## Known stubs in the current code

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
- **Optional Anthropic provider** — keep inference local by default, but add an
  opt-in Anthropic API path (e.g. Claude Haiku for cheap, high-quality
  summaries; a larger model for heavier coaching/triage reasoning), selectable
  per agent as the README describes. The summarizer already takes an injected
  client, so this slots in behind the same interface. Local-first stays the
  default; the cloud path is explicit and configurable.

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
