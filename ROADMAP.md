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

- **Coaching agent — engine core + first evaluator** ✅ — the decision engine
  that turns *what Prefrontal knows* into the right nudge (see the full spec,
  `docs/coaching-agent.md`). `prefrontal/coaching.py` holds the pure core:
  `Cue`/`CoachContext`/`Decision`, `collect_cues` (one bad module can't sink the
  tick), `choose_channel` (urgency floor → learned `channel_response` bump),
  `suppressed` (quiet hours + per-`dedup_key` debounce, no schema change — a
  `coach_fired:*` coaching-state stamp), and `decide`. `Module.evaluate(store,
  ctx)` is the new opt-in hook (default `[]`), and **Task Paralysis** is the first
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
- **Coaching agent** — **the engine, tick endpoint, and both v1 evaluators have
  shipped** (see "Recently shipped": `prefrontal/coaching.py` + `Module.evaluate`
  + Task Paralysis & Location Anchor evaluators + `prefrontal coach` +
  `POST /webhooks/coach/check`; `outing/check` now shares the engine's
  `evaluate_outing`). Specced in [`docs/coaching-agent.md`](docs/coaching-agent.md):
  a tick-driven decision engine that asks each module's `evaluate()` hook "what's
  due?", then decides whether to fire, what to say, and on which channel (learned
  `channel_response` + quiet hours + debounce), logging the outcome back as an
  episode. Still open (spec §12 rollout): the optional LLM phrasing pass on
  ambient/digest cues (step 5); outcome-logging correlation ids (§8); and folding
  in the encouragement layer below (§9). *(Next: deprecate `outing/check` once
  `coach/check` has run clean for a while — spec §13.)*
- **Delivery layer + interactive nudges (ntfy)** — **the action-button core has
  shipped** (see "Recently shipped": signed `/nudge/act` + the `actions` fields
  on the outing/focus/departure nudges). Unlike Pushover — whose only
  interactivity is an "open this URL" link — ntfy notifications support inline
  `http` action buttons that fire a request *directly from the notification*,
  with no app switch, so **Wrap up / I'm back / Abandon / Made it / Missed it**
  — and now **Stay on task / Park it / Switch anyway** on the reflective pause —
  are one background tap. Still open:
  - **first-class Python Pushover / Ntfy / TTS publishing** — today an n8n node
    does the publish by reading the `actions` a nudge returns; a native delivery
    client (routing, quiet hours, debounce) belongs with the coaching agent.
  - the proactive **panic** nudge carrying its first step inline + an action to
    open the full `/panic` triage.

  Doing this should also **clean up the Pushover-era workarounds** adopted while
  Pushover was the only channel:
  - the `shortcuts://run-shortcut?name=End%20focus` deep link in
    `deploy/n8n/hyperfocus-check.workflow.json` — it works but foregrounds the
    Shortcuts app and strands the user, and it's brittle (hardcodes the exact
    shortcut name);
  - nudge copy that tiptoes around Pushover's lack of inline actions (e.g.
    "…open End focus to wrap up" instead of a real **Wrap up** button);
  - the two-call `switch` → `resolve` flow and the multi-tap menu shortcuts —
    **done for ntfy**: the pause's Stay / Park it / Switch anyway are now single
    inline buttons (the Shortcuts menu path remains for the Pushover fallback).

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
  naturally with the coaching agent above. *(Partly shipped: **panic mode**
  (above) already handles the acute overwhelm case with a one-tap headline; what
  remains here is the softer, tone-calibrated daily-recovery variant.)*
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

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
