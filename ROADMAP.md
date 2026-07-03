# Roadmap

Prefrontal is in early development. This file tracks what's intentionally left as
a stub and what's planned next, so the gap between "scaffolded" and "finished"
stays visible to contributors. (See also `prefrontal modules -v` for per-module
intervention status.)

## ✅ Deployed and running (Module 1 live on the mini)

Prefrontal is **live on the Mac mini and in daily use** — real outings, calendar
sync, mail triage, the widget, and the nightly learn pass all run against a
multi-tenant deployment. The end-to-end Coffee Shop Nudge (outing endpoints, time
escalation, location-gating, abandoned auto-close, passive return, and the
learning + summarizer passes) is done and exercised in the wild. The original
bring-up runbook is kept below for reference / a fresh deploy (see
`docs/deployment.md`):

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

- **Self-care checks — "have you eaten?" + water** ✅ — the cues that deliberately
  pierce flow (a focus state is exactly when you forget to eat or drink), shipped
  as a sixth module, `prefrontal/modules/self_care.py`. A small registry of
  **basic-needs checks** rides the coaching tick (`evaluate()`), unified by a
  **daily target**: a **meal** is target 1 (from `meal_start_hour`≈11, re-ask
  every `meal_reask_minutes`≈40 until one "Ate" ends it for the day); **water** is
  target `water_daily_target`≈6 (from `water_start_hour`≈9, a "drink some water"
  reminder every `water_interval_minutes`≈90 where each **Drank** counts one and
  defers a full interval, done once the target's met). Cadence rides a per-interval
  *bucket* in the `dedup_key` so the engine fires each window once; responsive-hours
  + debounce come from the engine (no overnight nag). One-tap **Ate/Snooze** and
  **Drank/Snooze** ride the signed-action path (`meal_*`/`water_*` in
  `NUDGE_ACTIONS`, `meal`/`water` buttons in `notify.py`, `apply_self_care_action`
  in `/nudge/act`); progress is plain coaching-state cursors (`*_count` = `date|n`
  toward the target, `*_snoozed_until`), no schema change. Every confirm/snooze
  logs a `self_care` episode (seed for the cadence learner below).
  `/webhooks/coach/check` passes each cue's `actions` through and
  `deploy/n8n/coach-check.workflow.json` publishes them to ntfy, so the buttons
  render. **Off by default** — set the `self_care` coaching key to `on` (each check
  also has its own `meal_enabled`/`water_enabled`). Covered by
  `tests/test_self_care.py`. *(Next: the adaptive-cadence learner — see "Learning
  & adaptation" §6; and whether self-care grows into its own basics pack with
  meds/sleep.)*
- **Encouragement & recovery layer** ✅ — the counterweight to a system that
  nudges: when a day goes rough, shift tone from nudging to reassurance + a plan.
  `prefrontal/encouragement.py`'s deterministic `assess_day()` scores today's
  signals (a missed *hard* commitment is the heaviest at 3.0; each `miss`
  episode 1.0; double-bookings 0.5; a small rising-drift modifier) and flags
  `rough` past a threshold; `build_recovery()` composes existing logic into a
  plan — re-fit the rest of the day (`suggest_for_windows`), suggest deferring
  only *soft* commitments, and one tiny first step over the most-avoided todo
  (`decompose_task`); `render_encouragement()` writes it warm or plain, and
  `summarize_encouragement()` adds the optional Ollama prose pass with heuristic
  fallback. Surfaced at `GET /encouragement` (model-free; `already_sent` reflects
  a once-per-day cursor advanced by `POST /encouragement/sent`), `prefrontal
  encourage`, and `deploy/n8n/encouragement.workflow.json`. **Off by default**
  (the `encouragement` coaching key); auditable `signals`; covered by
  `tests/test_encouragement.py`. Standalone per `docs/encouragement.md`; the
  coaching agent can later wrap `assess_day` as one more cue producer.
- **Coaching agent — engine core + first evaluator** ✅ — the decision engine
  that turns *what Prefrontal knows* into the right nudge (see the full spec,
  `docs/coaching-agent.md`). `prefrontal/coaching.py` holds the pure core:
  `Cue`/`CoachContext`/`Decision`, `collect_cues` (one bad module can't sink the
  tick), `choose_channel` (urgency floor → learned `channel_response` bump),
  `suppressed` (quiet hours + per-`dedup_key` debounce, no schema change — a
  `coach_fired:*` coaching-state stamp), and `decide`. `Module.evaluate(store,
  ctx)` is the new opt-in hook (default `[]`), with two v1
  producers: **Task Paralysis** fires a `tiny_first_step` nudge over the
  worst-avoided open todo (reusing `avoided_todos` + the stored decomposition),
  and **Location Anchor** now emits the outing escalation cues (soft→nudge /
  firm→urgent / call→critical) through the same engine — its per-outing decision
  and side effects were lifted into a shared `evaluate_outing` /
  `apply_outing_evaluation` that `/webhooks/outing/check` and the evaluator both
  call, so the legacy endpoint is byte-identical (the full suite is the parity
  net) and there's one source of truth. Run one tick with `prefrontal coach`
  (`--dry-run` to see cues pre-suppression), or poll **`POST /webhooks/coach/check`**
  — the tick endpoint that fans over every enabled module and returns each
  fire-worthy cue with its chosen channel, deduped so a standing cue won't repeat
  (`deploy/n8n/coach-check.workflow.json` delivers them via ntfy at a
  channel-matched priority). Covered by `tests/test_coaching.py` +
  `tests/test_location_anchor.py`. This is steps 1 + 2 + 3 + both evaluators of
  the spec's rollout; LLM phrasing and outcome-logging correlation remain (see
  "Coaching agent" below).
- **Interactive nudge action buttons (ntfy)** ✅ — one-tap, background nudge
  responses with no app switch. `sign_action`/`verify_action`
  (`prefrontal/webhooks/oauth.py`) extend the signed one-tap-link mechanism from
  *dismiss* to an allowlisted action set, and `GET /nudge/act` runs the tapped
  action through the same close/record logic as its full endpoint: **Wrap up**
  (focus end), **I'm back** / **Abandon** (outing close), **Made it** / **Missed
  it** (departure outcome), and **Stay on task** / **Park it** / **Switch
  anyway** on the impulsivity reflective pause — collapsing its old two-call
  `switch` → `resolve` menu into a single background tap.
  `prefrontal/webhooks/notify.py` builds the ntfy `http` action-button specs, and
  the outing/focus/departure/pause endpoints return them as an `actions` list
  (empty unless a public origin + signing key are set) for a "publish to ntfy"
  delivery node (`deploy/n8n/interactive-nudge-ntfy.workflow.json`). Idempotent
  per button; covered by `tests/test_notify.py` + `tests/test_nudge_act.py`. This
  is the action-button core of the "Delivery layer + interactive nudges" item
  below; first-class Python publishing remains.
- **Multi-tenant (multiple users)** ✅ — one deployment now serves several people.
  A `users` table with per-user tokens (`sha256(token)`, shown once like an API
  key) and a `user_id` foreign key on every user-owned table; a `MemoryStore`
  bound to one user via `.scoped(user_id)` so no read/write can forget the scope;
  request resolution (`resolve_user` → `ScopedRequest`) and operator-only
  provisioning (`provision_user`, `POST /admin/users`). `coaching_state`/patterns
  are per-user, so each person learns independently. See `docs/multi-tenant.md`.
  *(This was the big "Beyond v1" item; it's now the default architecture.)*
- **Panic mode** ✅ — an overwhelm circuit-breaker for when you're too buried to
  think. `prefrontal/panic.py`'s deterministic `build_panic()` gathers everything
  actually bearing down — across calendar, todos, and mail — and ranks it into
  three buckets: **already behind** (commitments past their safe-departure or a
  hard meeting underway, overdue todos), **bearing down soon** (departures within
  ~2h, todos due today, urgent mail), and **piling up** (no hard clock — avoided
  todos, high-priority mail). Each item is tagged with where it came from
  (calendar label / inbox account) so work and home pressures sit side by side,
  and it picks the single most pressing *clocked* item for **one concrete first
  step**, reusing the Task Paralysis decomposition lever. (`fyi` commitments and
  already-ended events are excluded; date-only todo deadlines count as end-of-day
  so "due today" reads as *soon*, not *overdue*.) `render_panic()` emits steadying
  Markdown with an all-clear path; `summarize_panic()` adds an optional Ollama
  prose pass with heuristic fallback — the same two-layer shape as the briefing.
  Reachable four ways:
  - **On-demand:** `prefrontal panic` (`--llm` for prose) and `GET /panic`
    (structured buckets + rendered text + a one-line `headline`).
  - **Dashboard / family view:** a "😮‍💨 Panic" / "Feeling overwhelmed?" button
    opens a focused, dim-everything overlay with the first step front and center.
  - **One tap:** a "Panic" iOS Shortcut (`deploy/ios-shortcut.md`) that `GET`s
    `/panic` and reads back the `headline`.
  - **Proactive:** `POST /webhooks/panic/check` (+ `deploy/n8n/panic-check.workflow.json`)
    nudges **only when the plate tips into overwhelm** (`overwhelm_level()`:
    two-plus already late, or one late with a full plate). It edge-triggers on the
    level — like the departure signature — so a sustained pile-up nudges once, not
    every poll, with a cooldown floor. Tunable via the `panic_alert_min_pressing`
    and `panic_alert_cooldown_minutes` coaching-state keys.

  Endpoints live in `prefrontal/webhooks/routers/schedule.py`; covered by
  `tests/test_panic.py`. Delivers the get-back-on-track slice of the
  "encouragement & recovery layer" below, leaving the softer, tone-calibrated
  daily-recovery variant as the remaining piece. *(Next: gate the proactive
  `panic/check` nudge on quiet / responsive hours — it currently fires whenever
  polled, so an overwhelm push could land at 3am; capture whether the surfaced
  first step actually got done so overwhelm episodes feed learning — see
  "measure whether adaptations help" below; and reuse `overwhelm_level()` as one
  of the triggers for the encouragement layer's detector instead of a separate
  threshold.)*
- **"What fits right now" (widget)** ✅ — `GET /todos/now` computes the free gap
  until your next commitment (bounded by working hours + a cap) and returns the
  single best-fitting open todo — **biased toward the most-avoided** task and
  **preferring low-energy tasks later in the day** (honest prioritization, not the
  shiny thing). The Scriptable widget shows it on the home screen and all three
  Lock Screen accessories ("25m free · <todo>", or "catch up" for an avoided one).
- **Todo outcome capture** ✅ — closing a todo now logs a `task` episode (done ⇒
  `success`, drop ⇒ `miss`) via `record_todo_closed()` in `prefrontal/todos.py`,
  wired into `POST /todos/{id}/{action}` and `prefrontal todo done/drop`. This
  was the largest uninstrumented user-touch surface: the learning pass already
  saw outings, focus sessions, and mail, but a finished-or-abandoned todo — the
  moment an avoided task finally resolves — was thrown away. It feeds the `task`
  `drift` score; `actual_value` stays `None` (a todo's created→closed span is
  wall-clock, not time-on-task, so it never pollutes `time_estimation`), with the
  age kept in the episode `notes`. *(Departure outcomes are now captured too —
  see the next entry.)*
- **Automatic departure-outcome capture** ✅ — the mirror of the departure
  *reminder*: did you actually leave on time? A "leave Home" iOS geofence hits
  `POST /webhooks/departure/left`, which attributes the departure to the
  commitment you were heading to (`attribute_departure` — the soonest one whose
  leave window you're in), scores it on-time vs late against that commitment's
  computed leave-by (`classify_departure`, with a `departure_grace_minutes`
  tolerance), and logs a `departure` episode (`record_departure_outcome`). Like
  the abandoned-outing and closed-todo captures, `actual_value` is left `None` so
  it feeds the `departure` `drift` score without polluting the shared
  `time_estimation_bias` (leaving late must not *lower* the underestimate
  multiplier). Idempotent per commitment occurrence, and it silences any pending
  departure nudge for that commitment. All in `prefrontal/departure.py` +
  `routers/schedule.py`; covered by `tests/test_departure.py`. This was the last
  big uninstrumented user-touch surface flagged under "Learning & adaptation."
- **Attend-mode departures (work meetings you don't travel to)** ✅ — most work
  commitments are attended from wherever you already are (desk / WFH), so a
  travel-aware "leave now — 25 min travel" nudge for a meeting you'll take on your
  laptop was pure noise. `departure_mode()` now classifies each commitment: those
  on an attend-mode feed (`calendar_key` in `attend_calendars`, default `work`)
  skip travel logic entirely and fire a single short "starts in ~5 min — you're
  already where you need to be" reminder `work_departure_lead_minutes` before start
  (`basis="attend"`, no leave/travel copy, no heads_up→soon→go ladder). Two escape
  hatches restore the travel path for the rare in-person case: a `[commute]`/
  `[onsite]` tag in a single meeting's title/location, or flagging the whole day
  in-office via `POST /webhooks/departure/office-day` (a self-expiring toggle, tap
  it the mornings you commute). Both the check and the outcome (`/left`) surfaces
  plan the same way. In `prefrontal/departure.py` + `routers/schedule.py`; covered
  by `tests/test_departure.py`.
- **Task Paralysis module fully wired** ✅ — the last fully-stubbed module is now
  live, all three interventions `active`. **auto_decompose** breaks any todo over
  `decomposition_threshold_minutes` into a tiny first step at creation
  (`POST /todos`); **tiny_first_step** reframes a stalled task on demand
  (`POST /todos/{id}/decompose`, also fed to panic and `/todos/now`); and
  **body_double_nudge** is new — `repeat_stalled_tasks()`
  (`prefrontal/modules/task_paralysis.py`) finds tasks you keep bailing on
  (≥ `body_double_min_misses` `miss` episodes on the same title, not since
  resolved) and `GET /todos/stuck` surfaces each with a tiny first step and a
  start-together suggestion; the profile section names them so the coaching prose
  stops sending reminders and offers a body-double instead. Covered by
  `tests/test_modules.py` + `tests/test_webhooks.py`.
- **Avoidance detection** ✅ — `avoided_todos()` (`prefrontal/todos.py`) scores
  open loops by how long they've been skipped (age × priority), surfacing the
  important thing you keep putting off rather than letting it sink down the list.
  Exposed at `GET /todos/avoided` and woven into the morning briefing's "you keep
  putting off" line. *(Next input for the coaching agent's `tiny_first_step`
  picker — see `docs/coaching-agent.md`.)*
- **Editable todo deadlines + per-step check-offs** ✅ — `POST /todos/{id}/deadline`
  moves or clears a deadline on an open todo, and
  `POST /todos/{id}/steps/{step_index}/done` ticks off an individual decomposition
  step (tracked in `todo_decompositions.done_steps`, index 0 = the first step) so
  visible progress keeps a decomposed task moving. Both surface in `GET /todos`
  and the dashboard.
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
- **Scriptable home-screen & Lock Screen widget** ✅ — `deploy/scriptable/` polls
  `/outings`, `/commitments`, conflicts, todos, and `/todos/now` over Tailscale
  and renders a glanceable "right now": the active outing + escalation level, next
  commitments, conflict/todo counts, and the one todo that fits your current free
  window; taps open the `/family` view. One script drives every
  family: the full Home Screen card (Small/Medium/Large) **and** the iOS 16+ Lock
  Screen accessory slots (circular / rectangular / inline, monochrome via SF
  Symbols). *(Realizes the "iOS lock-screen widget" idea from the architecture.)*
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
  model is down. Run via `prefrontal summarize`, which now **caches** the
  narrative in the `profile_cache` table so `GET /profile` serves the prose
  without a per-request model round-trip (`?refresh=1` regenerates it,
  `?format=structured` returns the raw input; `X-Profile-*` headers report
  source/model/age/staleness). *(Next: optional Anthropic provider for
  higher-quality summaries.)*
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
  Energy-aware fitting shipped in the `/todos/now` picker (above). *(Next:
  multi-suggestion per window; auto-schedule a todo into a window as a commitment.)*

## Known stubs in the current code

- **n8n inbound handlers** — `POST /webhooks/n8n` classifies events via
  `parse_inbound_event()` but routes none of them to real handlers yet. The
  Triage agent spec (`docs/triage-agent.md`) is the plan to discharge this.
  *(`prefrontal/integrations/n8n.py`, `prefrontal/webhooks/routers/ingestion.py`.)*
- **Module interventions** — all five modules now have `status="active"`
  interventions: **Location-Aware Task Anchor** (escalation, location-gating,
  auto-close), **Hyperfocus** (protect/interrupt focus sessions), **Time
  Blindness** (departure timing + outcome capture), **Task Paralysis**
  (`tiny_first_step` / `auto_decompose` / `body_double_nudge` — see below), and
  **Impulsivity** (`reflective_pause` + `capture_and_defer` active). The only
  intervention still `planned` is Impulsivity's `switch_rate_feedback` (needs
  captured switch events — see `docs/impulsivity.md`). Run `prefrontal modules
  -v` for the live per-intervention status.

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

1. **Finish dense capture (largely done).** Todo closes, outings, focus, mail,
   the one-tap `POST /webhooks/shortcut` endpoint, and now **automatic departure
   outcomes** all log episodes. Departure capture closed the last big gap — did
   the user actually leave on time for a commitment? A "leave Home" geofence hits
   `POST /webhooks/departure/left`, which attributes the departure to the
   commitment it was for (`attribute_departure`), scores it on-time vs late
   against the computed leave-by (`classify_departure`), and logs a `departure`
   episode (`record_departure_outcome`) — `actual_value` left `None` so it feeds
   `drift` without polluting the shared `time_estimation_bias`. *(Next: derive
   the same signal passively from the stored location fix in
   `POST /webhooks/location` crossing the home radius, so it works without a
   dedicated geofence automation — needs a stored home coordinate + a prior-fix
   edge check; the geofence path above is the reliable default.)*
2. **LLM-as-sensor, not LLM-as-author.** ✅ (v1) — `prefrontal/sensor.py` turns a
   free-text note ("I always blow off admin on Mondays") into *candidate*
   structured updates: a `coaching_state` key/value or an episode. Crucially the
   model only **proposes** — `extract_candidates` emits JSON validated against a
   small **allowlist** (`PROPOSABLE_STATE_KEYS` / `PROPOSABLE_EPISODE_TYPES`, and
   it strips any fabricated predicted/actual numbers), candidates land as
   **pending** rows in a new `proposals` table, and only a human accept writes
   them — stamped `source="llm_inferred"` (distinct from `inferred`/`explicit`).
   No model reachable ⇒ *no* candidates (an honest no-guess fallback), mirroring
   the summarizer's grounded-prompt shape flipped to JSON. Driven by
   `prefrontal note "…"` and `prefrontal proposals list|accept|reject`; covered by
   `tests/test_sensor.py`. *(Next: HTTP surface — `POST /observe` + a `/proposals`
   review UI so it's one-tap from the phone; a conversation/transcript source; and
   letting an accepted state proposal feed the same calibration check as §4.)*
3. **Recency weighting / decay.** ✅ — `compute_patterns()` and `compute_bias()`
   now weigh each episode by an exponential decay on its age (`decay_weight`,
   `0.5 ** (age_days / half_life)`), so a recent shift in behavior moves the
   estimates faster than a year of stale data resists it. Confidence uses the
   *effective* sample size (summed weights), so a group of only-stale episodes is
   trusted less than the same count of fresh ones. Half-life is the
   `learning_half_life_days` coaching key (default 30d; set `0` to weigh all
   history equally — the pre-decay behavior). Covered by `tests/test_patterns.py`.
   *(Next: a sliding-window variant, and per-context half-lives once finer
   `context_key` bucketing lands.)*
4. **Close the loop: measure whether adaptations help.** ✅ — `bias_calibration`
   (`prefrontal/memory/patterns.py`) runs a **walk-forward** check each learn
   pass: it learns the `time_estimation_bias` from the older predicted/actual
   pairs and measures whether applying it lowered the mean absolute error on the
   *newer* pairs it never saw (`raw_error` → `adjusted_error`, `improvement`,
   `helps`) — honest, not circular. The verdict is persisted
   (`bias_calibration_helps` / `_improvement` / `_samples`) and surfaced in the
   profile ("this multiplier is improving recent estimates — trust it" / "⚠️ NOT
   improving — a reset may be due") and in `prefrontal learn` output, so a bad
   adaptation is *visible* rather than silently asserted. Covered by
   `tests/test_patterns.py` + `tests/test_summarizer.py`. *(Next: extend the same
   walk-forward check to the channel-choice and drift adaptations, and act on a
   "not helping" verdict automatically — decay the multiplier toward 1.0 or widen
   the confidence gate — rather than only reporting it.)*
5. **Generalize beyond the three hardcoded pattern types.**
   `time_estimation`/`channel_response`/`drift` are fixed in
   `prefrontal/memory/patterns.py`, and `context_key` is just the episode type.
   Adapting to a *new* dimension of someone's life is currently a code change,
   not learning. Add context-conditioned patterns (bias by task type, by time of
   day, by energy level) — the schema's `context_key` already supports finer
   bucketing — and derive `context_switch` once switch events are captured.
6. **Adaptive self-care cadence (with an honesty check).** ✅ — the self-care
   checks now *learn* their interval from how you actually respond
   (`adapt_self_care` / `adapt_self_care_interval` in
   `prefrontal/modules/self_care.py`, run in the nightly `learn`):
   - **Response latency is captured.** `mark_self_care_prompted` stamps the
     delivery time when a meal/water cue fires (from both `/webhooks/coach/check`
     and `prefrontal coach --deliver`); the Ate/Drank/Snooze tap records the
     nudge→tap latency in its `self_care` episode.
   - **Honesty check (the key guard).** When the *timed* confirms are mostly
     **instant** (within `INSTANT_CONFIRM_SECONDS`, ~reflexive dismissals), the
     learner *holds* the cadence rather than reading them as "needs it less" —
     so a scaffold that's just being swatted away is never quietly removed.
   - Otherwise: a high **snooze** rate (explicit "too often") *widens* the
     interval; genuine engagement (few snoozes + enough plausibly-timed confirms)
     *eases off* a little. v1 only ever widens, bounded to `MAX_INTERVAL_FACTOR`×
     the default, and never overrides an interval the user set explicitly. The
     verdict prints in `prefrontal learn`. Covered by `tests/test_self_care.py`.
   *(Next: fold in swept-unanswered as a "wrong channel/time" signal, and — per
   step 4 — verify the adapted cadence actually reduced misses before trusting it
   further.)*

## Beyond v1 (from the README architecture)

- **Triage agent** — a source-agnostic classify/prioritize/route step for any
  inbound signal (mail, calendar change, n8n event, manual capture), discharging
  the `parse_inbound_event` stub. Specced in
  [`docs/triage-agent.md`](docs/triage-agent.md). The shipped **mail ingestion**
  (above) is the first concrete slice — it triages email and routes actionable
  items to `todos`; the spec generalizes that single path into a reusable
  `Signal → TriageDecision → apply` core with a `triage_log`.
- **Coaching agent** — **the engine, tick endpoint, and both v1 evaluators have
  shipped** (see "Recently shipped": `prefrontal/coaching.py` + `Module.evaluate`
  + Task Paralysis & Location Anchor evaluators + `prefrontal coach` +
  `POST /webhooks/coach/check`; `outing/check` now shares the engine's
  `evaluate_outing`). Specced in [`docs/coaching-agent.md`](docs/coaching-agent.md):
  a tick-driven decision engine that asks each module's `evaluate()` hook "what's
  due?", then decides whether to fire, what to say, and on which channel (learned
  `channel_response` + quiet hours + debounce), logging the outcome back as an
  episode. **Outcome logging (§8) has now shipped** — a delivered interactive
  nudge is tracked by the `(context, target)` a one-tap ack arrives on
  (`note_delivered`); a tap through `/nudge/act` records an *acknowledged*
  `channel_response` episode (`resolve_ack`), and a nudge left unanswered past an
  ack window is swept into a *miss* (`sweep_stale_nudges`, run each tick). Both
  feed the per-channel ack-rate `patterns.py` already derives, so the nightly
  `learn` makes `choose_channel`'s "bump the channel you ignore" real instead of
  floor-only. `POST /webhooks/coach/ack` is the explicit hook for delivery layers
  that report their own outcomes; covered end-to-end by `tests/test_coaching.py`.
  Still open (spec §12 rollout): the optional LLM phrasing pass on ambient/digest
  cues (step 5); and folding in the encouragement layer below (§9). *(Next:
  deprecate `outing/check` once `coach/check` has run clean for a while — §13.)*
- **Delivery layer + interactive nudges (ntfy)** — **the action-button core has
  shipped** (see "Recently shipped": signed `/nudge/act` + the `actions` fields
  on the outing/focus/departure nudges). Unlike Pushover — whose only
  interactivity is an "open this URL" link — ntfy notifications support inline
  `http` action buttons that fire a request *directly from the notification*,
  with no app switch, so **Wrap up / I'm back / Abandon / Made it / Missed it**
  — and now **Stay on task / Park it / Switch anyway** on the reflective pause —
  are one background tap.
  - **first-class Python Pushover / Ntfy / TTS publishing** ✅ — a native
    delivery client (`prefrontal/integrations/delivery.py`) now publishes a
    coaching `Decision` directly, no n8n publish node required. `NtfyClient` /
    `PushoverClient` / `TTSClient` (local macOS `say`) are the transports;
    `DeliveryClient.deliver` maps the engine's channel class
    (`digest`→held, `push`/`sound`/`voice`) to one, preferring ntfy so the
    inline action buttons render (Pushover carries the first action as a tap
    URL). Per-user routing (`resolve_route`) reads `ntfy_topic` /
    `pushover_user_key` / `tts_enabled` from `coaching_state` over operator
    defaults in `Settings` (the multi-tenant §6.5 delivery fields); all-empty is
    the local-first no-op (nothing leaves the box), and a down transport is
    reported, never raised. Quiet-hours + debounce are **not** re-done here — the
    engine's `suppressed`/`record_fired` already gated the decision; this layer
    only routes and sends. Wired into `prefrontal coach --deliver` (a launchd
    tick can now deliver without n8n); covered by `tests/test_delivery.py`.
  - **proactive panic nudge — first step inline + open-triage button** ✅ — the
    overwhelm nudge already carried the first step in its message; it now also
    returns an `actions` list (`panic_actions`, a signed-free ntfy `view` button)
    that deep-links to `GET /dashboard?panic=1`, which auto-opens the panic
    overlay on load. So the nudge delivers the one step to start *and* is one tap
    from the whole triage. `POST /webhooks/panic/check` returns the buttons (empty
    unless a public origin is set), and `deploy/n8n/panic-check.workflow.json` now
    publishes to ntfy with them (retiring its Pushover-only publish). Covered by
    `tests/test_panic.py` + `tests/test_notify.py`.

  - **ntfy is now the default delivery across the board** ✅ — every remaining
    Pushover-based n8n workflow was converted to publish to ntfy (the
    `prefrontal-me` topic): departure-reminder, coffee-shop-nudge (the 50%/100%
    pushes; the 150% **Twilio call stays**), hyperfocus-check, calendar-sync
    (double-booking + possible-conflict alerts), and morning-briefing — joining
    panic-check, coach-check, interactive-nudge, and encouragement which already
    did. Each nudge workflow passes the endpoint's signed `actions` through so the
    one-tap buttons render. This also **cleaned up the Pushover-era workarounds**:
    - the `shortcuts://run-shortcut?name=End%20focus` deep link in
      hyperfocus-check is gone — the workflow now publishes the endpoint's real
      signed **Wrap up** button (background `GET /nudge/act`, no app-switch, no
      brittle name-match);
    - nudge copy that tiptoed around Pushover's lack of buttons is fixed (the
      hyperfocus check now says "tap Wrap up", not "open End focus");
    - the native delivery client (`delivery.py`) attaches buttons for the
      self-care **meal/water** cues too, not just outing/departure/focus;
    - the two-call `switch` → `resolve` flow / multi-tap menu was already single
      inline buttons for ntfy (the Shortcuts menu path remains for Pushover).

    Pushover isn't removed — the native `DeliveryClient` still supports it as a
    fallback (no inline buttons; the first action rides as a tap URL), and the
    `.env.example` documents both. Local-first stays the default (ntfy is
    self-hostable). Covered by `tests/test_delivery.py` + `tests/test_notify.py`.

  Local-first stays the default (ntfy is self-hostable). Pairs with the
  coaching-agent delivery routing and per-user delivery (multi-tenant) below.
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
- **Encouragement & recovery layer** ✅ **(shipped — see "Recently shipped")** —
  the deterministic detector + recovery plan + `GET /encouragement` are live
  (`prefrontal/encouragement.py`, spec `docs/encouragement.md`), opt-in and
  tone-calibrated, alongside **panic mode** for the acute-overwhelm case. Two
  small follow-ons remain: the **morning-briefing tone variant** (spec §6.2 —
  when the day already looks rough at wake-up, swap the briefing's closing bias
  nag for the encouragement acknowledgement) and, once the assessment is trusted,
  wrapping `assess_day` as a **coaching-agent cue producer** so its delivery
  routes through the shared engine (coaching-agent spec §9 — one `assess_day`).
- **Native Lock Screen Live Activity (live outing timer)** — the shipped
  Scriptable widget renders the Lock Screen accessory slots but refreshes only on
  iOS's ~15-min timeline cadence, so an active outing's elapsed time is stale
  between refreshes. A true *live* timer that counts up in real time on the Lock
  Screen (and in the Dynamic Island) needs a native **WidgetKit + ActivityKit
  Live Activity**, which Scriptable can't provide. This would be a small Swift
  app: start an Activity on `outing/start` (via a Shortcut or a push), update it
  as the escalation `level` changes, and end it on return — reading the same
  `/outings` data and per-user token. Cost is real: it needs Xcode, an Apple
  Developer account, and building/sideloading the app to the phone, so it's an
  opt-in upgrade rather than a replacement for the Scriptable widget, which stays
  the zero-native-code default. *(Deferred by choice; the accessory widget covers
  the glanceable case today.)*
- **Context Packs — composition over primitives, not new modules.**
  Challenge-area modules answer *how your ADHD shows up* (time blindness,
  hyperfocus, impulsivity, …); a **Pack** answers *what life you're managing* —
  **Parent**, **Caregiver**, **Grad student**, **New job**. Crucially a Pack is
  **not** a `Module` subclass: modules live on the executive-function-challenge
  axis, and a role like "parent" is an orthogonal *life-context* axis (a parent
  still has time blindness — "parent" isn't a peer of "hyperfocus"). It's a higher
  **composition layer** that (1) switches on a relevant subset of challenge
  modules, (2) seeds domain vocabulary — commitment `kind`s (today `self`/`fyi`;
  add e.g. `child`), todo `categories`, default windows — and coaching prefs,
  (3) registers a few **situation tools** that are thin compositions of existing
  primitives (todos, commitments, departure, panic, `decompose_task`, the
  NL-assistant `ALLOWED_OPS` whitelist, delivery), and (4) tailors surfaces like
  the `family` view. Proactive cues route through the shipped `Module.evaluate`
  tick engine — a Pack lights up nudges by enabling modules that already evaluate,
  or by contributing its own cue source. **Example — Parent:** school-run
  departure (departure + a geocoded `place`), pack-the-bag checklist
  (`decompose_task`), "who's covering pickup?" (conflict detection + the `kind`
  classifier over a shared calendar), "kid's home sick → replan the day" (panic +
  todo re-fit into the changed windows). Deliberately mostly declarative + a small
  tool registry, so a Pack is cheap to add and shareable ("install the Parent
  pack") — the same opt-in, modular ethos as challenge modules, on the orthogonal
  context axis. *(Open: precedence when Packs overlap — two setting the same
  category/window need merge rules; and the co-parent/household bits imply
  **shared/household scope**, beyond today's per-user multi-tenancy.)*
- **Shared household sheet — co-parent facts, agreements & load-balancing.**
  The first concrete driver for **household scope** (the open question on Context
  Packs above): today all data is strictly per-user (`store.scoped(user_id)`), but
  co-parents need shared read/write. The data model comes straight from a real
  co-parent tracker (the *Kids Info Tracker* template — its own tagline names the
  goal: *"everything you need to step in and help — any time, no asking
  required"*):
  - **Facts** — per-kid, categorized key/values: sizes (tops / bottoms / shoes +
    brand notes), routines (wake / breakfast / ready-by), food (allergies +
    severity + EpiPen), health (pediatrician / dentist / insurance / meds),
    school & activities. Each stamped `updated_by`/`updated_at`.
  - **Agreements** — standing behavior plans with light structure: a star/points
    chart with *earn-only* rules and reward thresholds (e.g. 10⭐ → small, 30⭐ →
    big), plus the consistency notes both parents follow so the kids get one
    answer, not two.
  - **Key contacts** (role / name / phone / email) — a facts facet; and **running
    shopping needs** (item · child · size/spec · where to buy · got-it) reuse
    child-tagged **todos** rather than a new list.

  Render it into the existing **family view** as a single visible sheet (*kids'
  facts · agreements · upcoming appts · recently changed*), reusing the
  `profile`/`profile_cache` render pattern rather than building a new UI. Edit it
  in plain English through the shipped **NL assistant** — extend `ALLOWED_OPS`
  with `set_fact`/`set_agreement`; appointments reuse commitments + the `child`
  `kind`. The point is **balancing invisible load**: provenance makes "who's
  carrying the mental load" legible, and the coaching/briefing layer pushes the
  deltas that matter to the *other* parent ("Sam's shoe size → 13; dentist Tue 3pm
  — you're on pickup"), so it's a load-balancer, not a passive shared note. Ships
  as the backbone of the **Parent** Context Pack. Full design:
  [`docs/household-sheet.md`](docs/household-sheet.md). *(Design leanings: a real
  `household` entity users belong to (cleaner than a shared pseudo-user);
  last-write-wins + provenance over richer merge at this scale; v1 = the visible
  shared sheet, v2 = the proactive delta-digest to the other parent. Open:
  household membership/invite model.)*

- **"Have you eaten?" — a self-care nudge that pierces flow.** ✅ **(shipped — see
  "Recently shipped")** — the meal check is live as the `self_care` module: from a
  tunable start hour it re-asks *"have you eaten?"* even mid-focus, with one-tap
  **Ate** / **Snooze** on ntfy, capped once a day and gated on responsive hours.
  Off unless the `self_care` coaching key is `on`. *(Still open: whether it becomes
  a facet of a broader self-care/basics pack alongside water/meds/sleep, and
  whether a confirmed meal logs an episode so the learning loop notices skip-days.)*
- **Shopify MCP — record-shop assistant + used-collection buying.** Connect to
  (or self-host) a **Shopify MCP server** so Prefrontal can read the record shop's
  catalog, inventory, and sales, turning the assistant into a genuine shop
  co-pilot rather than a personal-only tool. The near-term win is **buying
  decisions**: sell-through and stock signals from Shopify inform what to reorder
  and what's dead weight. The bigger prize is **used collections** — when a lot of
  used records comes in, help appraise and price it: pull comps, weigh
  condition/rarity against local sell-through, and flag what's worth buying vs
  passing. Slots onto the existing MCP tool surface (the assistant already reaches
  MCP tools via tool-search) and the NL-assistant/triage spine; a batch of
  candidate purchases is naturally a triage problem (`Signal → TriageDecision →
  apply`) and the actions land as todos/commitments. Architecturally this is the
  first **non-ADHD, small-business Context Pack** ("Record Shop") on the
  orthogonal life-context axis — a Shopify tool registry + shop vocabulary, reusing
  todos, triage, and delivery. Local-first stays the default; the Shopify
  connection is explicit and opt-in, like the Anthropic provider path. *(Open:
  Shopify auth/scopes and whether to run our own MCP server vs. a hosted one;
  comps data source for used pricing; multi-tenant boundary between shop data and
  personal data.)*

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
