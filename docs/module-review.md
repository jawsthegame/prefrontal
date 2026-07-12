# Per-Module Review

*A functional walkthrough of every module in the `prefrontal` package: what each one does, and
at least one concrete, code-grounded improvement for each. Compiled from a module-by-module pass
over the tree as of the `claude/architectural-review-ghkh3a` branch. File references are
`path:line`. This complements the structural view in [`architecture-review.md`](architecture-review.md);
here the lens is per-unit functionality and specific, actionable fixes.*

Legend for suggestion flavor: 🐛 correctness/edge-case · 🧹 simplification · ⚡ performance ·
🛡️ robustness/security · 🎯 UX · 🧪 test gap.

---

## 1. Challenge-area modules & the coaching engine

### `modules/base.py`
The `Module` ABC + `Intervention` dataclass that form the plug-in contract: metadata
(`key`/`title`/`challenge`), seeded `default_state`, lifecycle hooks (`evaluate`/`before_collect`/
`after_fire`), the protection flags, and `channel_targets`. `seed()` upserts only absent keys so
enabling a module never clobbers preferences.
- 🐛 `Intervention.status` (`base.py:61`) is a free-form `str`; only `planned`/`active` are
  recognized, so a typo (`"activ"`) silently misreports in `prefrontal modules -v`. Constrain to
  `Literal["planned","active"]` or validate in `__post_init__`.
- 🧪 Nothing enforces the docstring's promise that `Intervention.name` is unique within a module,
  yet `dedup_key`s build on it — add a registry-time uniqueness check.

### `modules/registry.py`
Single source of truth for which modules exist / are enabled; `register()` guards empty/duplicate
keys, `enabled_modules()` treats empty config as "all on" plus pack-enabled keys.
- 🧹 `enabled_modules` (`registry.py:80`) and `is_enabled` (`registry.py:115`) each independently
  lazy-import and fold in `pack_module_keys` — extract one `_enabled_keys(settings)` helper both call.
- 🎯 `get()` (`registry.py:56`) raises a bare `KeyError` while `register()` raises a descriptive
  `ValueError`; give `get()` a `KeyError(f"No module registered: {key!r}")` for parity.

### `modules/time_blindness.py`
Turns learned `time_estimation_bias` into departure buffers and owns both bookends of a "be
somewhere" obligation: morning-of `departure_buffer` (heads_up→soon→go) and night-before
`morning_prep` ("set an alarm"), plus opt-in elapsed-time callouts.
- 🐛 The suggested wake time is `leave_by - morning_routine_minutes` formatted `%H:%M`
  (`time_blindness.py:358`); for an early leave-by it can wrap to the previous evening ("23:30")
  and the alarm button reads as before "tonight." Guard against `wake_at` crossing the local date.
- 🧪 No handling/coverage for `morning_routine_minutes` exceeding the leave-by (negative offset).

### `modules/task_paralysis.py`
Activation-energy reducers: `refocus`, `tiny_first_step`, `body_double_nudge`, `clarify_ambiguous`,
`auto_decompose` — emitting one sharpest nudge per tick.
- 🐛 `profile_section` computes stall rate over `episodes_by_type("task")` (`task_paralysis.py:263`),
  but that type also holds focus-block and outing closes — conflating them badly misstates
  "task initiation friction: N/M." Filter to todo-derived episodes (reuse `_task_title(...) is not None`).

### `modules/hyperfocus.py`
The most substantial module: protects aligned deep work, interrupts misdirected hyperfocus via pure
`focus_level`/`should_protect`; one-tap / calendar-armed / proactive start paths; `evaluate`/
`after_fire` split read from writes and share `evaluate_focus_session` with the endpoint.
- 🧹 `should_suggest_focus` (`hyperfocus.py:306`) and `should_recap_focus` (`:334`) are byte-identical
  bodies — collapse into one `_in_hour_band(...)`.
- 🐛 `_focused_today` scans only `recent_focus_sessions(limit=20)` (`:881`); a heavy day of short
  sessions can push the morning's first past the window and wrongly re-offer a start/recap. Filter
  by date in the query, not a fixed row cap.

### `modules/impulsivity.py`
Friction between impulse and action: distance-scaled `reflective_pause`, `capture_and_defer`
(LLM-titled, heuristic fallback), proactive `drift_check`.
- 🧹 `pause_seconds` (`impulsivity.py:64`): `frac` is already clamped to `[0,1]`, so
  `factor = 3.0 - 2.0*frac` is provably in `[1,3]` — the outer `min(max(...))` is dead code.
- 🐛 The LLM title path truncates to 8 words with no ellipsis (`:244`) while the heuristic appends
  `…` (`:213`) — inconsistent titles depending on model availability. Add the `…`.

### `modules/location_anchor.py`
The "coffee-shop nudge": elapsed-time escalation soft→firm→call against a stated/inferred window,
with a `MIN_ESCALATION_WINDOW_MINUTES` floor; confirms homecomings (arrival prompt + backdated
auto-return) and a travel-aware commitment cascade.
- ⚡ `_active_outing_evals` (`location_anchor.py:614`) runs `upcoming_commitments()` + `travel_leads`
  (routing math over every commitment) and is called by **both** `evaluate` and `after_fire` each
  tick — so the full evaluation runs twice per tick. Compute once and stash on `ctx`/instance keyed
  by `ctx.now`.
- 🎯 `build_message` greets by name for `soft` and `call` but not `firm` (`:167`) — add it for a
  consistent voice mid-escalation.

### `modules/trip_tracking.py`
Passive companion to Location Anchor: label-ask for auto-detected round trips, plain-English
reflection capture, opt-in weekly `focus_balance` nudge, household `away_proposal`.
- 🐛 The focus-balance cue dedups on `ctx.now.isocalendar()` (`trip_tracking.py:186`), but `ctx.now`
  is naive UTC — the ISO-week boundary is computed in UTC, so a user west of UTC can get the "new
  week" nudge early/late and the dedup key can disagree with their local week. Use
  `local_datetime(ctx.now, ctx.timezone)`.

### `modules/delegation_checkin.py`
Re-surfaces parked delegations on an item-dependent cadence, self-gating on the engine's
`last_fired`, maturing each into an engagement outcome.
- 🐛 The engagement verdict treats *any* `updated_at > fired` as "engaged" (`delegation_checkin.py:123`);
  an incidental row edit inflates the "check-ins land X/N" stat. Tie it to a real signal (status
  stepping forward, or a matched reply).
- 🧹 `overdue = 999.0` (`:88`) is a magic "never fired" sentinel — name it.

### `modules/projects.py`
Re-surfaces an active project whose open work is untouched past `project_stale_days` (default 21),
one per tick, at most once per `RENUDGE_DAYS`.
- ⚡ `stale_project_candidates()` is queried in `evaluate` (`projects.py:84`), `before_collect`
  (`:124`), and `profile_section` — 2–3 full scans per tick for the same data. Fetch once per tick.
- 🐛 `days = (ctx.now - last).days` (`:88`) integer-floors age, so a 21.9-day-quiet project reads as
  21 and fires a day "late" vs the documented cutoff. Compare on `total_seconds()/86400` if
  day-precision matters.

### `coaching.py`
The pure decision core: OR-s module protection into `focus_protected`, runs hooks, collects cues,
applies suppression → channel choice → optional LLM phrasing, records fires, and owns the
channel-outcome + engagement learning loops, isolating every module-facing call.
- ⚡ Several full `store.all_state()` scans per tick — `sweep_stale_nudges` (`coaching.py:571`) plus
  one `sweep_engagement` scan per module (`:652`). As `coach_*` keys accumulate this is O(state)
  repeated; add a prefix-scan store API (`state_items_with_prefix("coach_engage:")`).
- ⚡ `choose_channel` calls `store.get_patterns("channel_response")` for every cue inside the
  `decide` loop (`:356`) — fetch the pattern map once and pass it in.

### `nudges.py`
The domain half of `GET /nudge/act`: dispatches ~18 one-tap actions to the same close/record helpers
their endpoints use, resolving the channel ack up front, idempotent per branch.
- 🐛 The trailing `made_it`/`missed_it` block (`nudges.py:260`) is an **unguarded else** — any
  unrecognized action falls through and logs a spurious `departure` episode + `dismiss_departure`.
  Add an explicit `if action in ("made_it","missed_it")` guard and a safe "unknown action" fallback;
  validating the action before `resolve_ack` (`:86`) fixes this and the premature-ack issue together.

---

## 2. LLM-facing domain

### `assistant.py`
Natural-language editor behind the dashboard chat: builds a per-user snapshot, asks the model for a
JSON action list, runs each op through the `_VALIDATORS` registry (which doubles as the `ALLOWED_OPS`
whitelist), previews, then executes against a scoped store.
- 🐛 `add_commitment` never checks `end_at > start_at` (`_v_add_commitment`, `assistant.py:875`),
  unlike `_v_set_away` which rejects an inverted range (`:1205`). An inverted range passes preview
  and corrupts conflict/departure math — add the ordering check after parsing.
- 🐛 Date fields are validated for non-blankness but not shape at preview time (`:744`,`:814`,`:875`),
  so "next Friday" renders into the "Apply?" preview and only no-ops at execute. Reuse `_as_iso_date`
  so bad dates drop with a reason before preview.

### `briefing.py`
Assembles the deterministic morning digest and optionally rewrites it as LLM prose with a 👍/👎 loop.
- 🐛 The 👍/👎 tally never decays (`learned_briefing_guidance`, `briefing.py:117`) — 15 early
  thumbs-downs need 15+ thumbs-ups to recover long after the prose improved. Use a rolling window / decay.
- 🧹 `time_estimation_bias` is parsed from state twice in one build (`:250`, `:294`) — hoist once.

### `panic.py`
Overwhelm triage: scores calendar/todo/mail pressures into late/soon/piling-up buckets, picks one
first step, computes a cascade; also owns proactive alerting with edge-triggering + cooldown.
- 🧹 `_todo_pressures` returns a `{todo_id: account}` map no caller uses (`panic.py:456` discards it) —
  drop the tuple return.
- 🐛 Two "already started, hard" commitments tie at a flat `1100.0` (`:210`) so the "single worst
  thing" is picked by sort-stability, not by which you're further into. Scale by minutes-since-start.

### `encouragement.py`
Rough-day tone shift: `assess_day` scores today's signals into a `rough` verdict; `build_recovery`
turns it into re-fit/defer/first-step; `briefing_note` produces the brief's closing line.
- 🐛 `SIGNAL_WEIGHTS` is authoritative for only one of its four entries — `assess_day` hardcodes
  `3.0`/`1.0`/`0.5` (`:210`,`:214`,`:223`) and only reads the dict for `overwhelmed` (`:251`). Tuning
  `SIGNAL_WEIGHTS["conflict"]` does nothing today. Read all four from the dict.
- ⚡ `assess_day` calls `build_panic(...)` just to read `overwhelm_level` (`:243`), and briefings run
  `build_briefing → briefing_note → assess_day` — so every briefing runs a full panic triage. Pass an
  already-built `PanicPlan` in, or memoize per `(store, now)`.

### `clarify.py`
Ambiguity detection + guided playbooks: a pure `ambiguity_score` gates an optional LLM pass proposing
one clarifying question; recognized readings map to a static `PLAYBOOKS` registry with `{area}`
localization.
- 🐛 The LLM path can lose a playbook the offline path guarantees: for "tax", the heuristic pulls
  curated `(label, task_type)` pairs (`clarify.py:287`), but the LLM readings only get a `task_type`
  via keyword match, so "pay my property tax" maps to nothing and yields a plain disambiguation with
  no guide. Seed/merge `_TOKEN_INTERPRETATIONS` into the LLM candidate for a recognized token.

### `sensor.py`
LLM-as-sensor: proposes *pending* allowlisted structured updates from a note; nothing writes until a
human accepts.
- 🛡️ The allowlist gates the *key* but not the *value* (`_validate`, `sensor.py:224`; `apply_proposal`
  writes `str(value)`, `:491`). A model proposing `responsive_hours_start = "morning"` or
  `preferred_briefing_format = "medium"` can, on one accept, poison state that downstream parses with
  `int()`/equality. Add a per-key value validator keyed off `PROPOSABLE_STATE_KEYS`.

### `classify.py`
Classifies a commitment title `self`/`child`/`fyi`: deterministic roster pass, then local LLM with
learned few-shot corrections, conservative `self` fallback.
- 🐛 `parse_kind_reply` uses substring `find` (`classify.py:97`), so `"self"` matches inside
  "yourself" and `"child"` inside "children" — *"you'd attend yourself — it's FYI"* misclassifies as
  `self`. Match on word boundaries (`re.search(rf"\b{kind}\b", ...)`), as `roster_child_match` does.

### `triage.py`
Source-agnostic classify/route: deterministic heuristic escalating to one JSON LLM call only below
`HEURISTIC_TRUST` (0.75), then idempotent routed side effects + `triage_log`.
- 🐛 Noise keywords collide with real business language: `_NOISE_WORDS` has bare `"sale"`/`"deal"`
  (`triage.py:74`) and a marketing hit scores `0.78 ≥ HEURISTIC_TRUST`, so *"review the sale agreement
  by Friday"* is classified `noise` and dropped, never reaching the model. Lower the marketing-cue
  confidence below the trust floor, or require co-occurrence with a bulk-sender signal.
- 🧹 `_when_from_text` is called twice per pass (`:238`,`:239`) — compute `when` once.

---

## 3. Task / time / location domain

### `todos.py`
Enriches a bare title into schedulable fields + decomposition, avoidance scoring, and closed-todo
episode capture; model-first with heuristic fallback.
- 🐛 `heuristic_deadline` (`todos.py:270`) has a bare-weekday fallback regex with no `by/on/before/due`
  prefix, so "review **Monday**'s notes" sets a real deadline. Gate the second regex on a leading/prefixed position.
- 🐛 `avoidance_score` (`:794`) reads `estimate <= 30` without a type guard, unlike `category_stats`
  (`:956`); a malformed row raises `TypeError`. Add the `isinstance` guard.

### `scheduling.py`
Finds free windows between commitments and ranks which open todos fit, applying learned bias and
per-category windows.
- 🐛 `work_window_now` (`scheduling.py:231`) parses `HH:MM` with a bare `int(...split(":"))` and no
  guard, so a malformed coaching-state override 500s the "what fits now" widget — route through the
  defensive `parse_window` and fall back to defaults.
- ⚡ `find_slots` (`:190`) re-runs `free_windows` over the entire commitment list for each day in the
  scan (O(days × commitments)); pre-bucket commitments by local day first.

### `commitments.py`
Calendar ingestion: UTC normalization (`to_utc`), RRULE expansion, cross-feed dedupe, idempotent
sync, conflict detection.
- 🛡️ `_expand_master` (`commitments.py:600`) materializes `list(rule.between(...))` with no occurrence
  cap — a `FREQ=MINUTELY`/`SECONDLY` feed over the 30-day horizon is an unbounded-memory DoS. Add a
  hard count cap alongside the malformed-RRULE guard.
- 🐛 `dedupe_events` (`:446`) collapses on title+start+end, so two genuinely distinct same-titled
  events at the same time (two kids' "Swim lesson") merge into one. Fold `location`/a UID fragment in
  if that's a real scenario.

### `departure.py`
"You're home, time to leave": travel time from haversine × road-factor ÷ speed, padded by learned
bias, escalating heads_up→soon→go.
- 🐛 `estimate_travel_minutes` (`departure.py:118`) uses one flat `speed_kmh` for all distances, so a
  300 m walk and a 40 km leg are both scored at 30 km/h. A crude distance-banded speed markedly
  improves leave-by accuracy without a routing API.
- 🐛 `travel_leads` (`:154`) advances through *all* passed commitments with no `is_attendable` filter,
  so FYI/placeholder events (which some callers don't pre-filter) distort the leg geometry. Filter
  inside `travel_leads`.

### `impact.py`
Turns projected free-time into the downstream domino chain of at-risk commitments; `fragile_stretch`
is the bias-inflated morning variant.
- 🐛 `cascade_impact` (`impact.py:139`) assumes strictly sequential attendance, so two genuinely
  *overlapping* commitments (a real double-booking) get serialized — the first's full duration stacks
  before the second, inventing a phantom delay. Detect overlap (reuse `find_conflicts`) and surface it
  as a conflict instead.

### `focus_balance.py`
Life-domain (shop/work/home/kids/personal) weekly rollup over passive trips + returned outings, vs
optional per-domain targets, to drive a gentle "light on kids" nudge.
- 🐛 The `normalize_focus_domain` docstring example is wrong (`focus_balance.py:150` says
  `"kids" → "home"`; `kids` is canonical → `kids`). Fix to a real synonym example (`"childcare" → "kids"`).
- 🎯 `infer_domain_from_text` returns `None` when two distinct domains appear, so "shop for the kids"
  silently drops to unassigned. Add a precedence order (prefer a protect-sphere) so common
  multi-signal intentions still land.

### `trips.py`
Passive closed-loop trip tracking: a home-radius state machine over location pings, plus reflection
classification (LLM + keyword fallback).
- 🐛 `heuristic_reflection_outcome` (`trips.py:127`) does bare substring matching — `"late"` matches
  "re**late**d", `"fast"` matches "break**fast**", `"good"` matches "**good**bye" — so "we related
  well, breakfast after" could score a MISS. Switch to word-boundary matching (as `todos._matches` does).

### `projects.py`
Suggests which opt-in project an incoming item belongs to (LLM + token-overlap heuristic, high
confidence gate).
- 🐛 `heuristic_project` (`projects.py:67`) normalizes confidence by the *project's* token count, so a
  one-word project description scores 1.0 on a single incidental match while a rich description is
  penalized — inverting the intent. Require ≥2 overlapping tokens or a length-aware score.

### `delegation.py`
Hands a todo to a pluggable handler (`agent` = local brief; `email` = VA over SMTP), with check-in
cadence and inbound-reply loop closing.
- 🐛 `AgentHandler.run` detects the offline heuristic via the substring `"Generated offline" in brief`
  (`delegation.py:559`), but `generate_prep`'s prose-salvage path (`:479`) can return a model brief that
  echoes that phrase — mislabeling it. Return an explicit flag from `generate_prep` instead of
  string-sniffing.
- 🐛 `match_delegated_reply` (`:197`) compares the sender against the raw stored `destination`, so a VA
  entered as `"Sam <sam@x.com>"` never equals the bare `sender_email` and the loop never closes.
  Parse/normalize both sides.

### `geocode.py`
Resolves a commitment's free-text location via curated aliases → cache → opt-in Nominatim.
- 🛡️ `enrich_commitments` (`geocode.py:170`) can fire up to 25 back-to-back network geocodes per sync
  with no throttling; Nominatim's policy mandates ≤1 req/sec. Add an inter-call delay (or a small
  per-pass network budget) on the network path.
- 🐛 A persistently *erroring* target is deliberately not cached (`:137`), so it reappears every sync
  and consumes the budget ahead of resolvable ones. Add a short-lived negative backoff for errors,
  distinct from the permanent `not_found` miss.

### `ics.py`
Dependency-free ICS fetch + parse (line unfolding, TZID, RRULE/EXDATE/RECURRENCE-ID), dropping
declined/cancelled events.
- 🛡️ `fetch_ics` (`ics.py:31`) reads the whole response with no size cap and `parse_ics` builds an
  unbounded list — a hostile/runaway feed can exhaust memory. Cap fetched bytes and/or VEVENT count.
- 🐛 `_ics_date` (`:43`) uses `search` not `match`, so it latches onto the first date-looking substring
  anywhere in a value. Anchor the regex / `fullmatch` the token.

### `service_shifts.py`
A 21-line vestige: only `monday_of(date_str)` remains (the municipality-schedule scrape was removed).
- 🐛 `monday_of` (`service_shifts.py:20`) calls `strptime` with no error handling, raising a bare
  `ValueError` to the CLI on a bad date. Wrap with a clear message; add the trivial unit test
  (including the already-Monday case). Consider folding this lone helper into `clock`.

---

## 4. Memory layer

### `memory/store.py`
The `MemoryStore` façade over ~17 repo mixins; owns single- or per-thread connections and enforces
multi-tenancy structurally (`scoped()`/`_uid()`).
- 🐛 `provision_user → seed_user_state` (`store.py:308`) writes ~15+ defaults, each its own commit; a
  crash mid-seed leaves a half-provisioned user the absent-only guard then treats as "already seeded."
  Wrap the seed in one transaction.
- 🐛 `close()` (`:271`) empties `_conns_by_thread` but never clears `self._local.conn`, so a later
  `conn` access on the same thread returns a *closed* connection. Reset the thread-local on close.

### `memory/db.py`
Thin connection/init: `sqlite3.Row`, `foreign_keys=ON`, WAL, 5 s busy timeout; `init_db` runs the
migration ladder then `schema.sql`.
- 🐛 `~`-expansion bug: `connect()` creates the parent dir at the *expanded* path (`db.py:44`) but
  hands the **raw** `db_path` to `sqlite3.connect` (`:51`). A `~/prefrontal.db` opens a literal `~`
  under CWD. Pass the expanded path to `sqlite3.connect` too.

### `memory/migrate.py`
The single ordered migration ladder: legacy→multi-tenant rebuild, then schema-diff column backfill,
coaching-state backfill, project-rank backfill.
- 🐛 `backfill_added_columns` will hard-crash on a `NOT NULL DEFAULT CURRENT_TIMESTAMP` column — the
  docstring documents the SQLite limitation (`migrate.py:112`) but nothing enforces it, so the illegal
  `ALTER` (`:123`) throws on every existing DB. Detect a non-constant default and skip/fail with an
  actionable message.
- ⚡ Steps 2–4 re-diff every table on every boot (only step 1 is version-gated). Gate them behind a
  bumped `user_version` once applied.

### `memory/_helpers.py`
Token hashing, `DEFAULT_COACHING_STATE`, feed label/slug parsing, safe deep-link builders.
- 🛡️ `DEFAULT_COACHING_STATE` hardcodes `("home_zip", "19027", …)` (`_helpers.py:50`) — a specific ZIP
  seeded into every user on every deployment. Move to a `Settings`-configurable default.
- 🧹 `feed_label`/`feed_slug` re-implement the same `":" in external_id` split — have `feed_label`
  call `feed_slug`.

### `memory/patterns.py`
The learning engine: episodes → derived patterns + coaching multipliers, with recency decay,
confidence, per-context bias, walk-forward calibration, and auto-derived half-lives.
- 🐛 Inconsistent "is this an estimation pair" test: `_is_estimation_pair` uses
  `bool(predicted_value)` (`patterns.py:397`, excludes 0) while `_time_estimation_patterns` uses
  `is not None` (`:1298`) — the table row and the bias include different sets for a zero prediction.
  Pick one (`is not None`).
- 🐛 `derive_half_life` sorts by `str(timestamp)` (`:985`), unlike every sibling that uses `_parse_ts`;
  empty/odd timestamps sort to the front and mis-split older/recent halves. Sort via `_parse_ts`.

### `memory/summarizer.py`
Deterministic `build_profile` → LLM `summarize_profile` (fallback on failure), cached in
`profile_cache`.
- 🧹 `structured_hash` is dead weight: `set_profile_cache` stores a SHA-256 (`state.py:298`) the schema
  says exists to detect staleness cheaply, but `cache_is_stale` (`summarizer.py:374`) does a full
  string compare and ignores it. Compare the hash or drop the column.
- 🛡️ No length bound on model output (`:291`) — a pathological multi-KB reply is cached and prepended
  to every agent prompt.

### `memory/schema.sql`
The canonical executable schema: users/households, the three learning tables, and per-module feature
tables, with thoughtful partial-unique indexes and provenance comments.
- ⚡ Missing composite index for type-scoped episode reads: `pending_episodes`/`episodes_by_type`
  filter on `user_id AND episode_type` ordered by `timestamp`, but no index serves that. Add
  `idx_episodes_user_type(user_id, episode_type, timestamp)`; the global `idx_episodes_type` is
  near-useless multi-tenant.
- 🛡️ `episodes`/`nudges` grow forever and `recompute_patterns` loads the full history each pass; no
  retention story, and `dismissed_departures` rows are never cleaned when their commitment is
  cancelled. Add a retention sweep (or document the growth).

### `memory/repos/*`
Per-domain SQL mixins over a shared `Repo` base (`_query_all`/`_query_one`/`_upsert_returning_id`).
- 🐛 `_base._upsert_returning_id` returns `0` when the reselect finds nothing (`_base.py:60`) — an
  impossible-but-silent id that masks a broken upsert. Raise instead.
- 🐛 `users.get_user_by_token_hash` (`users.py:91`) `SELECT * FROM users` + Python loop on **every**
  authenticated request, despite `idx_users_token` and a docstring claiming an indexed lookup. Use
  `WHERE token_hash = ?` — O(1) not O(users).
- ⚡ `episodes.all_episodes()` (`episodes.py:189`) loads full per-user history every learning run —
  accept a `since` bound so the sliding window pushes the cutoff into SQL.
- 🐛 `trips.open_trip` (`trips.py:25`) has no guard against opening a second active trip though the
  docstring asserts only one is open — add a guard / partial unique index `WHERE status='active'`.
- 🐛 `schedule` clock injection is inconsistent: `upcoming_commitments` takes an injectable `now`
  (`schedule.py:143`) but `previous_commitments`/`commitments_needing_geocode`/`cancel_missing_calendar`
  hardcode `datetime('now')`. Thread `now` through.
- 🐛 `projects.project_name_in_use` guards case-*insensitively* (`LOWER(name)`, `projects.py:188`) but
  the DB unique index is case-*sensitive* (`schema.sql:225`) — two racing "Kitchen"/"kitchen" both
  persist. Make the index `COLLATE NOCASE`.
- 🛡️ Idempotency is pushed onto callers in `mail.record_mail` (`mail.py:68`), `triage.log_triage`
  (`triage.py:48`), and `clarifications.add_clarification` (`clarifications.py:51`) — all raise
  `IntegrityError` on a concurrent re-delivery despite having unique indexes. Use `ON CONFLICT DO
  NOTHING/UPDATE`.
- 🐛 `household.redeem_invite` (`household.py:1499`) / `create_own_household` (`:1428`) have a TOCTOU
  race — read-then-update lets two concurrent redemptions both win. Make it a conditional
  `UPDATE … WHERE redeemed_by IS NULL` and treat `rowcount==0` as "already used."
- 🐛 `search` fetches each type `ORDER BY recency LIMIT 200` *before* relevance ranking (`search.py:34`),
  so the exact-title match you're hunting is never scored if it's not in the 200 most-recent rows —
  exactly the "done todo you half-remember" case. Push a cheap relevance pre-filter into SQL.
- 🐛 `todos.set_decomposition` uses `INSERT OR REPLACE` (`todos.py:381`), which resets `created_at`;
  use `ON CONFLICT DO UPDATE` preserving it.
- 🛡️ `proposals.set_proposal_status` (`proposals.py:88`) accepts any status string — validate against
  `{accepted, rejected}`.
- 🧹 `nudges.py:108` ends with an orphaned comment about `todo_decompositions` scoping whose code lives
  in `todos.py` — delete it. Docstrings in `sources.py` say `kind` is `gcal`, but schema/onboarding use
  `ics` (`sources.py:17`) — align.

---

## 5. Web layer

### `webhooks/app.py`
The FastAPI factory: builds shared clients, a `ProviderResolver` and `RouterServices`, a per-thread
`MemoryStore` lifespan, and includes 16 routers. A module-level `app = create_app()` is the ASGI target.
- 🛡️ `app = create_app()` at import (`app.py:217`) constructs every client, so one raising on bad
  settings becomes an `ImportError` that breaks unrelated imports of the package. Guard/lazy-init.
- 🧪 Injected-client aliasing weakens tests: with `ollama` injected, `summarizer_client = ollama`
  (`:89`), so the distinct longer-timeout summarizer path is never exercised. Allow a separate
  `summarizer` injection.

### `webhooks/deps.py`
Shared request auth: `resolve_user` (token → Google cookie → default user → legacy secret) returns a
store pre-scoped to the user; `require_operator`/`require_member` layer guards.
- 🐛 Bootstrap-operator selection is order-dependent — the legacy secret maps to `list_users()`'s first
  active operator with no ordering (`deps.py:93`). Pick deterministically (oldest id).
- 🛡️ No rate limiting on repeated bad-token attempts (`:86`) on a LAN-exposed deployment; and the
  distinct "missing" vs "invalid" 401 messages leak token presence.

### `webhooks/services.py`
The frozen `RouterServices` bundle built once and handed to every `build_router`.
- 🧹 `run_geocode` is typed `Callable[..., dict[str,int]]` (`services.py:49`), erasing the real
  keyword signature — a `Protocol` documents and type-checks the contract routers call.

### `webhooks/helpers.py`
Shared formatters + URL builders (read-backs, the n8n delivery block, signed dismiss links, ntfy
button wrappers).
- 🧹 Orphaned trailing `#:`-docstring for `INFER_TIMEOUT_SECONDS` (`helpers.py:201`) with no constant —
  moved to `_common.py`; delete.
- 🐛 `_fmt_minutes` (`:25`) doesn't guard non-finite input — `inf` surfaces as `"inf min"` in a
  read-back. Add a finite check.

### `webhooks/_common.py`
Holds the shortcut-action→outcome map and the self-contained HTML page shells, read and folded at
import.
- 🛡️ All assets read at import with no error handling (`_common.py:29`+) — a missing `.css`/`.js`/`.png`
  becomes a hard `FileNotFoundError` at process start (and any package import). Wrap/lazy-load.
- 🛡️ Inlining precludes a `Content-Security-Policy` that forbids inline script, on pages that render
  authenticated user data — worth a documented note.

### `webhooks/notify.py`
Pure builder of ntfy one-tap action-button dicts per nudge kind; assembles signed `GET /nudge/act`
buttons.
- 🐛 `nudge_actions` (`notify.py:213`) never caps buttons at ntfy's max of 3 (`trip_label_actions`
  correctly slices `[:3]`, `:241`) — works by luck today; a 4-button kind silently produces an invalid
  payload. Add `[:3]`.
- 🛡️ `_NUDGE_BUTTONS` actions and the oauth `NUDGE_ACTIONS` allowlist must stay in sync by hand — add
  an import-time `all(a in NUDGE_ACTIONS ...)` check.

### `webhooks/oauth.py`
Browser Google sign-in beside token auth: HMAC-signed self-expiring tokens back the session cookie,
CSRF `state`, and one-tap dismiss/act links.
- 🛡️ Logout is client-side only (`oauth.py:346`); the 30-day HMAC session is stateless, so a leaked
  cookie stays valid for its full TTL and the only kill-switch is rotating `session_secret` (nuking
  all sessions). A server-side session version/nonce enables real per-user revocation.
- 🐛 OAuth silently self-disables when `session_secret` is unset but sign-in is "enabled" — every
  callback 400s opaquely (`:300`,`:60`). Validate at startup and fail loudly.
- 🐛 A 200 userinfo response with a non-JSON body makes `.json()` raise `ValueError` outside the
  `except httpx.HTTPError` (`:266`,`:272`) and 500s the callback. Catch `ValueError` too.

### `webhooks/routers/*`
Sixteen per-tag `build_router(services)` factories. Common threads below; each router gets ≥1 fix.
- **schedule** — 🐛 `commitments_list` silently clamps an out-of-range `limit` (`schedule.py:498`)
  while sibling `calendar_slots` 422s; align. 🐛 `impact_cascade` doesn't guard `over_minutes` for
  finiteness (`:663`) — `nan`/`inf` flows into `timedelta`.
- **todos** — 🐛 the background delegation thread uses a stale `todo` snapshot from the request thread
  (`todos.py:877`); re-fetch inside `_bg()` and bail if gone. 🐛 `todo_schedule`/`todos_fit` don't
  bound `minutes` (`:613`,`:982`).
- **household** — 🐛 `set_prompt`/`set_tiers` (`household.py:438`,`:476`) read-modify-write the star
  blob and clobber a concurrent `award_stars`; merge server-side. 🐛 `add_appointment` (`:918`) writes
  no synthetic `external_id`, so a later sync of the same event double-books.
- **ingestion** — 🐛 the documented "clear domain" case (`domain=""`) is unreachable in `trip_retro`
  (`ingestion.py:468`) — the guard rejects it, unlike `/webhooks/trip/domain`. 🐛 docstring says triage
  is local Ollama but it's routed through `provider.client("triage")` (`:559`).
- **anchor** — 🧹 `nudge_dismiss`/`nudge_act` duplicate the verify→get_user→scoped block verbatim
  (`anchor.py:367`,`:421`) — factor a `_resolve_signed(...)`. 🎯 `nudges_list` docstring points at the
  deprecated `/webhooks/outing/check`.
- **assistant** — 🐛 docstring promises a 503 the handler never raises (`assistant.py:58`) — a provider
  raise becomes a 500. Implement or fix the doc.
- **clarify** — 🛡️ `set_localization` stores an unvalidated ZIP (`clarify.py:128`); validate a 5-digit
  US ZIP. 🎯 `clarification_resolve` 404s an already-resolved row (`:178`) — a 409 distinguishes
  double-submit from a bad id.
- **coaching** — 🧹 every cue hardcodes `"fire": True` (`coaching.py:143`) — the field is
  informationless; drop it. 🛡️ `current_lat/lon` read raw from JSON (`:103`) with no validation — use
  a typed body.
- **impulsivity** — 🐛 `focus_resolve` reports `"switched"` even when the close lost a race
  (`impulsivity.py:219`) — check `closed is not None`. 🧹 the active-session block is duplicated with
  `focus_switch` (`:123`,`:192`).
- **focus** — 🐛 `focus_log` calls `record_focus_end(..., closed, ...)` without a `None` guard
  (`focus.py:223`), unlike `focus_end` (`:341`). 🐛 `focus_arm` `break`s on an arbitrary overlapping
  block (`:161`) — sort by latest `end_at`.
- **memory** — 🐛 `?format=structured&refresh=1` silently ignores `refresh` (`memory.py:80`). ⚡ no
  `ETag`/`Cache-Control` despite the cheap-polling goal.
- **projects** — ⚡ `project_get` loads full open sets and filters in Python (`projects.py:107`) — a
  `project_id`-scoped query is cheaper and complete. 🧹 inconsistent return shapes between
  `commitment_set_project` and `focus_set_project` (`:191`,`:204`).
- **search** — 🐛 searches raw `q` but echoes `q.strip()` (`search.py:40`) — strip before searching. 🎯
  no `has_more`/total, so the UI can't offer "show more."
- **sensor** — 🐛 `observe` echoes new rows in list order not creation order (`sensor.py:97`). 🛡️
  `list_proposals` forwards `status` with no allowlist (`:117`).
- **system** — 🧹 bare integer status codes (`system.py:303`,`:380`) break the `status.HTTP_*`
  convention. 🐛 `set_smtp` returns a 200 `{"configured": False}` on a write failure (`:370`), hiding a
  real problem.
- **admin** — 🐛 `admin_create_user` re-implements uniqueness checks with a TOCTOU gap (`admin.py:56`);
  catch the store's own error as `admin_set_user_email` does (`:98`). 🛡️ `admin_update` runs
  `run_update` synchronously with no timeout (`:244`).

### `webhooks/schemas/`
Pydantic request/response models split by domain; `__init__.py` is a re-export facade.
- 🛡️ Closed vocabularies typed as free-form `str` — life-domain fields (`schedule.py:81`, `anchor.py:22`,
  `ingestion.py:69`, `todos.py:78`), `CommitmentKind/Hardness/Outcome`, `AgreementSet.kind`,
  `SelfCareMark.key` enumerate valid values in prose but should be `Literal[...]`. `CaptureImpulse.priority`
  is unbounded while `TodoCreate.priority` correctly pins `ge=0, le=3`.
- 🛡️ Documented string formats (ISO-date/`HH:MM`/E.164/ZIP: `household.py:16`,`:83`,`:121`,
  `clarify.py:40`) have no `pattern`/length constraints — add regexes so malformed values 422 cleanly.

---

## 6. Integrations & infrastructure

### `integrations/ollama.py`
Sync wrapper over a local Ollama server: `generate()` + a `/api/tags` `available()` probe; exposes
`num_ctx` so long prompts aren't front-truncated.
- 🐛 `available()` (`ollama.py:70`) uses the full 60 s generation timeout — a liveness check that can
  hang 60 s defeats the "cheap probe to decide fallback" purpose. Pass a short (2–3 s) timeout.
- 🎯 A missing-model error body (`{"error": ...}`) isn't surfaced (`:121`) — include `data["error"]`
  in the `OllamaError`.

### `integrations/anthropic.py`
The opt-in cloud path mirroring `OllamaClient`; `available()` gates on key+SDK without a network call;
a refusal returns `""`.
- 🐛 A `max_tokens` truncation is returned as if complete — only `refusal` is special-cased
  (`anthropic.py:135`, cap 1024 at `:56`), so a truncated JSON body fails parsing downstream with no
  signal. Detect `stop_reason == "max_tokens"` and raise/return `""`.
- ⚡ A fresh client is built on every `generate()` (`:120`) — cache it.

### `integrations/provider.py`
`ANTHROPIC_AGENTS` config → per-agent client choice, availability-only.
- 🐛 `unknown_agents()` (`provider.py:118`) is dead safety code — nothing surfaces typos, so
  `ANTHROPIC_AGENTS=summariser` silently keeps that agent local. Log a one-time warning at construction.

### `integrations/base.py`
A one-line shared `ProviderError(RuntimeError)`.
- 🛡️ It carries no structured context, so callers can't tell a retryable transport blip from a
  terminal failure. A `retryable: bool` attribute would let the summarizer retry once before degrading.

### `integrations/delivery.py`
Native delivery: maps a `Decision`'s channel class to ntfy → Pushover → TTS → Twilio with signed
buttons; never raises; `resolve_route` withholds operator-default targeting on a multi-user box.
- ⚡ `resolve_route` runs `store.each_user(status="active")` on **every** delivery (`delivery.py:195`)
  just to compute `multi_user`; the household/chore sweeps call it in loops. Pass the known member
  count / memoize the flag.
- 🐛 `NtfyClient.publish` forwards `actions` with no cap (`:271`) — 4+ buttons are rejected wholesale,
  dropping the nudge. Truncate `[:3]`.

### `integrations/n8n.py`
Bidirectional n8n: outbound event POST + idempotent workflow upsert.
- 🧪 `N8nClient.trigger()` calls the module-level `httpx.post(...)` (`n8n.py:122`) instead of an
  `httpx.Client(transport=...)` — the one integration client that can't take an injected transport for
  tests. Accept an optional `transport`.
- 🛡️ `_existing_ids` (`:230`) loops on `nextCursor` with no guard against a repeated cursor — cap
  iterations.

### `integrations/nominatim.py`
Minimal forward geocoder distinguishing a clean "no match" from a transport failure.
- 🛡️ The docstring promises ≤1 req/sec but enforces nothing (`nominatim.py:14`) — a batch pass can
  hammer OSM and risk a ban. Add a monotonic inter-request delay inside the client.
- 🐛 `geocode` always takes `data[0]` (`:88`) regardless of `importance` — surface it so callers can
  reject weak resolutions.

### `integrations/sms.py`
Twilio SMS for the household invite handoff; no-op unconfigured, never raises.
- 🐛 `send` trusts `to` unnormalized (`sms.py:95`) while the sibling voice client normalizes first —
  normalize inside `send`.
- 🎯 The Twilio JSON error (`code`/`message`) is discarded (`:111`) — a 400 "unverified number" is
  opaque. Include it in `detail`.

### `integrations/smtp.py`
The only outbound email surface (a delegated todo's prep brief over the user's SMTP); STARTTLS.
- 🐛 Only STARTTLS/587 is supported (`smtp.py:105`) — a provider offering only implicit-TLS/465 (common
  for app-password relays) can't send. Branch to `smtplib.SMTP_SSL` for 465.
- 🎯 Only a plain-text body is set (`:103`) — a `text/html` alternative reads better for a brief with links.

### `integrations/summarize.py`
The single "digest → optional LLM prose → fallback" home, resolving via `ProviderResolver`.
- 🐛 It never passes `num_ctx` (`summarize.py:92`) — yet the briefing/summarizer are the *heavy* call
  sites feeding big digests, so the local model may only see a sliver (Ollama's ~2048 default). Estimate
  `num_ctx` from `len(rendered)` and bump the timeout.

### `config.py`
Central env-driven `Settings` with a dependency-free `.env` loader and tolerant parsers.
- 🐛 Several numeric envs crash startup on a typo instead of degrading: `port=int(...)` (`config.py:394`),
  `triage_quick_drop_days=float(...)` (`:440`), `triage_repeat_threshold=int(...)` (`:443`) — unlike
  `calendar_horizon_days` (`:435`). Add `_int_env`/`_float_env` helpers that fall back.
- 🎯 `_load_dotenv` (`:35`) doesn't strip `export ` or trailing inline comments — a copied shell-style
  `.env` line silently sets the wrong value.

### `crypto.py`
Fernet seal/unseal for per-user source secrets from `PREFRONTAL_SECRET_KEY`/keyfile.
- 🛡️ No key-rotation path: a single `Fernet` (`crypto.py:58`) means rotating the key bricks every
  sealed secret at once. Support `MultiFernet` (primary for new seals + old keys for decrypt, e.g.
  `PREFRONTAL_SECRET_KEYS`) so an operator can rotate without forcing every user to re-enter credentials.

### `clock.py`
The single timestamp-contract source (naive UTC, `YYYY-MM-DD HH:MM:SS`) plus the local-wall-clock
projection family.
- 🐛 `parse_ts` (`clock.py:52`) truncates to 19 chars and parses against the space-separated `TS_FMT`,
  so an ISO `T`-separated string (`"2026-07-12T09:30:00"` — the shape `stats.py` handles separately)
  returns `None`. Normalize the 11th char (`s[:19].replace("T"," ")`) so the one canonical parser
  accepts both.

### `geo.py`
Dependency-free leaf: haversine distance + default home radius.
- 🧪 No validation or tests — `haversine_m` accepts a corrupt lat of `200.0`/`NaN` and returns a
  meaningless distance that feeds the "you're home" suppression. Add a lat/lon range guard and a couple
  of known-distance unit tests.

### `log.py`
The logging seam: `get_logger` / idempotent `configure_logging`.
- 🐛 `configure_logging` uses `logging.basicConfig` (`log.py:38`), which is a **no-op** when the root
  logger already has handlers — exactly the uvicorn/gunicorn case, so the intended format never applies
  in the deployed webhook app. Pass `force=True`, or attach an explicit handler to the `prefrontal`
  namespace.

### `llm_json.py`
Tolerant JSON extraction from messy model replies (whole-string → fence → balanced span).
- 🐛 The balanced-span scanner (`llm_json.py:86`) counts braces inside quoted strings, so a valid
  `{"note":"use }{ carefully"}` closes early and yields an unparseable candidate. Track string/escape
  state, or use `JSONDecoder().raw_decode` from the first `{`.

### `selfupdate.py`
Remote/CLI update+restart, operator-gated; restart spawned detached.
- 🛡️ A successful update followed by a broken override `restart_cmd` reports `restarted: True` while
  leaving the old code running — `_spawn_detached` (`selfupdate.py:96`) fire-and-forgets to `DEVNULL`.
  Write a version/boot marker the caller can poll, or don't discard the detached command's stderr.

### `sources.py`
Service layer over the source registry; owns the encryption boundary; per-account→domain→default
resolution.
- 🛡️ A rotated/missing key makes source *listing* crash, not just resolution — `ics_sources`
  (`sources.py:167`) and `smtp_sources` (`:255`) call `unseal(...)` inline, so `SecretKeyError`
  mid-iteration 500s the whole settings page. Catch per-row and return a `needs_reauth` marker.

### `stats.py`
Pure aggregation of episodes into the Insights page's five views.
- 🐛 Two bare `except Exception` in `_chores` (`stats.py:250`,`:264`) swallow every error as "no
  household," hiding a genuine SQL error / regression. Catch the specific no-household guard exception.
- 🛡️ `_self_care` pulls `limit=10_000` (`:183`) — a long-running deployment could silently compute over
  a truncated window; use a windowed query keyed on the display period.

---

## 7. Mail subsystem

### `mail/models.py`
Normalized `MailItem` + permissive inbound normalization applying the retention policy at the door.
- 🐛 Requiring `Message-ID` drops legitimate header-less mail (`models.py:160`; the IMAP fetcher maps a
  missing header straight there, `imap.py:242`) — automation/list traffic without the header is counted
  invalid and never triaged. Thread the stable IMAP UID through as the `uid` fallback.

### `mail/triage.py`
Two-layer triage (local-model verdict + conservative keyword fallback) with a deterministic
`suppress_todo_reason` gate over the top.
- 🐛 The heuristic's bare `"?"` action marker (`triage.py:326`) matches anywhere in the body
  (`:354`), so a newsletter with one question mark becomes a `needs_action` "reply" — a real
  false-positive source when the model is down. Scope `"?"` to the subject, or require a second-person
  pronoun.

### `mail/ingest.py`
Orchestrates normalize→dedup→triage→persist through the shared `triage.apply` pipeline ("one triage,
not two") + the inbound delegation loop-closer.
- 🐛 `retriage_messages` bypasses that unification — it calls `store.add_todo(...)` directly
  (`ingest.py:528`) instead of routing through `triage_apply`, so a re-triage todo writes **no
  `triage_log` row** and `GET /triage/recent` is inconsistent depending on origin. Route the
  newly-flagged branch through the shared path.

### `mail/imap.py`
Stdlib-only IMAP fetcher (read-only, never touches `\Seen`, recency-bounded UNSEEN, Gmail
`is:important` filter).
- 🛡️ It mutates the module-global private `imaplib._MAXLINE` to 10 MB process-wide (`imap.py:173`) — a
  side effect on a private stdlib internal affecting every IMAP consumer and fragile across versions.
  Paginate the search instead, or restore the prior value in a `finally`.
- ⚡ Messages are fetched one UID at a time (`:190`) — up to 50 sequential round-trips per sync; a
  single batched `FETCH` cuts latency.

### `mail/feedback.py`
Closes the triage-learning loop, folding *reliable* drop signals (quick drops, repeat senders) into
the prompt + a hard denylist, excluding one-off slow drops.
- 🎯 The denylist is exact-address-match only (`feedback.py:135`), so a repeat offender that rotates
  its From local-part (`bounce+123@`, `notify-456@`) evades it. Add an opt-in *domain*-level denylist
  past a higher threshold.

---

## 8. Household subsystem & Context Packs

### `household/__init__.py`
The shared-sheet assembler: deterministic `build_sheet`/`render_sheet` + the periodic sweep
orchestrations, each shared by an endpoint and its CLI twin.
- 🐛 `_appt_when` uses glibc-only `%-I`/`%-d` strftime directives (`household/__init__.py:355`,`:363`)
  that raise `ValueError` on musl (Alpine) / Windows — the Mac-mini reference host works, but any
  containerized deploy breaks sheet rendering. Use portable formatting (the same fix applies to
  `fmt_time_12h` in `chores.py:171`).

### `household/chores.py`
The recurring-chore/routine engine: pure recurrence + timing predicates, schedule inheritance, the
`run_chores_check` sweep, routine completion.
- ⚡ `run_chores_check` issues per-member `member_away_window()` (`chores.py:657`) and per-chore
  `service_shift(service, week)` (`:698`) queries inside its loop, every 15–30 min. Batch the
  service-shift lookups for the week and resolve away-windows once into the map already being built.

### `household/load.py`
The three load-balancing features (weekly check-in, delta digest, balance view), tone-engineered to
avoid a scoreboard read.
- 🐛 `unseen_changes` caps star grants at the 50 most recent (`load.py:163`) while scanning facts and
  agreements in full — a parent away a while in a star-heavy household loses genuinely-unseen older
  grants from the digest. Push the `since` filter into the query (`star_awards_since(since)`).

### `household/_util.py`
Three pure leaf helpers (`_parse_hhmm`, `_fact_what`, `_star_grant_what`).
- 🐛 `_fact_what` renders a falsy-but-valid value as "cleared" (`_util.py:30`) — a fact value of `0`
  reads "dose cleared" instead of "→ 0". Test `value is None`/empty-string explicitly, not general
  truthiness.

### `packs/` (Context Packs)
Declarative composition over primitives: a `Pack` switches on modules, seeds categories/kinds/coaching
defaults (absent-only), self-registers on import; `parent` and `caregiver` ship.
- 🛡️ A pack's declared `modules`/`commitment_kinds` are never validated against the real registries, so
  a typo (`modules=("time_blindnes",)`, `commitment_kinds=("carer",)`) is a silent no-op contributing
  dead vocabulary. Add a validation pass at `register()` (or startup) asserting each key exists in the
  module registry / `commitments.KINDS`.

---

## 9. Cross-cutting themes

Several patterns recur across otherwise-clean modules and are worth a systematic sweep:

1. **Inconsistent input hardening.** The codebase's convention is to *degrade* on malformed input, but
   pockets crash instead: config numeric envs (`config.py:394`/`440`/`443`), `scheduling.work_window_now`
   (`:231`), `service_shifts.monday_of`, and non-finite (`nan`/`inf`) numbers accepted where sibling
   endpoints 422 (`schedule.py`, `todos.py`, `search.py`). A shared `_int_env`/`_float_env` and a
   finite-number validator would close most of these.
2. **Idempotency pushed onto callers.** `mail`, `triage`, and `clarifications` raise `IntegrityError`
   on concurrent re-delivery despite having unique indexes; `ON CONFLICT` upserts (as `patterns`/`state`/
   `household` already use) make re-ingestion robust.
3. **Naive substring matching where word boundaries are meant.** `classify.parse_kind_reply`,
   `trips.heuristic_reflection_outcome`, and `triage`'s `"?"`/`sale` markers all over-match; `re.search(r"\b…\b")`
   (as `todos._matches` and `roster_child_match` already do) fixes them.
4. **The auth-path full-table scan** (`users.get_user_by_token_hash`) runs on every authenticated
   request — the single highest-value hot-path fix.
5. **Repeated per-tick / per-delivery scans** — `location_anchor`/`projects` double-evaluate per tick,
   `coaching`/`delivery` re-query per cue/delivery, and `store.each_user`/`all_state` are scanned
   redundantly. Fetch-once-per-tick and prefix-scoped state APIs would cut this.
6. **Docstring drift** in unusually well-documented modules — `users` ("indexed lookup"),
   `sources` (`gcal` vs `ics`), the `structured_hash` staleness claim, and the deprecated-endpoint
   references in `anchor`/`ingestion` docstrings.
7. **Portability & robustness at the edges** — glibc-only strftime (`household`), stateless
   non-revocable sessions and import-time client/asset construction (`oauth`/`app`/`_common`),
   unbounded feed/RRULE/model output (`ics`/`commitments`/`summarizer`), and no encryption-key rotation
   (`crypto`).

### Highest-leverage individual fixes
- 🛡️ `commitments._expand_master` unbounded RRULE expansion — a DoS vector (`commitments.py:600`).
- 🐛 `nudges.py` unguarded-else logging spurious departure episodes (`nudges.py:260`).
- 🐛 `memory/db.connect` `~`-expansion opening a literal `~` file (`db.py:51`).
- 🐛 `household.redeem_invite` TOCTOU letting two users claim one invite (`household.py:1499`).
- ⚡ `users.get_user_by_token_hash` full scan on every request (`users.py:91`).
- 🐛 `log.configure_logging` silently no-op under uvicorn, so production logs lose their format (`log.py:38`).
