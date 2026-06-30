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
7. **Schedule learning** ✅ — nightly `prefrontal learn && prefrontal summarize`
   via `deploy/learn.sh` + `deploy/com.morningstatic.prefrontal-learn.plist`
   (launchd `StartCalendarInterval`, 03:30); see deployment §12. Load it and the
   profile recalibrates on its own.

Everything above the dry run is configuration; no further code is required for
the first test. Code follow-ups below are optional polish.

## Recently shipped

- **Todo outcome capture** ✅ — closing a todo now logs a `task` episode (done ⇒
  `success`, drop ⇒ `miss`) via `record_todo_closed()` in `prefrontal/todos.py`,
  wired into `POST /todos/{id}/{action}` and `prefrontal todo done/drop`. This
  was the largest uninstrumented user-touch surface: the learning pass already
  saw outings, focus sessions, and mail, but a finished-or-abandoned todo — the
  moment an avoided task finally resolves — was thrown away. It feeds the `task`
  `drift` score; `actual_value` stays `None` (a todo's created→closed span is
  wall-clock, not time-on-task, so it never pollutes `time_estimation`), with the
  age kept in the episode `notes`. *(Next: capture departure outcomes
  automatically — see "Learning & adaptation" below.)*
- **Mail ingestion + triage** ✅ — `prefrontal/mail/` normalizes a batch of
  messages, triages each (Ollama with a deterministic heuristic fallback) into
  `needs_action`/`urgency`/`category`/one-line `summary`, dedupes on the
  account-scoped `message_id`, and **surfaces actionable mail as `todos`** so it
  flows into the existing open-loop machinery (fit, briefing). Per-account
  retention (`full` vs `signals`) keeps bodies local or never-stored. Surfaced
  via `prefrontal mail` (list/sync/fetch), `POST /webhooks/mail/sync`, and
  `GET /mail`. *(This is the first concrete slice of the broader Triage agent —
  see `docs/triage-agent.md` for the source-agnostic generalization.)*
- **Hyperfocus focus sessions** ✅ — `prefrontal/modules/hyperfocus.py` +
  `focus_sessions` table: a declared deep-work block with an optional plan and an
  `aligned` "is this what I meant to do?" bit. Asymmetric by design — it
  *protects* an aligned block from other nudges while healthy, and only
  interrupts to gently check alignment once it overruns or to force a break past
  the hard ceiling. Wired end-to-end via `POST /webhooks/focus/{start,check,end}`
  and `GET /focus`; all four interventions are `active`.
- **Todo decomposition (tiny first step)** ✅ — `prefrontal/todos.py` +
  `todo_decompositions` table: a todo big enough to stall on
  (≥ `decomposition_threshold`) is broken into a tiny first step
  (≤ `max_first_step_minutes`) plus collapsed remaining steps — the task
  initiation lever for the Task Paralysis module (Ollama + heuristic fallback).
- **Scriptable home-screen widget** ✅ — `deploy/scriptable/` polls `/outings`,
  `/commitments`, conflicts, and todos over Tailscale and renders a glanceable
  "right now": the active outing + escalation level, next commitments, and
  conflict/todo counts; taps open the `/family` view. *(This is the
  "iOS lock-screen widget" idea from the architecture, now shipped.)*
- **Commitment geocoding (places → cache → Nominatim)** ✅ —
  `prefrontal/geocode.py` resolves a commitment's free-text `location` to
  `dest_lat`/`dest_lon` so the departure reminder's travel estimate actually
  fires. Layered + local-first: a user-curated `places` alias table
  (`POST /places`, instant/offline), then a `geocode_cache` (incl. recorded
  misses), then an **opt-in** Nominatim geocoder
  (`prefrontal/integrations/nominatim.py`, gated by the `geocoding_enabled`
  state flag — off by default). Enrichment runs best-effort on calendar sync and
  manual add, with `POST /commitments/geocode` to backfill. Failures degrade to
  the `lead_minutes` fallback. *(Next: a CLI for places; reverse-geocode the
  iOS location ping for nicer context; self-host Nominatim on the mini.)*
- **Last-known location + travel-aware departure reminders** ✅ —
  `POST /webhooks/location` stores the phone's position (one iOS "Update
  location" automation), so the coffee-shop nudge gates on location **without
  Home Assistant** (the check falls back to the stored fix). `prefrontal/
  departure.py` + `POST /webhooks/departure/check` then compute *when to leave*
  for the next commitment: a local, bias-adjusted travel estimate
  (straight-line distance × road-factor ÷ speed, no maps API) from the stored
  location to the commitment's optional `dest_lat`/`dest_lon`, escalating
  heads-up → soon → go and deduped per `(commitment, level)`. Falls back to the
  static `lead_minutes` when coordinates or a recent fix are missing. The
  rewritten `deploy/n8n/departure-reminder.workflow.json` polls it and pushes via
  Pushover. *(Next: optional geocoding of free-text `location`; per-commitment
  travel learning; surface the leave-by time in the briefing and widget.)*
- **Pattern-computation pass** ✅ — `prefrontal/memory/patterns.py` derives
  `time_estimation`, `channel_response`, and `drift` patterns from `episodes`
  (confidence = `n/(n+k)`) and recomputes the `time_estimation_bias` multiplier.
  Run via `prefrontal learn` (scheduled nightly — see step 7 above). *(Next: add
  finer `context_key` bucketing than episode type; derive `context_switch` once
  switch events are captured.)*
- **LLM-backed summarizer** ✅ — `summarize_profile()` feeds the structured
  profile to a local Ollama model (`prefrontal/integrations/ollama.py`) and
  returns prioritized coaching prose, falling back to the heuristic when the
  model is down. Run via `prefrontal summarize`. *(Next: optional Anthropic
  provider for higher-quality summaries; cache/serve the narrative from
  `GET /profile`.)*
- **Calendar ingestion + double-booking** ✅ — `commitments` table +
  `prefrontal/commitments.py`: feed-aware calendar sync
  (`/webhooks/calendar/sync`, personal Google + work ICS merged), manual add,
  `GET /commitments`, and overlap detection at `GET /commitments/conflicts` with
  a Pushover alert in the sync workflow.
- **Impact analysis** ✅ — `prefrontal/impact.py`: projects realistic free-time
  from the `time_estimation_bias` and flags upcoming commitments now at risk
  (`start_at − lead_minutes` vs projection). Surfaced in `/webhooks/outing/check`
  (an `impact` list + `hard_conflict` flag) and named in the nudge ("…'Team sync'
  is now at risk"). *(Next: full cascade/domino propagation through the chain;
  expose impact beyond outings.)*
- **Morning briefing** ✅ — `prefrontal/briefing.py`: a daily digest of today's
  commitments, double-bookings, what slipped this past week, and a coaching note
  (the time bias), honoring `preferred_briefing_format`. `GET /briefing` +
  `prefrontal briefing` (`--llm` for Ollama prose, heuristic fallback); delivered
  by `deploy/n8n/morning-briefing.workflow.json`.
- **Todos + time-fitting** ✅ — `todos` table + `prefrontal/scheduling.py`: open
  loops (call the dentist, plan a birthday) with estimate/priority/deadline, plus
  `free_windows()` over the schedule and `fit_todos()` that ranks what fits a gap
  (bias-adjusted). `GET/POST /todos`, `GET /todos/fit?minutes=N`,
  `prefrontal todo`/`fit`, and a "spare time" section in the morning briefing.
  *(Next: multi-suggestion per window; auto-schedule a todo into a window as a
  commitment; energy-aware fitting.)*

## Known stubs in the current code

- **n8n inbound handlers** — `POST /webhooks/n8n` classifies events via
  `parse_inbound_event()` but routes none of them to real handlers yet. The
  Triage agent spec (`docs/triage-agent.md`) is the plan to discharge this.
  *(`prefrontal/integrations/n8n.py`, `prefrontal/webhooks/app.py`.)*
- **Module interventions** — three of the five modules are wired end-to-end with
  `status="active"` interventions: **Location-Aware Task Anchor** (escalation,
  location-gating, auto-close), **Hyperfocus** (protect/interrupt focus
  sessions), and **Time Blindness**. The remaining two are still declared stubs:
  **Task Paralysis** (decomposition exists in `todos.py`, but its `tiny_first_step`
  / `auto_decompose` / `body_double_nudge` interventions are still `planned`) and
  **Impulsivity** (`reflective_pause` / `capture_and_defer` / `switch_rate_feedback`
  all `planned` — see `docs/impulsivity.md`). Run `prefrontal modules -v` for the
  live per-intervention status.

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

## Learning & adaptation — the road past v1

Prefrontal's "it gets better the longer you use it" loop is real but narrow:
`episodes` → `recompute_patterns()` (deterministic stats) → `coaching_state` /
`patterns` → `summarize_profile()` (LLM renders prose) → injected into every
agent prompt. The only thing that *adapts* is the deterministic layer; the LLM
sits at the end and renders, deliberately kept out of the write path so the
profile can't be hallucinated into.

The honest constraint is that **learning quality is capped by observable,
structured signal — not by model quality or prompt design.** The steps below are
ordered by leverage; each is independent but builds on denser capture.

1. **Finish dense capture (in progress).** Todo closes now log episodes (above);
   outings, focus, mail, and the one-tap `POST /episode` endpoint already do. The
   remaining gap is **automatic departure outcomes** — did the user actually
   leave on time for a commitment? Today that outcome is only captured if the
   user taps `made_it`/`missed_it` (`POST /episode`); auto-capturing it needs an
   actual-departure signal (a geofence exit or the stored location fix in
   `POST /webhooks/location` crossing the home radius), compared against the
   computed leave-by time in `prefrontal/departure.py`. That's a real feature,
   not a one-liner — hence its own line item rather than folding into the todo
   work.
2. **LLM-as-sensor, not LLM-as-author.** Add a path that turns *unstructured*
   signal (a free-text note, a conversation, an observed behavior) into
   *candidate* episodes or `coaching_state` updates — e.g. "I always blow off
   admin on Mondays" has nowhere to land today, because the loop only learns from
   things already shaped as structured episodes. The model should *propose* into
   the existing deterministic / human-confirmed write path (a new `source` value
   like `llm_inferred`, distinct from `inferred`/`explicit` in `coaching_state`),
   never write authoritative facts directly. This preserves the auditable spine
   while widening what can be observed. The summarizer's grounded-prompt +
   heuristic-fallback shape (`prefrontal/memory/summarizer.py`) is the pattern to
   mirror, flipped to emit structured JSON instead of prose.
3. **Recency weighting / decay.** `compute_patterns()` and `compute_bias()` weigh
   every episode equally regardless of age, so the profile tracks cumulative
   history and is slow to follow a person who has *changed*. Add a half-life /
   exponential decay (older episodes count less) or a sliding window, so a recent
   shift in behavior moves the bias faster than a year of stale data resists it.
4. **Close the loop: measure whether adaptations help.** Nothing today verifies
   that a learned value actually improves outcomes — e.g. does applying the 1.4×
   `time_estimation_bias` reduce subsequent `miss` rates? Track prediction error
   over time (post-bias predicted vs actual) and surface it, so a bad adaptation
   is visible and self-correcting rather than asserted. This also gives the
   signal to know when to trust a pattern enough to act on it more assertively.
5. **Generalize beyond the three hardcoded pattern types.**
   `time_estimation`/`channel_response`/`drift` are fixed in
   `prefrontal/memory/patterns.py`, and `context_key` is just the episode type.
   Adapting to a *new* dimension of someone's life is currently a code change,
   not learning. Add context-conditioned patterns (bias by task type, by time of
   day, by energy level) — the schema's `context_key` already supports finer
   bucketing — and derive `context_switch` once switch events are captured.

## Beyond v1 (from the README architecture)

- **Triage agent** — a source-agnostic classify/prioritize/route step for any
  inbound signal (mail, calendar change, n8n event, manual capture), discharging
  the `parse_inbound_event` stub. Specced in
  [`docs/triage-agent.md`](docs/triage-agent.md). The shipped **mail ingestion**
  (above) is the first concrete slice — it triages email and routes actionable
  items to `todos`; the spec generalizes that single path into a reusable
  `Signal → TriageDecision → apply` core with a `triage_log`.
- **Coaching agent** — generate reminders/check-ins from the profile (the
  morning briefing is the first slice of this). Specced in
  [`docs/coaching-agent.md`](docs/coaching-agent.md): a tick-driven decision
  engine that asks each module's new `evaluate()` hook "what's due?", then
  decides whether to fire, what to say, and on which channel (learned
  `channel_response` + quiet hours + debounce), logging the outcome back as an
  episode. Generalizes the `outing/check` loop and folds in the encouragement
  layer below.
- **Delivery layer** — first-class Pushover / Ntfy / TTS integrations in Python
  (today delivery is handled in n8n).
- **Ingestion** — core mail monitoring has **shipped** (see "Recently shipped":
  `prefrontal/mail/` with IMAP fetch + n8n/Apps-Script batch sync). Still open:
  the Google Apps Script work-email digest as an alternative source, and folding
  ingestion under the general Triage agent (`docs/triage-agent.md`).
- **Optional Anthropic provider** — keep inference local by default, but add an
  opt-in Anthropic API path (e.g. Claude Haiku for cheap, high-quality
  summaries; a larger model for heavier coaching/triage reasoning), selectable
  per agent as the README describes. The summarizer already takes an injected
  client, so this slots in behind the same interface. Local-first stays the
  default; the cloud path is explicit and configurable.
- **Multiple users** — specced in detail in
  [`docs/multi-tenant.md`](docs/multi-tenant.md) (shared DB, row-level `user_id`
  scoping); the sketch below is the summary it supersedes. Today Prefrontal is
  single-tenant throughout: one
  SQLite DB, a global `state` table (the behavioral profile, `home_radius_m`,
  `time_estimation_bias`, etc.), one `PREFRONTAL_WEBHOOK_SECRET`, and a single
  Pushover/Twilio delivery target in the n8n workflows. Multi-user support would
  require: a `users` table and a `user_id` foreign key on the per-user tables
  (`episodes`, `outings`, `commitments`, `todos`, and the `state`/patterns rows,
  which would become per-user); per-user auth (a token or key per user rather
  than one shared secret) and request scoping so every endpoint resolves the
  caller; per-user delivery routing (each user's own Pushover/Twilio/Shortcut
  identifiers, so a nudge goes to the right phone); and per-user learning so the
  pattern pass and summarizer compute a profile per user instead of globally.
  The pure cores (escalation, impact, scheduling, summarizer) are already
  user-agnostic and would carry over unchanged; the work is in the storage
  layer, auth, and delivery. *(Open design question: separate DB-per-user vs. a
  shared DB with row-level scoping — the latter is simpler to operate and is the
  likely default.)*
- **Encouragement & recovery layer (optional)** — when a day is going badly,
  shift tone from nudging to reassurance. A detector watches the signals already
  computed — the briefing's "what slipped", rising `drift`, a missed *hard*
  commitment, repeated outings run long — and, past a threshold, emits an
  encouraging message that acknowledges the rough day without judgment, then
  gives concrete get-back-on-track recommendations: re-fit the day's todos into
  the remaining free windows (`fit_todos()` + free-window logic already exist),
  suggest deferring or dropping low-priority commitments, and name one small
  next step. Opt-in and tone-calibrated to the coaching prefs (so it never
  becomes saccharine for users who don't want it). Local-first: the trigger and
  recommendation logic are deterministic; the Ollama summarizer phrases the
  reassurance with a heuristic fallback. Likely surfaces as a briefing variant
  plus a `GET /encouragement`-style endpoint an n8n flow can deliver. Pairs
  naturally with the coaching agent above.

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
