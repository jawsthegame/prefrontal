# Roadmap

Prefrontal is in early development. This file tracks what's intentionally left as
a stub and what's planned next, so the gap between "scaffolded" and "finished"
stays visible to contributors. (See also `prefrontal modules -v` for per-module
intervention status.)

## ⭐ Priority: first real-world test (Module 1 live on the mini)

The code for an end-to-end Coffee Shop Nudge is **done and tested** — outing
endpoints, time escalation, location-gating, abandoned auto-close, passive
return, and the learning + summarizer passes. The only remaining work is
**operational** (on the Mac mini); see `docs/deployment.md` for the full runbook.
Ordered path to the first real nudge:

1. **Stand up Prefrontal** — clone, `pip install -e .`, set a strong
   `PREFRONTAL_WEBHOOK_SECRET` in `.env`, `prefrontal init-db`, load the launchd
   agent (`deploy/com.morningstatic.prefrontal.plist`). Confirm `GET /health`.
2. **Ollama** — `ollama pull qwen2.5:14b` (24GB mini), set `OLLAMA_MODEL`.
3. **n8n** — import `deploy/n8n/coffee-shop-nudge.workflow.json`; set the
   Prefrontal token, the Twilio Basic-Auth credential + `To`/`From`, and Pushover
   token/user.
4. **iOS Shortcuts** — build "Going out" / "I'm back" (`deploy/ios-shortcut.md`).
   *Optional but recommended:* feed `current_lat`/`current_lon` into the n8n
   `Check Outings` body (from an HA/iOS location source) to activate
   location-gating + passive return.
5. **Tailscale** — so the phone reaches the mini remotely.
6. **Dry run** — start an outing with a 1-minute window and confirm: push at
   ~30s (50%), push at ~1m (100%), Twilio call at ~90s (150%), and that
   `/return` (or coming home) logs the episode.
7. **Schedule learning** — nightly `prefrontal learn && prefrontal summarize`.

Everything above the dry run is configuration; no further code is required for
the first test. Code follow-ups below are optional polish.

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
  Module 1 (Location-Aware Task Anchor) is the exception: its escalation,
  location-gating, and auto-close interventions are wired end-to-end.
- **n8n departure-reminder template** — ships with a hardcoded calendar
  placeholder; swap in a real Google Calendar / CalDAV node.
  *(`deploy/n8n/departure-reminder.workflow.json`.)*

## Module 1 — Location-Aware Task Anchor: follow-ups

- **Location-gating the escalation** ✅ — when a location check places the user
  within the home radius (`home_radius_m`), `/webhooks/outing/check` suppresses
  the nudge and passively closes the outing as returned, so coming home early (or
  forgetting "I'm back") never triggers a call. Needs `current_lat`/`current_lon`
  in the poll body to activate.
- **Abandoned auto-close** ✅ — outings left open past `abandon_after_ratio`× the
  window (default 3×) are auto-closed as `abandoned` and logged as a drift `miss`
  (no fabricated duration), so they stop lingering active.
- **Feed outings into learning** ✅ — outing returns log `task` episodes that the
  pattern pass folds into `time_estimation` and the bias multiplier.
- **Finer `context_key`** — give outings their own pattern bucket so coffee runs
  calibrate separately from other tasks (still open).

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
