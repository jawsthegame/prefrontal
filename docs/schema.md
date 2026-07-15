# ADHD Agent Memory Schema

## Overview

Three **core** tables cover episodic memory, behavioral patterns, and coaching
state — the heart of the learning loop. A growing set of **feature** tables
(outings, trips, focus sessions, commitments, todos, mail, and supporting caches) back
the individual modules and ingestion paths; they are documented under
[Additional tables](#additional-tables). A summarizer agent compresses memory
into a profile document injected into every agent's system prompt.

Prefrontal is **multi-tenant**: one deployment serves several people. A `users`
table holds identities and tokens, and **every per-user table carries a
`user_id NOT NULL REFERENCES users(id)`** column (omitted from the per-column
tables below for brevity, and folded into a composite `UNIQUE`/index where a key
was previously global — e.g. `coaching_state` is unique on `(user_id, key)`). The
`MemoryStore` is bound to one user via `.scoped(user_id)`, so every read and
write is structurally filtered to that user — no call site can leak across
tenants. See [`multi-tenant.md`](multi-tenant.md).

The canonical, executable definition of this schema lives in
[`prefrontal/memory/schema.sql`](../prefrontal/memory/schema.sql). This document is the
human-readable companion — if the two ever disagree, the `.sql` file is the source of truth.

---

## Tables

### `users`
Provisioned identities. One deployment serves several people; their rows never
cross. Managed by `provision_user` / the `prefrontal user` CLI / `POST /admin/users`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `handle` | TEXT UNIQUE | Short operator/CLI name (`tom`, `sam`) |
| `display_name` | TEXT | Shown in nudges/briefings |
| `token_hash` | TEXT UNIQUE | `sha256(token)` — the raw token is shown once at creation and never stored |
| `status` | TEXT | `active`, `disabled` |
| `is_operator` | BOOLEAN | May call the admin (user-provisioning) endpoints |
| `household_id` | INTEGER NULL → `households(id)` | The household this user co-parents in, or NULL (not in one). The **second scope** — see the household tables below. A later-added column (rides the migrate.py back-fill). |
| `created_at` | DATETIME | |

---

### `episodes`
Raw outcome records. One row per agent interaction cycle.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `timestamp` | DATETIME | When the episode occurred |
| `episode_type` | TEXT | `departure`, `task`, `checkin`, `reminder` |
| `predicted_value` | REAL | What the agent estimated (time, duration, etc.) |
| `actual_value` | REAL | What actually happened |
| `acknowledged` | BOOLEAN | Did Tom respond to the trigger? |
| `channel` | TEXT | `notification`, `sound`, `tts`, `sms` |
| `context` | TEXT | Free text — location, time of day, task type |
| `outcome` | TEXT | `success`, `miss`, `partial` |
| `notes` | TEXT | Optional agent or user annotation |
| `energy` | TEXT | Task energy load (`low`/`medium`/`high`) when known — context-conditioned bias (§5) |
| `category` | TEXT | Task category when known — context-conditioned bias (§5) |

---

### `patterns`
Derived summaries computed from `episodes` by the pattern-computation pass
(`prefrontal/memory/patterns.py`, run via `prefrontal learn`). `time_estimation`,
`channel_response`, and `drift` are derived today; `context_switch` awaits switch
source data. `confidence` grows with sample size as `n / (n + 5)`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `pattern_type` | TEXT | `time_estimation`, `channel_response`, `drift`, `context_switch` |
| `context_key` | TEXT | What this pattern applies to (e.g. `departure`, `morning`, `work_block`) |
| `observed_value` | REAL | Average or median observed |
| `predicted_value` | REAL | What was being estimated |
| `variance` | REAL | Difference — positive means underestimate |
| `sample_size` | INTEGER | Number of episodes this is derived from |
| `confidence` | REAL | 0.0–1.0, low until sample size is meaningful |
| `last_updated` | DATETIME | When this pattern was last recalculated |

---

### `coaching_state`
Persistent preferences and working memory for the coaching layer.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `key` | TEXT UNIQUE | Preference name |
| `value` | TEXT | Current value |
| `last_updated` | DATETIME | When it was last changed |
| `source` | TEXT | `inferred`, `explicit` — did Tom set this or did the agent derive it? |

#### Seed rows

| key | value | source |
|---|---|---|
| `preferred_briefing_format` | `short` | explicit |
| `escalation_delay_minutes` | `5` | inferred |
| `responsive_hours_start` | `08:00` | inferred |
| `responsive_hours_end` | `14:00` | inferred |
| `preferred_reminder_channel` | `notification` | inferred |
| `time_estimation_bias` | `1.4` | inferred (40% underestimate multiplier) |
| `active_escalation_path` | `notification,sound,tts` | explicit |
| `travel_speed_kmh` | `30` | inferred (departure-reminder travel estimate) |
| `travel_road_factor` | `1.3` | inferred (straight-line → road distance) |
| `departure_prep_minutes` | `5` | inferred (flat buffer added to travel) |
| `travel_pad_fraction` | `0` | inferred (distance-relative travel padding, e.g. `0.15` = +15%; learned from the departure late-rate, or set explicitly in Settings) |
| `travel_pad_autolearn` | `on` | explicit (master switch for auto-populating `travel_pad_fraction`; `off` freezes it) |
| `departure_heads_up_minutes` | `30` | inferred (gentle "leave soon" horizon) |
| `departure_soon_minutes` | `10` | inferred ("get ready" horizon) |
| `geocoding_enabled` | `0` | explicit (opt-in network geocoding; off by default) |
| `home_zip` | `19027` | explicit (home ZIP that localizes guided playbooks; editable per user) |
| `playbook_localization` | `0` | explicit (opt-in toggle for ZIP-localized playbook guides; off by default) |

> **Seeded per user, not in `schema.sql`.** These defaults are written when a
> user is provisioned (`provision_user` in `store.py`), so each person gets their
> own copy; the `.sql` file no longer carries a seed block.

> **Runtime keys (not seeded).** `POST /webhooks/location` writes the phone's
> last-known position as `last_location_lat`, `last_location_lon`, and
> `last_location_accuracy_m`; the latitude row's `last_updated` is its freshness.
> The outing check and departure check read these when a poll body omits explicit
> coordinates. `last_departure_signature` records the last fired
> `(commitment, level)` so a standing departure reminder doesn't re-alert.
> `crunch_until` is a self-expiring timestamp (set by `prefrontal crunch on`) that
> suspends the per-key work/life time bands during a deadline stretch.
> The **feature-usage loop** (`prefrontal/usage.py`) stores its state here too:
> `usage_muted_features` is the CSV of modules the user muted (the coaching tick
> drops them); `usage_nudge_week` is the ISO week the weekly nudge last fired
> (once-a-week dedup); `usage_nudge_pending` is the feature the live nudge's
> one-tap Mute/Keep buttons act on; and `usage_kept:<feature>` timestamps a "keep"
> so that feature isn't re-suggested for a while.

> **Module-contributed keys.** Each enabled challenge-area module
> (`prefrontal/modules/`) seeds its own additional `coaching_state` rows when the
> database is initialized — e.g. `departure_buffer_minutes` and the evening
> early-start heads-up's `early_start_threshold` / `morning_prep_hour` /
> `morning_routine_minutes` / `alarm_shortcut_name` (time blindness),
> `max_first_step_minutes` (task paralysis), `hyperfocus_block_minutes` /
> `protect_aligned_hyperfocus` (hyperfocus), `pause_seconds` (impulsivity).
> Seeding never clobbers an existing value. `early_start_threshold` is also
> **learned** from late morning departures on the nightly `learn` pass (it moves
> off the seeded default once there's signal, unless you set it explicitly). Run
> `prefrontal modules -v` to see what's active.

---

## Additional tables

Beyond the three core tables above, the schema (`prefrontal/memory/schema.sql`)
also defines:

- **`outings`** — active/historical "task anchors" (a stated intention + time
  window) for the Location-Aware Task Anchor module. Carries the same nullable
  `domain` (shop/work/home/kids/personal) as `trips`, so a *returned* outing feeds
  the focus-balance rollup alongside passive trips (`prefrontal/focus_balance.py`);
  set at declaration or via `POST /webhooks/outing/domain`, and inferred from the
  free-text `intention` when unset.
- **`implementation_intentions`** — *if-then plans* for the Implementation
  Intentions module (`prefrontal/modules/implementation_intention.py`): a `cue_text`
  (the trigger in the user's words) paired with a tiny pre-decided `action_text`,
  cued on a curated `cue_place` (matched by proximity via `geo.nearest_place`), a
  `cue_window` local time-of-day band (`"HH:MM-HH:MM"`), and/or a `cue_event`
  home-crossing transition (`arrive_home`/`leave_home`, edge-detected on the tick
  against the stored home presence) — an AND over whichever are set. The coaching
  tick surfaces the action the moment its cue is detected (delivery *at the
  trigger*, not on a clock); `last_fired_at` is an
  advisory "last surfaced" stamp (the engine's debounce, not it, prevents
  re-firing). `status` is `active`/`archived` — retiring a plan is a neutral
  archive, never a broken streak. Captured in one utterance through the NL
  assistant (`add_if_then`).
- **`trips`** — closed-loop round trips detected *passively* from location pings
  crossing the home radius (`prefrontal/trips.py`, the Closed-Loop Trip Tracking
  module). Unlike an outing, nothing is declared up front: `departed_at` is when
  the phone left the home radius, `returned_at` when it came back (closing the
  loop), `max_distance_m` the farthest it reached. After the fact the user adds a
  `label`, a `category` (activity flavor — errand/social/…), and an honest
  plain-English `reflection`, classified into a `reflection_outcome`
  (success/partial/miss) that resolves the `task` episode (`episode_id`) the return
  logged — feeding the learning loop. The nullable `domain` is the orthogonal
  life-sphere (shop/work/home/kids/personal) the **focus-balance** rollup
  (`prefrontal/focus_balance.py`) sums out-of-home time up by — the same work/home
  domain todos and mail carry, so a work trip and a work todo roll up together;
  optional per-domain weekly `focus_target:<domain>` coaching keys (seeded by the
  Parent pack) turn a shortfall into a gentle "light on home this week" nudge.
- **`focus_sessions`** — active/historical deep-work blocks (a stated task, an
  optional planned duration, and an `aligned` "is this what I meant to do?" bit)
  for the Hyperfocus module. Drives the asymmetric protect-vs-interrupt logic: an
  aligned block is shielded from other modules' nudges until it overruns its plan
  (a gentle check) or passes the hard ceiling (a biological break). Also carries
  `switch_impulses`/`switches_deferred` counters — the Impulsivity module's
  capture-and-defer tally over the session — and an optional `todo_id` linking
  the block to the todo it's working, so on close that todo's `energy`/`category`
  tag the episode for context-conditioned bias (§5).
- **`commitments`** — upcoming schedule items synced from calendars (or added
  manually), used for double-booking detection and impact analysis. Optional
  `dest_lat`/`dest_lon` enable a local travel-time estimate for departure
  reminders (`prefrontal/departure.py`); without them the static `lead_minutes`
  buffer is used. `source_url` keeps the verbatim deeplink back to the source
  event/email. Optional free-text `notes` (like `todos.notes`) are a user field —
  set on manual creation or via `POST /commitments/{id}/notes`, kept across
  calendar re-syncs like `hidden`/`outcome` — and are folded into any nudge built
  for the commitment (e.g. the departure reminder: "leave now — Note: bring the
  insurance card"). `hardness` (`hard`/`soft`) marks how firm a commitment is —
  `hard` (a must-happen obligation panic/cascade treats as a fire when it slips)
  vs `soft` (an elastic block); `hardness_source` (`feed`/`user`/`default`) mirrors
  `kind_source`, so a user override (`POST /commitments/{id}/hardness`, the
  assistant, or the dashboard toggle) sticks across calendar re-syncs while a
  feed-provided value stays refreshable.
- **`todos`** — open loops (not pinned to a clock time) with an estimate and
  priority, fitted into free windows between commitments (`prefrontal/scheduling.py`).
  Each carries an inferred, editable `category` (a short topic label). The
  canonical set is *derived* — there's no registry table; it's `SELECT DISTINCT
  category` — and capped at 20 (`MAX_CATEGORIES`): `augment_todo` reuses an
  existing category unless genuinely novel and under the cap, and the dashboard
  can override it (`POST /todos/{id}/category`). The rollup that drives the
  dashboard's Categories panel (counts, typical estimate, completion rate,
  avoidance) is `category_stats` in `prefrontal/todos.py`. Each also carries a
  nullable `domain` (`work`/`home`/… the life sphere, stamped from the mail
  account or set explicitly) that **outranks category** for scheduling — the
  work/life guardrail. A todo may only be *suggested* at appropriate times: the
  window is resolved by precedence **per-todo `time_window` override → `domain` →
  `category` → `source` → default**, and a hard global off-zone (default
  22:00–06:00) applies to everything — see `WindowConfig` in
  `prefrontal/scheduling.py` and `POST /todos/{id}/window`. A travel-requiring
  todo is additionally never suggested after `TRAVEL_LATEST_HOUR` (18:00).
  A nullable `started_at` records that the user marked "I've started this"
  (`POST /todos/{id}/start`, cleared by `/unstart`, CLI `todo start`) — the
  initiation half of a **follow-through** signal: pairing the start with the
  eventual close tells whether a *started* task actually got finished. The close
  episode notes it ("completed/abandoned after starting"), and a started-then-
  dropped todo always counts as a give-up (`miss`) rather than a hygiene discard
  (`todo_episode_fields` / `_dropped_is_give_up`). The dashboard shows an
  in-progress badge on started todos, and the insights panel a **follow-through**
  headline — of the tasks you started, how many you finished (`follow_through_stats`,
  surfaced by `GET /todos/categories`).
- **`blockers`** — the mirror image of a todo: someone *else* is blocked on
  *you* (the ball's in your court). A todo is your own open loop; a blocker also
  names the `person` waiting and, via `blocking_since`, how long they've waited.
  It exists to feed **prioritization** — panic mode (`prefrontal/panic.py`) buckets
  open blockers into late/soon/piling-up (scored a notch above the equivalent todo,
  so a person waiting outranks your own back-burner) and the morning briefing
  surfaces "🙋 Waiting on you" — so an unblock can outrank a shiny new task
  (fighting shiny-object syndrome). `priority` is `0`…`3` like todos; `deadline` is
  an optional "needs it by"; an optional `todo_id` links the open loop that clears
  it. *Resolved* (`status='resolved'`, `resolved_at` stamped) rather than deleted,
  so the history stays. Managed by `prefrontal/memory/repos/blockers.py`, the
  `/blockers` CRUD router, the `prefrontal blocked` CLI, and captured in one
  utterance through the NL assistant (`add_blocker`). Pure aging/summary helpers
  live in `prefrontal/blockers.py`.
- **`todo_decompositions`** — one row per todo big enough to stall on: a tiny
  first step (≤ `max_first_step_minutes`) plus the remaining steps as JSON, the
  task-initiation lever for the Task Paralysis module (`prefrontal/todos.py`).
- **`decomposition_feedback`** — the learning signal from *dismissed* breakdowns:
  when the user waves a breakdown off, a snapshot (`todo_id`, `title`, `first_step`,
  `steps`, `category`, `estimate_minutes`) is recorded with a `reason` —
  `not_useful` (folds back into the decomposer as negative examples) or
  `not_needed` (repeated → auto-decompose switches back off), plus a `source`
  (`llm`/`heuristic`/`llm_declined`). Read by `learned_decomposition_guidance` /
  `auto_decompose_suppressed` (`prefrontal/todos.py`).
- **`dismissed_conflicts`** — soft double-bookings the user has waved off, keyed
  by a signature of the event pair so a dismissal sticks across calendar re-syncs
  but lapses if either event moves (`prefrontal/commitments.py`).
- **`dismissed_departures`** — departure reminders waved off via the one-tap
  "dismiss" link on a nudge (`GET /nudge/dismiss`), keyed by
  `commitment_id`. `/webhooks/departure/check` drops these commitments from its
  candidates; a future occurrence is a new id and re-arms on its own. (Outing
  nudges are silenced by pinning `outings.last_level` to its ceiling instead —
  no row here.)
- **`mail_messages`** — ingested and triaged email, one row per message
  (deduped on account-scoped `message_id`). The triage pass
  (`prefrontal/mail/`, Ollama with a heuristic fallback) fills `needs_action`,
  `urgency`, `category`, and a one-line `summary`; `todo_id` links to the open
  loop created for anything actionable. Retention is per-account
  (`full` stores body/snippet; `signals` stores only subject/sender + verdict).
- **`places`** — user-curated destination aliases (`name` → `lat`/`lon`, e.g.
  "gym" → coords), matched against a commitment's location/title before any
  network geocoding (`prefrontal/geocode.py`, managed via `POST /places`).
- **`geocode_cache`** — normalized free-text location → coordinates (or a
  recorded miss, `lat`/`lon` NULL), so the same address resolves once instead of
  re-calling the geocoder each sync. Populated only when `geocoding_enabled` is on.
- **`profile_cache`** — one cached LLM profile narrative **per user** (the coaching
  prose from `summarize_profile`), with its `source` (`llm`/`heuristic`),
  `model`, the `structured` input it was derived from, a `structured_hash`, and
  `generated_at`. Written by `prefrontal summarize` (or `GET /profile?refresh=1`)
  and served by `GET /profile`, so the slow model round-trip happens once rather
  than on every poll.
- **`kind_feedback`** — labeled examples for the "is this my commitment, a kid's,
  or just an FYI?" classifier: a normalized `title` → confirmed `kind`
  (`self`/`child`/`fyi`), the model's `llm_kind` prediction (for accuracy
  tracking), keyed `(user_id, title)` so a correction sticks for that title.
- **`triage_feedback`** — negative corrections for mail triage: when the user
  drops a mail-derived todo, the sender/subject/summary/category/urgency and the
  `days_open` at drop time are recorded, so a repeat or quick-drop sender is
  down-weighted next time (`prefrontal/mail/`).
- **`triage_log`** — the per-user triage audit/idempotency ledger: one row per
  ingested signal keyed by `(user_id, source, external_id)` (partial unique index
  when `external_id` is set), recording the `kind`/`route`/`decided_by` verdict so
  a flaky re-poll never double-creates a todo and the triage decisions stay
  inspectable (`prefrontal/mail/`).
- **`nudges`** — a log of nudges the system decided to send (`kind`
  `outing`/`departure`/`star`/…, escalation `level`, the delivered `message`), so
  the widget/dashboard can surface "the last thing Prefrontal told you" and a
  missed push stays visible. An optional `expires_at` gives a nudge a TTL, so a
  stale "leave now" stops showing once its moment has passed.
- **`feature_events`** — the append-only feature-usage stream behind the "what am
  I using, what am I not?" loop (`prefrontal/memory/repos/feature_usage.py`,
  surfaced on `/stats`). One row per time a feature was **offered** to you (a
  coaching nudge fired — stamped in `record_fired` with the `Cue`'s `module` +
  `intervention`, otherwise only ever in memory), **engaged** with (a one-tap
  action / iOS Shortcut — `apply_nudge_action` / `POST /webhooks/shortcut`), or
  **invoked** by you (a pull surface opened — one FastAPI middleware for the
  dashboard/panic/briefing/… routes, and a hook in the `prefrontal` CLI). Columns:
  `feature` (module key or pull-surface name), `intervention` (the declared
  `Intervention.name` for a push feature, else NULL), `event`
  (`offered`/`engaged`/`invoked`), `source`, `ref`. Deliberately separate from
  `episodes` — this is meta-telemetry about *which behaviors you lean on*, not a
  behavioral outcome the learning loop trains on. Every write is best-effort and
  never blocks the thing it records. `feature_usage_rollup` joins it against the
  module registry so a *never-fired* feature still shows as dormant.
- **`proposals`** — LLM-as-sensor candidates (`prefrontal/sensor.py`): per-user
  rows with a `kind` (`state`/`episode`), a JSON `payload`, a `rationale`, a
  `source` (`llm_inferred`), and a `status` (`pending`/`accepted`/`rejected`).
  A free-text note is turned into *allowlisted* candidates that stay **pending**
  until a human accepts — nothing authoritative is written by the model.
  `prefrontal note` / `prefrontal proposals list|accept|reject`.
- **`sources`** — the per-user external **source registry**: one row per ingestion
  feed keyed `(user_id, kind, account)` where `kind` is `imap` (a mailbox) or
  `ics` (a private ICS calendar feed). Holds connector-shaped `config` JSON and a
  Fernet-sealed `secret_enc` (the feed URL / mailbox password — **never**
  plaintext, `prefrontal/crypto.py`); `enabled=0` pauses a source without deleting
  it. Managed via `prefrontal mail add-source` / `prefrontal calendar add-source`
  (`prefrontal/sources.py`) — the per-user replacement for one global set of feeds
  in the environment.
- **`clarifications`** — ambiguity questions for vague todos/commitments
  (`prefrontal/clarify.py`, a Task-Paralysis initiation lever): per-user rows with
  a `target_type` (`todo`/`commitment`), a loose `target_id`, the `title` snapshot,
  a `question`, JSON `options` (candidate readings, each with an optional
  `task_type`), a `source` (`llm`/`heuristic`), and a `status`
  (`pending`/`resolved`/`dismissed`). A one-word item ("Tax") that can't be started
  because it can't be *named* gets one inline question; answering records the
  chosen reading in `answer` (+ a recognized `task_type`, which unlocks a guided
  **playbook** overlay), dismissing marks it not-ambiguous. Like a proposal it
  stays pending until the human answers. A partial unique index allows at most one
  *pending* question per item, and the detection sweep (`sweep_ambiguous_items`,
  run on the coaching tick beside the decomposition sweep) skips any item with
  history (pending/answered/dismissed) so it never re-asks.
  `POST /clarifications/check` (the on-demand twin), `GET /clarifications`,
  `POST /clarifications/{id}/resolve|dismiss`,
  `GET /clarifications/playbooks/{task_type}`.
- **`people`** — the identified, categorized **roster** Prefrontal builds from the
  names ingested items keep mentioning (`prefrontal/people.py`). Per-user rows with
  a canonical `name`, a normalized `name_key` (so "sam"/"Sam" are one person,
  `UNIQUE (user_id, name_key)`), JSON `aliases` (other spellings matched to them), a
  `relationship` (`family`/`coworker`/`friend`/`professional`/`service`/
  `acquaintance`/`other`/`unknown`), an `importance` (`0`…`3`, mirroring todo
  priority), free-text `notes`, and the `mention_count`/`first_seen`/`last_seen`
  recurrence signal that every naming *touches*. It exists to feed **learning**
  (importance-≥2 people become a "Key people" section in the summarizer profile) and
  **prioritization** (`people.priority_boost` bumps a triage-created todo that names
  a high-importance person). `status` is `active`/`archived`. Managed by
  `prefrontal/memory/repos/people.py`, the `/people` CRUD router, and the
  `prefrontal people` CLI.
- **`person_mentions`** — the **review queue**. When an ingested item names someone
  not yet on the roster, a `pending` mention lands here for the user to *identify*
  (link an existing person, or create + categorize a new one) or *dismiss* (not a
  person / not worth tracking). Per-user rows with the `name` as it appeared, its
  `name_key`, a `source` (`mail`/`calendar`/`n8n`/`manual`/`braindump`/`triage`), a
  `context` snippet, the routed item `ref` (`todo:…`/`commitment:…`) and
  `external_id` it came from, a nullable `person_id` (set on identify), and a
  `status` (`pending`/`identified`/`dismissed`). Like a sensor proposal or a
  clarification, nothing authoritative is written from a raw extracted name — it
  stays pending until a human resolves it. A partial unique index keeps a recurring
  name to **one** pending row (a repeat appearance instead touches the known person
  / leaves the single queue entry). Names are extracted heuristically on the triage
  ingest path (`prefrontal/triage.py`, best-effort, never blocking capture) and
  on-demand via `POST /people/extract` (which can also use the model). Endpoints:
  `GET /people/queue`, `POST /people/mentions/{id}/identify|dismiss`.

---

## Shared household sheet (household-scoped)

The backbone of the **Parent** Context Pack (full design:
[`household-sheet.md`](household-sheet.md)). Unlike every table above — each
scoped to one `user_id` — these are scoped to a **household**: two co-parents
belong to one household (`users.household_id`) and see the *same* rows. The
`MemoryStore` household methods (`prefrontal/memory/repos/household.py`) inject
`WHERE household_id = ?` the way the per-user methods inject `WHERE user_id`, and
a user in no household raises rather than reading across households. Rendered into
the `/family` view by `prefrontal/household.py`; edited in plain English via the
assistant's `set_fact`/`clear_fact`/`set_agreement`/`remove_agreement` ops.

**`child_id` convention:** household-wide (not per-kid) facts/agreements use the
sentinel `child_id = 0` — SQLite treats NULLs as distinct in a UNIQUE constraint,
so a NULL would let duplicate household-wide rows through. A real child is a
positive `children.id`.

- **`households`** — the household entity (`id`, `name`, `created_at`). Membership
  is either operator-set (`create_household` / `set_user_household`, via
  `prefrontal household add/join` and `POST /admin/households`) **or self-serve**:
  a user creates their own (`POST /household/create`) and invites a co-parent with
  a code (see `household_invites`). Also holds the
  opt-in weekly **mental-load check-in** schedule — `checkin_enabled`,
  `checkin_day` (0=Mon…6=Sun), `checkin_time` (`"HH:MM"`), and `checkin_last_sent_at`
  (weekly dedup by ISO week) — and the opt-in daily **delta digest** toggle
  `digest_enabled`. The digest's per-parent "last looked at the sheet" and "last
  digested" stamps live in each user's `coaching_state`
  (`household_seen_at` / `household_digested_at`), so it surfaces only the *other*
  parent's unseen changes; the sweep is `POST /webhooks/household/digest/check`.
  `balance_enabled` opts into the gentle **load-balance view** — a
  "who's been keeping the sheet up" split derived on read from `updated_by` /
  `awarded_by` counts over a 30-day window (no push, shown on `/kids`). Finally, an
  optional **away/vacation window** — `away_start`/`away_end` (inclusive local
  `"YYYY-MM-DD"`, all NULL = not away) and a short `away_note` — gates chores whose
  `away_behavior='suppress'`, so a beach-trip week doesn't nag about the recycling.
- **`children`** — the roster: stable identity (`household_id`, `name`,
  `birthday`), `UNIQUE (household_id, name)`. Also holds **pets**: `kind`
  (`child`/`pet`, default `child`) and `species` (dog/cat/…; NULL for a child), so
  a pet reuses the same facts/appointments/shopping plumbing. Everything else about
  a child is a fact, not a column.
- **`household_facts`** — the categorized key/value grid: `child_id` (0 =
  household-wide), `category` (a controlled vocab — `sizes`, `routine`, `food`,
  `health`, `school`, `contact`), normalized `item`, free-text `value`, and
  provenance (`updated_by`/`updated_at`). `UNIQUE (household_id, child_id,
  category, item)` — writes upsert in place and re-stamp who touched it (the raw
  material for the v2 delta digest).
- **`household_agreements`** — standing behaviour plans: `title`, `kind`
  (`reward`/`consistency`/`routine`), plain-language `body`, an optional
  `structured` JSON for star/points charts (thresholds → rewards, earn-only), and
  provenance. `UNIQUE (household_id, child_id, title)`. A star chart's `structured`
  may also carry a `prompt` block — `{enabled, days:[0=Mon…6=Sun], time:"HH:MM",
  question}` — a recurring "did they earn a star today?" check-in; `last_prompted_at`
  dedups that prompt to once per local day (the sweep at
  `POST /webhooks/household/star-prompts/check`).
- **`household_stars`** — the star/points **ledger** behind a reward chart: one
  append-only row per grant (`agreement_id`, `child_id` copied from the chart,
  `delta`, optional `note`, `awarded_by`/`created_at`). The agreement's
  `structured` declares the *goals*; this table is the running *earnings* against
  them (a chart's total is `SUM(delta)` over its `agreement_id`). A grant that
  carries the total across a threshold "reaches" that goal — the write layer then
  congratulates and pushes to **both** co-parents (`newly_reached_goals` in
  `prefrontal/household.py` → `deliver_to_household` in
  `prefrontal/integrations/delivery.py`). Awarded via
  `POST /household/agreements/{id}/stars` or `prefrontal household star`; the
  running total and next reward render on the shared sheet.

- **`household_checkins`** — the weekly mental-load self-reports: one row per
  parent per ISO `week` with a `response` (`light`/`balanced`/`heavy`) and
  provenance. `UNIQUE (household_id, week, user_id)` — a re-tap overwrites in
  place. Deliberately subjective and non-judgmental: it records how each parent
  *felt*, never who did what, and once both have replied a gentle shared note goes
  back to both (`checkin_summary` in `prefrontal/household.py`). Opt-in via the
  `households.checkin_*` schedule; the sweep is
  `POST /webhooks/household/checkin/check`.

- **`household_shopping`** — the shared shopping list: `item`, `spec`,
  `where_to_buy`, `got` (0/1), `child_id` (0 = household-wide), with provenance on
  both add (`added_by`/`created_at`) and buy (`got_by`/`got_at`). Either parent
  adds and checks off; it rides the shared sheet (`build_sheet` → a **Shopping**
  section). A shared checklist, deliberately not per-user `todos`. Available to
  every household (not load-balancing), including a solo one.

- **`household_invites`** — self-serve membership: a short, unique, expiring
  `code` a member mints (`created_by`) for their household; a co-parent redeems it
  (`redeemed_by`/`redeemed_at`, one-time) to join with no operator step. Endpoints
  `POST /household/invites{,/redeem,/{id}/revoke}`; CLI `household invite`/`redeem`.

- **`household_routines`** — a named grouping of chores with ONE **accountable**
  owner (`accountable_id`, RACI "A" — the mental-load holder, distinct from a
  chore's `owner_id` "responsible" doer). Carries the schedule its chores inherit:
  `days` (weekday-int CSV; empty = every day), `month_days` (day-of-month-int CSV
  `"1,15"`; when set it takes precedence over `days`, and a day past a short month
  fires on its last day), and `due_time` (`HH:MM` local, or `''` = not time-tied —
  a pure grouping). Plus `impact`, `enabled`, provenance, and `last_completed_on`
  (local "YYYY-MM-DD" all its chores were last done — the per-day cursor that dedups
  the completion celebration).
  `UNIQUE (household_id, title)`. Accountability is load: it's the "carrying" facet
  of the balance view (`accountability_counts`). A routine is "done for the day"
  once every one of its enabled chores is logged done today; finishing that last
  chore congratulates both parents once (`log_chore_done_and_celebrate`, the shared
  path behind every "mark done" surface) and the sheet highlights it. Endpoints
  `POST /household/routines` (+`/enabled`, `/remove`); CLI `household routine`.

- **`household_chores`** — recurring shared chores (the active load-balancer):
  `title`, `owner_id` (RACI "R" — whose job to do it; NULL = either parent),
  `routine_id` (NULL = stands alone), `days` (weekday-int CSV; empty = inherit
  routine / every day), `month_days` (day-of-month-int CSV `"1,15"`; when set it
  takes precedence over `days`), `due_time` (`HH:MM` local; `''` = inherit the
  routine's time, or run *untimed* — a checklist chore with no reminder), `remind_before`
  (minutes), `impact` (the "why"), `enabled`, and two local-date dedup cursors
  `last_reminded_on` / `last_missed_on`. `away_behavior` (`keep`/`suppress`)
  decides whether the chore is skipped during the household's away window; the
  optional `service` (`trash`/`recycling`/…) links a municipal-pickup chore to a
  `service_shifts` row so a holiday-shifted collection day moves the reminder. A
  chore in a routine inherits its schedule unless it sets its own time
  (`effective_chore_schedule`). The sweep
  (`POST /webhooks/household/chores/check`) sends the owner a lead-time reminder
  and — if it slips past due — the *other* parent a gentle heads-up. Pure logic +
  the shared `run_chores_check` live in `prefrontal/household.py`; CLI
  `household chore`/`chores-check`.

- **`household_chore_log`** — one row per chore per local day it was completed
  (`done_on`, `done_by`), `UNIQUE (household_id, chore_id, done_on)`. Answers "done
  today?" for the sweep and the sheet, and keeps *who did it* legible so the loop
  closes even when the other parent picks up a slipped chore — and feeds the
  "doing" facet of the balance view (`contribution_counts` counts completions). A
  one-tap **Done** button (`chore_done`) or `POST /household/chores/{id}/done`
  writes it.

- **`service_shifts`** — scraped municipal **pickup-day shifts**: one row per
  `(household_id, service, week)` recording that a collection (`trash`/`recycling`/…,
  matching `household_chores.service`) moved to `shifted_weekday` (0=Mon…6=Sun) that
  week, with a `reason` ("Independence Day") and `source_url`/`fetched_at`
  provenance. A chore linked by `service` reads this so a holiday-shifted collection
  reminds on the moved day rather than the usual one.

Appointments are **not** a new table: a kid's appt is a `commitments` row tagged
`kind='child'` (`prefrontal/commitments.py`), which the sheet surfaces in its
"upcoming" section.

---

## System Prompt Injection

A summarizer agent runs periodically and writes a `profile-<handle>.md` from the above
tables, **caching** the result in `profile_cache`. Every agent prepends this to
its system prompt; `GET /profile` serves the cached narrative (regenerate with
`?refresh=1`, or get the raw structured input with `?format=structured`).

**Example output:**

```
Tom typically underestimates travel time by 40% — apply a 1.4x multiplier to 
all departure predictions. He responds to notifications reliably before 2pm but 
ignores them after 3pm; escalate to TTS for anything time-critical in the 
afternoon. Task blocks involving admin work have a high drift rate — check in 
at 20 minutes rather than 30.
```

---

## Feedback Capture

Outcomes get into `episodes` via:

- **iOS Shortcut buttons** — "Made it" / "Missed it" one-tap logging via webhook
- **End-of-day check-in** — agent parses a short voice or text summary
- **Passive inference** — location confirmation, calendar event completion
