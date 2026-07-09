# Changelog

Shipped work in Prefrontal. This is the release-notes companion to
[`ROADMAP.md`](ROADMAP.md), which now tracks only what's *planned* or open.
Entries are moved verbatim from the old roadmap, so a few inline "see below" /
"see §N" references point into `ROADMAP.md`'s forward-looking sections.

## Recently shipped

- **Delegate a todo to an assistant (prep / follow-up hand-off)** ✅ — some open
  loops are less "do a tiny first step" and more "someone should go dig up the
  options, draft the email, and hand it back ready to send." `prefrontal/delegation.py`
  is that hand-off: a todo is delegated to a pluggable **handler** that does the
  prep and writes it onto a new `todo_delegations` row (one per todo, mirroring
  `todo_decompositions`). Two handlers ship, chosen from a registry (`HANDLERS`
  derived from `_HANDLERS`, so the API can't accept a handler it can't dispatch):
  `agent` — the local model writes a research **brief** + **draft communications**
  straight back on-box (status `prepped`, ready to review, house-style LLM call
  with a heuristic fallback so it still produces an outline offline); and `email`
  — the same brief is composed into a message and sent to a human VA over the
  user's own SMTP source (status `forwarded`; if SMTP isn't configured or the
  relay errors, the brief is still stored and the status is `failed` so nothing is
  lost). Lifecycle `forwarded → in_prep → prepped → returned/failed`, with a
  heads-up push (`deliver_to_member`) when it lands. This is Prefrontal's first
  **outbound-email** path (`prefrontal/integrations/smtp.py` — stdlib `smtplib`, a
  no-op when unconfigured, never raises); SMTP credentials live as a **per-user
  Fernet-encrypted `sources` row** (`kind="smtp"`), configured on the Settings page
  (`GET`/`POST /smtp`, the password sealed at rest and never echoed back). Surfaces:
  `POST /todos/{id}/delegate` + `/delegate/return` (the delegation rides along on
  `GET /todos`), a `prefrontal todo delegate` CLI command, and an NL `delegate_todo`
  op on the `/assistant` box ("have the assistant prep the dentist call" → an
  agent hand-off; "get my VA on X, email jane@…" → an email one). The assistant's
  execute layer now threads the selected model client through `execute_actions`
  so the op can write its prep brief.

- **Ambiguity clarification + guided playbooks (a Task-Paralysis lever)** ✅ —
  task paralysis has a quieter cause than size: you can't start what you can't
  *name*. A calendar event called "Tax" or a todo that just says "Mom" stalls
  because it could mean several different things. `prefrontal/clarify.py` gives the
  system a way to notice that ambiguity and hone it in before it becomes another
  avoided loop: a pure `ambiguity_score` heuristic (short / single-word /
  known-ambiguous titles, discounted when a clear action verb or a concrete detail
  is present) gates a local-model pass that proposes ONE clarifying question with a
  few candidate readings (`detect_clarification`, LLM-first with a hand-authored
  heuristic fallback so the "Tax" case works fully offline). The question lands as
  a **pending** `clarifications` row — the same propose-then-confirm safety model
  as the LLM sensor — and is surfaced inline in a dashboard "Needs clarification"
  card. Answering it records the chosen reading (a `todo`'s notes are honed
  non-destructively), and a reading that maps to a recognized **task type** (e.g.
  `tax_filing`) opens a step-by-step guided *playbook* in a dim-everything overlay
  (the same overlay pattern panic mode uses) — the "pop-up that guides me through
  the task." Dismissing marks an item not-ambiguous so the sweep never re-asks.
  the detection sweep (`sweep_ambiguous_items`) runs **on the coaching tick**
  (`POST /webhooks/coach/check` and `prefrontal coach --deliver`, beside
  `sweep_avoided_decompositions`), so the queue fills passively — bounded model
  calls per tick, and it never re-asks an item it has history for.
  `POST /clarifications/check` is the on-demand "check now" twin;
  `GET /clarifications`, `POST /clarifications/{id}/resolve|dismiss`,
  `GET /clarifications/playbooks/{task_type}` round out the HTTP surface, and a
  `prefrontal clarify check|list|resolve|dismiss|guide` CLI mirrors it for
  headless use (the resolve logic is shared via `apply_clarification_answer`, so
  HTTP and CLI can't drift). Declared as the Task Paralysis `clarify_ambiguous`
  intervention and surfaced in its profile section. Covered by
  `tests/test_clarify.py` + `tests/test_clarify_endpoints.py` + `tests/test_cli.py`.
  The registry has since grown to eight task types (tax filing, passport, DMV
  license, vehicle registration, insurance claim, home repair, finding a
  provider, appointments), and guides **localize to the user's home ZIP** when
  opted in: a step's `{area}` token renders as the `home_zip` (seeded to the
  deployment default, back-filled to existing users by the migration ladder)
  once `playbook_localization` is on — off by default, toggled via
  `prefrontal clarify localize on`, degrading to a generic phrase otherwise.
  Free-text answers map to a task type by the **most specific** keyword match, so
  a generic word can't hijack a specific reading.
- **Departure reminders on the coaching tick (toward retiring n8n)** ✅ — the
  `departure_buffer` intervention is now a coach cue: `TimeBlindnessModule.evaluate`
  emits the most-urgent due departure (reusing the same `plan_upcoming_departures`
  / `next_departure` / `build_departure_message` the `/webhooks/departure/check`
  endpoint and the widget's leave-by use), so a single native `prefrontal coach
  --deliver` tick sends "leave by" nudges without n8n polling that endpoint.
  Fire-once and escalation are the engine's job — the `dedup_key`
  (`departure:<id>:<level>`) makes each heads_up→soon→go transition a fresh fire,
  and `go` maps to `critical` so the final "head out now" bypasses quiet hours
  like the endpoint does. The `context_key="departure"` cue carries the signed
  **Made it / Missed it** buttons through the native delivery client. The endpoint
  stays for now (shares nothing that double-fires — the engine owns the coach-side
  dedup), to be deprecated once the tick has run clean. Covered by
  `tests/test_modules.py`. *(That fold toward n8n-free delivery has since landed:
  panic rides the same coach tick, the household sweeps have their own native
  launchd job (`com.prefrontal-household.plist`), and **a launchd `coach --deliver`
  schedule now drives the tick** (`deploy/com.prefrontal-coach.plist` +
  `deploy/coach.sh`, every 60s), replacing the coach-check / hyperfocus-check /
  departure-reminder / panic-check poll workflows in one job. With the native
  Twilio voice call below, the outing 150% escalation is native too — so the nudge
  workflows can all be deactivated; see "Delivery layer".)*
- **Parent pack / shared household sheet** ✅ — the co-parent surface shipped end
  to end (`prefrontal/household.py`, `webhooks/routers/household.py`,
  `memory/repos/household.py`; the `/kids` dashboard + `/family` glance;
  `prefrontal household add|join|leave|show|invite|redeem|star|prompt-check|
  checkin-check|digest-check|balance|shopping|chore|routine|chores-check`). It
  carries: a
  real **household scope** (`households` + `users.household_id`), **facts** &
  **agreements**, **star charts** with goals + dual-parent congratulation and
  scheduled award prompts, a shared **shopping list**, **recurring shared chores**
  (owner reminder + miss-handoff to the other parent), the objective
  **load-balance view**, the daily **delta digest** (push what your co-parent
  changed), an optional weekly **mental-load check-in**, **single-parent** support
  (load-balancing gated), and **self-serve invites** (create/redeem/revoke). n8n
  workflows drive the sweeps (`star-prompt-check`, `checkin-check`, `digest-check`,
  `chores-check`). Full design:
  [`docs/household-sheet.md`](docs/household-sheet.md).
- **Household routines (RACI) + accountability in the balance view** ✅ — chores
  now group under a **routine** (`household_routines`) with exactly one
  **accountable** owner (RACI "A" — the mental-load holder, distinct from the
  chore's "responsible" doer). A routine carries the schedule its chores
  **inherit** (a chore overrides only if it sets its own time; a chore can be
  *untimed* — a checklist item with no clock, `due_time=''`). Crucially, load
  balancing gained a **second facet**: `contribution_counts` now counts chores
  actually **done** (`household_chore_log.done_by`) — not just sheet edits — as the
  *doing* tally, and `accountability_counts` reports the *carrying* tally (enabled
  routines each parent holds). `balance_view(counts, carrying=…)` surfaces both,
  each with its own gentle caption. Endpoints `POST /household/routines`
  (+`/enabled`, `/remove`), `chore.routine_id`, and `prefrontal household routine`.
  Covered by `tests/test_chores.py` + `tests/test_household.py`. The **`/kids`
  dashboard** now surfaces it: a **Shared chores** card (one-tap "done today" that
  logs who did it, add form with owner/routine/time, routine badge + inherited
  schedule), a **Routines** card (accountable owner + add form), and the
  **carrying** facet in the "Sharing the load" panel alongside doing. The sheet
  payload gained a `members` list to populate the owner/accountable pickers.
- **LLM-as-sensor — free text → candidate updates** ✅ — `prefrontal/sensor.py`
  reads a plain note ("I always blow off admin on Mondays") and *proposes*
  allowlisted `coaching_state`/episode candidates that land as **pending**
  `proposals` rows; only a human accept applies them (`source=llm_inferred`).
  `prefrontal note` / `prefrontal proposals list|accept|reject`.
- **Work/life guardrails — per-account domains + time bands** ✅ — a todo now
  carries a life **domain** (`work`/`home`/…) that resolves to a time band and
  **outranks its category**, so a work email that triages as "communication" is
  still held to work hours and never squeezed into a home evening. `resolve_window`
  precedence is now *per-todo override → domain → category → source → default*
  (`prefrontal/scheduling.py`); the band is a **hard** gate (`todo_allowed_at`),
  matching the existing off-zone. Mail ingestion stamps the domain from the
  account: `PREFRONTAL_ACCOUNT_DOMAINS` (e.g. `work=work,personal=home`) maps each
  inbox to a domain, threaded through `ingest_messages`. A new nullable
  `todos.domain` column (created by the idempotent schema + migrate back-fill) with
  `add_todo(domain=…)` / `set_todo_domain`. Off by default (no accounts mapped ⇒
  unchanged scheduling). Covered by `tests/test_todos.py` + `tests/test_mail.py`.
  **Crunch mode** ✅ ships the escape hatch: a self-expiring `crunch_until`
  coaching-state timestamp (`prefrontal crunch on --hours N` / `off` / `status`)
  suspends the per-key bands so anything can surface any waking hour during a
  deadline stretch — the off-zone and travel-late gate still apply. Editing a
  todo's domain is now a first-class surface: `POST /todos/{id}/domain` (declared
  before the `{action}` catch-all) and `prefrontal todo domain <id> [value]`
  (omit the value to clear), both normalizing to lowercase and reusing
  `set_todo_domain`. The **dashboard** exposes it as an outlined pill on each todo
  (distinct from the solid category chip) with a picker — the in-use domains plus
  the canonical work/home, "New…", and "Clear" — and the domain is searchable; the
  **widget** gets it in the `/todos/now` suggestion payload so it can label the pick.
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
  `tests/test_self_care.py`. The **adaptive-cadence learner** ships (see "Learning
  & adaptation" §6): it now folds **unanswered** nudges in too —
  `sweep_unanswered_self_care` logs a nudge left un-acted past a window as an
  `ignored` episode, which the learner reads as a "wrong time / too frequent"
  signal alongside snoozes. A **meds** check now ships as a third basic (one-tap
  Took / Snooze, its own start hour / interval / daily target for multi-dose
  regimens) — **off even when self_care is on**, since medication is personal, so
  it's opt-in per person via `meds_enabled`. A **bio-break** check followed
  (open-ended: a plain interval reminder within a time *window*, bounded by an
  `biobreak_end_hour` rather than a daily quota — no count ever silences it, `Went`
  just defers the next). The **wind-down / sleep** check now rounds out the basics
  pack ✅ — a once-a-day (target 1) *evening* nudge to "start winding down for bed"
  from `winddown_start_hour`≈21, re-asking every `winddown_reask_minutes`≈30 until
  one **Winding down** settles it for the night (`winddown_started`/`winddown_snooze`
  in `NUDGE_ACTIONS`, a 🌙 button in `notify.py`, `winddown` in the delivery
  `_CONTEXT_KIND`/coach `_CUE_ACTION_KIND` maps). Unlike the daytime checks it lives
  right against the responsive-hours edge, and by design **leans on the engine's
  quiet-hours gate** — a wind-down cue outside responsive hours is `suppressed` like
  any other — rather than modelling a bedtime itself, so it never nags into the
  night. **Off even when self_care is on** (a bedtime is a personal preference, like
  meds): opt in via `winddown_enabled`. Covered by `tests/test_self_care.py`.
  *(The "does it broaden into a self-care/basics pack?" question is now moot in
  practice — meal + water + meds + bio-break + wind-down cover the basics; a formal
  `Pack` bundling them with seeded defaults is possible but not needed to use them.)*
- **Encouragement & recovery layer** ✅ — the counterweight to a system that
  nudges: when a day goes rough, shift tone from nudging to reassurance + a plan.
  `prefrontal/encouragement.py`'s deterministic `assess_day()` scores today's
  signals (a missed *hard* commitment is the heaviest at 3.0; an *overwhelmed*
  plate right now — reusing panic's `overwhelm_level()` — also 3.0; each `miss`
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
  `prefrontal/webhooks/notify.py` builds the ntfy `http` action-button specs
  (empty unless a public origin + signing key are set), and the native delivery
  client attaches them to each cue on the `prefrontal coach --deliver` tick — so a
  tap fires `GET /nudge/act` in the background with no app switch. Idempotent per
  button; covered by `tests/test_notify.py` + `tests/test_nudge_act.py`. The old
  "publish to ntfy" n8n node (`deploy/n8n/interactive-nudge-ntfy.workflow.json`)
  was retired once first-class Python publishing landed.
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
  daily-recovery variant as the remaining piece. The three follow-ups have all
  shipped: **quiet-hours gating** ✅ — `evaluate_panic_check(quiet_hours=…)` now
  **defers** (never drops) an overwhelm edge that lands outside responsive hours,
  leaving `last_panic_level` untouched so the first poll back in responsive hours
  still fires; both `/webhooks/panic/check` and the native `coach --deliver` tick
  pass `in_quiet_hours(...)`, so an overwhelm push can no longer land at 3am.
  **First-step outcome capture** ✅ — a fired nudge logs a *pending* `panic`
  episode (`record_panic_step_sent`) that a one-tap "✓ Did it" resolves to
  success (`resolve_panic_step`) and an unanswered one is swept to a miss
  (`sweep_pending_panic_steps`), so the drift pass learns whether the surfaced
  step actually got done. **Encouragement reuse** ✅ — `overwhelm_level()` is now
  one of the triggers for the encouragement layer's `assess_day` detector
  (weighted like a missed-hard commitment) instead of a separate threshold, so a
  plate that's buried *right now* reads rough even before anything is missed.
  Covered by `tests/test_panic.py` + `tests/test_encouragement.py`.
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
  live, all interventions `active`. **auto_decompose** (opt-in, **off by default**
  via `auto_decompose_enabled`) breaks a todo you're *avoiding* into a tiny first
  step on the coaching tick (`sweep_avoided_decompositions`), not at creation, and
  only when the model judges it worth it; **tiny_first_step** reframes a stalled
  task on demand (`POST /todos/{id}/decompose`, also fed to panic and `/todos/now`); and
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
- **In-progress pinning + focus-conflict alert** ✅ — a *started* todo
  (`POST /todos/{id}/start`, cleared by `/unstart`) is pinned to the top of
  `GET /todos` (`sort_todos_for_display`), so the task you're mid-flight on stays
  visible. `focus_conflict()` (`prefrontal/todos.py`) flags when everything you've
  started ranks below an important task you're avoiding-and-haven't-started —
  returned as the `focus_conflict` field on `GET /todos` and rendered as a gentle
  "worth switching?" banner on the dashboard. The honest-prioritization companion
  to avoidance detection.
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
  the `lead_minutes` fallback. A **`prefrontal place`** CLI (`add` / `list`)
  curates aliases offline from the terminal, the twin of `POST` / `GET /places`.
  *(Next: reverse-geocode the iOS location ping for nicer context; self-host
  Nominatim on the mini.)*
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
  Pushover. The **widget now surfaces the leave-by time** for the next
  commitment: a read-only `GET /departure/next` (the side-effect-free companion to
  `POST /webhooks/departure/check` — no dedup, no nudge, safe to poll) plans the
  soonest upcoming commitment and returns its `leave_by`, which the Scriptable
  widget shows as a "leave 4:15 PM · 12m" line under the next commitment (colored
  by departure level, gated to a *today* travel commitment so a leave-by days out
  or an attend-from-desk meeting stays quiet). Both surfaces share a new read-only
  `plan_upcoming_departures` helper. The **morning briefing now surfaces it too** —
  a "🚶 Leave by:" section listing today's remaining travel commitments with their
  leave-by (bias-adjusted travel estimate, or the static lead), planned with the
  same `plan_departure`/`departure_kwargs` the nudge uses so the digest matches
  what it's nudged for; attend-mode, zero-lead, **FYI** (someone else's event),
  and placeholder-hold items are omitted — the same real, own-commitment subset
  the nudge and cascade use via `is_attendable`, so an FYI event never gets a
  "leave by" (or a tight-stretch flag) — gated on the Time Blindness module.
  Covered by `tests/test_departure.py` + `tests/test_briefing.py`. *(Next:
  optional geocoding of free-text `location`; per-commitment travel learning.)*
- **Briefing layout + feedback loop** ✅ — the deterministic digest now leads with
  today and what to act on (schedule → leave-by → risks → opportunities) and keeps
  the gentler look-back (what slipped, focus, balance) down by the closing note,
  with consistent emoji headers and warmer phrasing. Each delivered digest ends
  with a small 👍/👎 "Did this help?" footer (signed one-tap `/nudge/act` links,
  `briefing_helped`/`briefing_not_helped`, riding a synthetic `0` target like the
  self-care checks). The running tally feeds `learned_briefing_guidance` back into
  the LLM briefing prompt — a run of 👎 tightens the voice, a run of 👍 holds its
  shape. Covered by `tests/test_briefing.py` + `tests/test_nudge_act.py`.
- **Pattern-computation pass** ✅ — `prefrontal/memory/patterns.py` derives
  `time_estimation`, `channel_response`, and `drift` patterns from `episodes`
  (confidence = `n/(n+k)`) and recomputes the `time_estimation_bias` multiplier.
  Run via `prefrontal learn` (scheduled nightly — see step 7 above). Both of the
  deferred pieces have now shipped: `context_switch` derivation (from the captured
  per-session `switch` episodes — see §5), and **finer `context_key` bucketing than
  episode type** ✅ — a new **activity** dimension (`compute_bias_by_activity`,
  `time_estimation_bias:activity:<activity>`) buckets `task` estimation pairs by the
  activity read off the episode `context` (`outing` / `focus` / `trip` / …), one
  step below `episode_type`. Every out-of-flow surface logs a `task` episode, so the
  `type:task` multiplier pooled a coffee run ("back in 15" that stretches to 45) with
  a well-estimated focus block; the activity bucket separates them. `resolve_bias`
  gained an `activity` layer (band → energy → category → **activity** → type →
  global), and the outing nudge projection (`location_anchor.evaluate` +
  `/webhooks/outing/check`) now resolves `activity="outing"` so a coffee run
  calibrates against *outing* history, falling back to the global until there's
  enough outing signal. Surfaced in the profile ("By activity: …") and
  `prefrontal learn`; auto half-life derivation and per-context half-life overrides
  cover it like the other dimensions. Covered by `tests/test_patterns.py`.
- **LLM-backed summarizer** ✅ — `summarize_profile()` feeds the structured
  profile to a local Ollama model (`prefrontal/integrations/ollama.py`) and
  returns prioritized coaching prose, falling back to the heuristic when the
  model is down. Run via `prefrontal summarize`, which now **caches** the
  narrative in the `profile_cache` table so `GET /profile` serves the prose
  without a per-request model round-trip (`?refresh=1` regenerates it,
  `?format=structured` returns the raw input; `X-Profile-*` headers report
  source/model/age/staleness). The summarizer is now one of the agents that can
  opt into the **Anthropic provider** (`ANTHROPIC_AGENTS=summarizer`) for
  higher-quality prose, with Ollama as the fallback — see below.
- **Calendar ingestion + double-booking** ✅ — `commitments` table +
  `prefrontal/commitments.py`: feed-aware calendar sync, manual add,
  `GET /commitments`, and overlap detection at `GET /commitments/conflicts`.
  Per-user **private ICS feeds** now sync natively (`prefrontal/ics.py`,
  `prefrontal calendar add-source|sync --all-users`, launchd `com.prefrontal-calendar`
  every 15 min) — the no-n8n path that *replaces* the old n8n `calendar-sync`
  workflow (deactivate it to avoid double-ingestion). The `/webhooks/calendar/sync`
  endpoint remains for batch/n8n callers.
- **Impact analysis + cascade** ✅ — `prefrontal/impact.py`: projects realistic
  free-time from the `time_estimation_bias` and flags upcoming commitments now at
  risk (`start_at − lead_minutes` vs projection). `cascade_impact()` then
  propagates the overrun *through* the chain — a late finish carries forward
  through each commitment's own length, so a meeting two hops down is flagged when
  the delay reaches it, with `delay_minutes`/`projected_start`/`caused_by` naming
  the upstream domino; the chain self-heals when a gap absorbs the slip. Surfaced
  in `/webhooks/outing/check` (an `impact` list + `hard_conflict` flag, message
  tail "This cascades: 'A' → 'B' → 'C'") and, beyond outings, at
  `GET /impact/cascade` (queryable from any free-time via `free_at`/`over_minutes`,
  else the active outing, else now). The **dashboard** renders the domino strip
  live and a "running behind" scrubber (`over_minutes`) to pre-project it, and the
  **morning briefing** runs `fragile_stretch()` — today's remaining commitments
  cascaded under the learned bias-inflated durations — to preview the tightest
  back-to-back stretch ("⏳ Tight stretch: if today runs long, A → B") before it
  slips, staying silent on a day with slack or when the bias shows no overrun.
  **Panic mode** (`GET /panic`) adds a "⚠️ Knock-on" line — the cascade seeded at
  now over upcoming commitments — so when you're already late it names the
  downstream chain that topples too, not just the first fire (shown only when two
  or more downstream commitments are at risk). The **iOS widget**
  (`deploy/scriptable/prefrontal-widget.js`) mirrors it: a "running behind" Lock
  Screen facet (`behind` param) and a home-screen line, gated the same way.
  **Travel-aware leads**: when a location is known, `GET /impact/cascade` replaces
  each leg's static `lead_minutes` with real bias-adjusted travel between commitment
  coordinates (`departure.travel_leads` → `cascade_impact(lead_override=…)`), so a
  leg you can't actually drive in the flat buffer is flagged; `travel_aware` reports
  when it applied. *(Next: thread the same override into the outing/panic surfaces.)*
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
  Energy-aware fitting shipped in the `/todos/now` picker (above).
  **Auto-scheduling** shipped: `POST /todos/{id}/schedule` blocks time for a todo
  as a `manual` commitment — the block length is the bias-adjusted estimate (or an
  explicit `minutes`), placed at an explicit `at` or the earliest fitting free
  window today (`first_window_fitting` over `free_windows`, within waking hours),
  turning a "good for: X" suggestion into a real hold. **Multi-suggestion per
  window** shipped too: `suggest_for_windows(options_per_window=N)` returns a menu
  per gap (the reserved primary + advisory alternatives), and the briefing's spare
  section shows them ("good for: X _(or: Y, Z)_").

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
   agent (`deploy/com.prefrontal.plist`). Confirm `GET /health`.
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
   via `deploy/learn.sh` + `deploy/com.prefrontal-learn.plist`
   (launchd `StartCalendarInterval`, 03:30); see deployment §12. Load it and the
   profile recalibrates on its own.

Everything above the dry run is configuration; no further code is required for
the first test. Code follow-ups below are optional polish.

