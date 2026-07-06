-- Prefrontal behavioral memory schema.
--
-- The canonical, executable definition of the memory layer. The human-readable
-- companion lives in docs/schema.md; if the two disagree, THIS file wins.
--
-- A `users` table makes this multi-tenant: one deployment serves several
-- people, and every user-owned table carries a NOT NULL user_id referencing it.
--
-- Three core tables hold the learning loop (all per-user):
--   episodes        raw outcome records, one per agent interaction cycle
--   patterns        derived summaries computed from episodes by the learn pass
--   coaching_state  persistent key/value preferences and working memory
--
-- Plus feature tables backing the modules and ingestion paths:
--   outings              Location-Aware Task Anchor blocks (intention + window)
--   trips                closed-loop trips auto-detected from location crossings
--   focus_sessions       Hyperfocus deep-work blocks (protect / interrupt)
--   commitments          synced/manual schedule items (impact + double-booking)
--   todos                open loops fitted into free windows
--   todo_decompositions  tiny-first-step breakdown for stall-prone todos
--   dismissed_conflicts  soft double-bookings the user has waved off
--   dismissed_departures departure reminders waved off from a notification tap
--   mail_messages        ingested + triaged email, surfaced as action items
--   profile_cache        single cached LLM profile narrative served by GET /profile
--   places, geocode_cache  local-first destination resolution for departures
--
-- Everything is idempotent: CREATE TABLE IF NOT EXISTS, so applying this file
-- repeatedly is safe. (Coaching-state defaults are no longer seeded here; they
-- are seeded per user at provision time — see provision_user in store.py.)
--
-- Multi-tenant: one deployment serves several people. Every user-owned table
-- carries a NOT NULL user_id referencing users(id), and the MemoryStore is
-- constructed bound to one user_id so it structurally injects the scope into
-- every read and write (no call site can forget a WHERE user_id=?). See
-- docs/multi-tenant.md for the full design.

PRAGMA foreign_keys = ON;

-- Provisioned users. One deployment serves several people; their rows never
-- cross. Tokens are never stored in plaintext — only sha256(token), shown once
-- at creation like an API key. `handle` is the stable CLI/operator identifier;
-- `display_name` is what appears in nudges ("Tom, you said 15 min…").
-- Households — the second scope alongside per-user. Two co-parents belong to the
-- same household and share its rows (facts, agreements, roster); this is the
-- deliberate exception to strict per-user scoping. A user belongs to 0 or 1
-- household (v1). See docs/household-sheet.md.
CREATE TABLE IF NOT EXISTS households (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Opt-in weekly "mental load" check-in: a gentle, non-judgmental self-report
    -- both parents get on the chosen weekday/time. Off by default. See
    -- household_checkins + docs/household-sheet.md.
    checkin_enabled      INTEGER NOT NULL DEFAULT 0,  -- 0/1, opt-in
    checkin_day          INTEGER,                     -- 0=Mon … 6=Sun
    checkin_time         TEXT,                        -- "HH:MM" local
    checkin_last_sent_at DATETIME,                    -- weekly dedup (see week_key)
    -- Opt-in daily "delta digest": push each parent the OTHER parent's changes
    -- they haven't seen yet. Off by default. Per-parent "seen"/"digested" stamps
    -- live in coaching_state (household_seen_at / household_digested_at).
    digest_enabled       INTEGER NOT NULL DEFAULT 0,  -- 0/1, opt-in
    -- Opt-in "who's keeping the sheet up" balance view — a gentle, non-accusatory
    -- picture from updated_by/awarded_by counts. Off by default; shown on /kids
    -- (no push). Derived on read, so there's nothing else to store.
    balance_enabled      INTEGER NOT NULL DEFAULT 0   -- 0/1, opt-in
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handle       TEXT    NOT NULL UNIQUE,        -- short login-ish name: 'tom', 'sam'
    display_name TEXT,                            -- shown in nudges/briefings
    token_hash   TEXT    NOT NULL UNIQUE,         -- sha256(token); raw token never stored
    status       TEXT    NOT NULL DEFAULT 'active', -- active | disabled
    is_operator  BOOLEAN NOT NULL DEFAULT 0,      -- may call admin endpoints
    -- The household this user co-parents in, or NULL (not in one). A later-added
    -- column, so it rides the migrate.py back-fill (nullable, no data migration);
    -- the household tables key on it. See docs/household-sheet.md.
    household_id INTEGER REFERENCES households(id),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_token ON users (token_hash);

-- Raw outcome records. One row per agent interaction cycle.
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    timestamp       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    episode_type    TEXT    NOT NULL,           -- departure|task|checkin|reminder|mail|panic|switch
    predicted_value REAL,                       -- what the agent estimated
    actual_value    REAL,                       -- what actually happened
    acknowledged    BOOLEAN,                    -- did the user respond to the trigger?
    channel         TEXT,                       -- notification | sound | tts | sms
    context         TEXT,                       -- free text: location, time of day, task type
    outcome         TEXT,                       -- success | miss | partial
    notes           TEXT,                       -- optional agent or user annotation
    energy          TEXT,                       -- task's energy load (low|medium|high), when known
    category        TEXT                        -- task's category, when known (context-conditioned bias)
);

CREATE INDEX IF NOT EXISTS idx_episodes_type ON episodes (episode_type);
CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes (user_id, timestamp);

-- Derived summaries. Updated periodically by the summarizer agent from episodes.
CREATE TABLE IF NOT EXISTS patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    pattern_type    TEXT    NOT NULL,           -- time_estimation | channel_response | drift | context_switch
    context_key     TEXT    NOT NULL,           -- what this pattern applies to (departure, morning, work_block)
    observed_value  REAL,                       -- average or median observed
    predicted_value REAL,                       -- what was being estimated
    variance        REAL,                       -- difference; positive means underestimate
    sample_size     INTEGER DEFAULT 0,          -- number of episodes this is derived from
    confidence      REAL    DEFAULT 0.0,        -- 0.0-1.0, low until sample size is meaningful
    last_updated    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- One row per (user_id, pattern_type, context_key) so the summarizer can
    -- upsert in place, and two users never collide on the same pattern key.
    UNIQUE (user_id, pattern_type, context_key)
);

-- Persistent preferences and working memory for the coaching layer. Per-user:
-- each user gets their own copy of the defaults at provision time, and one
-- user changing a preference never touches another's.
CREATE TABLE IF NOT EXISTS coaching_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    key          TEXT    NOT NULL,              -- preference name
    value        TEXT,                          -- current value
    last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source       TEXT,                          -- inferred | explicit
    UNIQUE (user_id, key)
);

-- Active and historical "outings": a stated intention plus a time window, used
-- by the Location-Aware Task Anchor module to nudge the user back on track. One
-- row per declared outing ("getting coffee, back in 15 min").
CREATE TABLE IF NOT EXISTS outings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    intention           TEXT    NOT NULL,            -- free-text stated mission
    time_window_minutes REAL    NOT NULL,            -- stated "back in N minutes"
    departure_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    home_lat            REAL,                         -- baseline location (optional)
    home_lon            REAL,
    status              TEXT    NOT NULL DEFAULT 'active',  -- active | returned | abandoned
    last_level          TEXT    NOT NULL DEFAULT 'none',   -- highest escalation already fired
    returned_at         DATETIME,                     -- set when the outing is closed
    home_arrived_at     DATETIME,                     -- first time location confirmed home (backdates a return; drives the arrival prompt + grace auto-close)
    domain              TEXT,                         -- life-sphere for focus balance (shop/work/home/kids/personal)
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outings_user_status ON outings (user_id, status);

-- Closed-loop "trips": a leave-home → return-home round trip detected *passively*
-- from location pings crossing the home radius (no declared intention or window,
-- unlike an outing). One row per loop: `departed_at` when the phone first left
-- the home radius, `returned_at` when it came back (which closes the loop). After
-- the fact the user is asked to `label` and `category`-ize the trip, and can
-- submit a plain-English `reflection` on how it went — classified into an
-- `reflection_outcome` (success/partial/miss) that resolves the `task` episode
-- (`episode_id`) the return logged, so honest self-report feeds the learning loop.
-- `domain` is the orthogonal life-sphere (shop/work/home/kids/personal) the focus-balance
-- rollup (`prefrontal/focus_balance.py`) sums time-out by; it mirrors the work/home
-- domain todos and mail already carry, so a work trip and a work todo roll up together.
CREATE TABLE IF NOT EXISTS trips (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    departed_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- left the home radius
    returned_at        DATETIME,                       -- back within it (closes the loop)
    status             TEXT    NOT NULL DEFAULT 'active',  -- active | completed
    depart_lat         REAL,                           -- home fix the loop opened from
    depart_lon         REAL,
    max_distance_m     REAL    NOT NULL DEFAULT 0,      -- farthest from home seen while out
    label              TEXT,                           -- user "what was this" (NULL until labeled)
    category           TEXT,                           -- user categorization (errand/social/…)
    domain             TEXT,                           -- life-sphere for focus balance (shop/work/home/kids/personal)
    reflection         TEXT,                           -- plain-English "how it went", set on reflect
    reflection_outcome TEXT,                           -- classified success | partial | miss
    episode_id         INTEGER,                        -- the task episode the return logged
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trips_user_status ON trips (user_id, status);

-- Active and historical "focus sessions": a declared block of deep work on a
-- stated task, used by the Hyperfocus module. Unlike an outing (which escalates
-- to pull you *back* home), a focus session is asymmetric — it *protects* an
-- aligned block while it's healthy and only interrupts to (a) gently check
-- alignment once it overruns its plan, or (b) force a biological break past the
-- hard ceiling. `aligned` is the protect bit (is this the thing you meant to be
-- doing?); `planned_minutes` is an optional intended duration; `breadcrumb` and
-- `outcome` are captured on exit to make re-entry cheap and feed the learning loop.
CREATE TABLE IF NOT EXISTS focus_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    intended_task   TEXT    NOT NULL,                  -- what you're getting into
    todo_id         INTEGER,                           -- optional link to the todo being worked (its energy/category tag the episode)
    planned_minutes REAL,                              -- optional intended duration
    aligned         BOOLEAN NOT NULL DEFAULT 1,        -- is this the thing you meant to do?
    started_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status          TEXT    NOT NULL DEFAULT 'active',  -- active | ended | abandoned | switched
    last_level      TEXT    NOT NULL DEFAULT 'none',    -- highest interrupt already fired
    switch_impulses   INTEGER NOT NULL DEFAULT 0,       -- times a switch-away was signalled
    switches_deferred INTEGER NOT NULL DEFAULT 0,       -- of those, captured-and-deferred
    breadcrumb      TEXT,                              -- "where I was / next step", set on exit
    outcome         TEXT,                              -- worth_it | should_have_stopped | pulled_off
    ended_at        DATETIME,                          -- set when the session is closed
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_focus_sessions_user_status
    ON focus_sessions (user_id, status);

-- Upcoming commitments — the schedule the system reasons about (e.g. for impact
-- analysis: "running over now puts your 10:30 at risk"). Populated from a
-- calendar by the sync endpoint, or added manually. `lead_minutes` is the
-- travel+prep buffer needed before `start_at`; `hardness` distinguishes a hard
-- deadline from a soft one. `external_id` is the calendar event id, used to
-- upsert on re-sync (partial-unique so manual rows can omit it).
CREATE TABLE IF NOT EXISTS commitments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    external_id  TEXT,                              -- calendar event id (NULL for manual)
    title        TEXT    NOT NULL,
    start_at     DATETIME NOT NULL,                 -- stored normalized to UTC
    end_at       DATETIME,
    location     TEXT,                              -- free-text location (e.g. "123 Main St")
    source_url   TEXT,                              -- deeplink to the source event/email (verbatim), if the sync supplies one
    dest_lat     REAL,                              -- optional destination coordinates, used to
    dest_lon     REAL,                              --   estimate travel time for departure reminders
    lead_minutes REAL    NOT NULL DEFAULT 10,       -- travel+prep buffer before start
    hardness     TEXT    NOT NULL DEFAULT 'soft',   -- hard | soft
    source       TEXT    NOT NULL DEFAULT 'calendar', -- calendar | manual
    status       TEXT    NOT NULL DEFAULT 'active',   -- active | cancelled
    kind         TEXT    NOT NULL DEFAULT 'self',   -- self (yours) | fyi (where someone will be; never a conflict)
    kind_source  TEXT,                              -- how kind was set: llm | user | default
    hidden       BOOLEAN NOT NULL DEFAULT 0,        -- user "don't show me this": dropped from every surface (dashboard, widget, conflicts, departures); survives calendar re-sync
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_commitments_external
    ON commitments (user_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commitments_user_start ON commitments (user_id, start_at);

-- Open loops — "todos" that aren't pinned to a clock time but need doing
-- (call the dentist, plan a birthday). The scheduler fits these into free
-- windows between commitments. `estimate_minutes` is how long it'll take;
-- `priority` 0=low…3=urgent; `energy` and `deadline` are optional hints.
CREATE TABLE IF NOT EXISTS todos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    title            TEXT    NOT NULL,
    notes            TEXT,
    estimate_minutes REAL,
    priority         INTEGER NOT NULL DEFAULT 1,    -- 0 low | 1 normal | 2 high | 3 urgent
    deadline         DATETIME,                       -- optional, UTC
    energy           TEXT,                           -- low | medium | high (optional)
    category         TEXT,                           -- inferred, editable; canonical set derived (capped at 20)
    time_window      TEXT,                           -- optional per-todo override "HH:MM-HH:MM" local; else domain/category/source/default window
    domain           TEXT,                           -- work | home | ... — the life sphere; outranks category for the window (work/life guardrails)
    source           TEXT    NOT NULL DEFAULT 'manual', -- manual | impulse (captured-and-deferred)
    status           TEXT    NOT NULL DEFAULT 'open', -- open | done | dropped
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at     DATETIME
);

CREATE INDEX IF NOT EXISTS idx_todos_user_status ON todos (user_id, status);

-- Dismissed "possible conflicts" — soft double-bookings (a generic Busy/Block/
-- Hold overlapping a real event) the user has waved off. Keyed by a signature
-- of the event pair (external_id + start + title), so a dismissal sticks across
-- re-syncs but lapses if either event moves or is retitled (the key changes).
CREATE TABLE IF NOT EXISTS dismissed_conflicts (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    signature    TEXT NOT NULL,
    dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, signature)
);

-- Departure reminders the user waved off from a notification (the one-tap
-- "dismiss" link on a Pushover nudge). Keyed by commitment_id, which is unique
-- per occurrence, so a dismissal suppresses that specific reminder while a
-- future occurrence (a new row/id) re-arms on its own.
CREATE TABLE IF NOT EXISTS dismissed_departures (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    commitment_id INTEGER NOT NULL,
    dismissed_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, commitment_id)
);

-- Labeled examples that teach the "is this my commitment, or just FYI about
-- where someone will be?" classifier. Seeded by the LLM's own determinations
-- and, more importantly, by the user correcting them in the UI. Keyed by the
-- normalized (lowercased) title so the latest verdict per title wins; these
-- rows are folded back into the classifier prompt as few-shot examples, so the
-- model's behavior evolves toward the user's corrections over time.
CREATE TABLE IF NOT EXISTS kind_feedback (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,       -- normalized (lowercased, trimmed) title
    display     TEXT NOT NULL,       -- original-case title, shown to the model
    kind        TEXT NOT NULL,       -- the corrected/confirmed label: self | fyi
    llm_kind    TEXT,                -- what the model predicted (NULL if no LLM verdict)
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, title)
);

-- Ingested and triaged email. One row per message, deduplicated on the
-- account-scoped `message_id` so re-syncing the same inbox is idempotent.
-- Retention is per-account (see config.mail_accounts): a `full`-policy account
-- stores `snippet`/`body`; a `signals` account stores only subject + sender +
-- the triage verdict (body/snippet stay NULL and are never even sent to the
-- model). The triage fields are filled by the Ollama pass (with a heuristic
-- fallback); `todo_id` links to the open loop created for anything that needs
-- action. Local-first: nothing here leaves the host.
CREATE TABLE IF NOT EXISTS mail_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    account       TEXT    NOT NULL,                -- logical account name (personal | work | corp)
    message_id    TEXT    NOT NULL,                -- provider message id, account-scoped unique
    thread_id     TEXT,                            -- provider thread id (optional)
    sender_name   TEXT,
    sender_email  TEXT,
    subject       TEXT,
    received_at   DATETIME,                        -- stored normalized to UTC
    snippet       TEXT,                            -- NULL for signals-policy accounts
    body          TEXT,                            -- NULL for signals-policy accounts
    unread        BOOLEAN,
    needs_action  BOOLEAN NOT NULL DEFAULT 0,      -- triage: does this need a reply / action?
    urgency       TEXT,                            -- low | normal | high | urgent
    category      TEXT,                            -- reply | meeting | fyi | newsletter | notification | other
    waiting_on    TEXT,                            -- who is waiting on the user (free text), if any
    summary       TEXT,                            -- one-line triage summary
    triage_source TEXT,                            -- llm | heuristic
    policy        TEXT    NOT NULL DEFAULT 'full', -- retention policy applied (full | signals)
    todo_id       INTEGER REFERENCES todos (id),   -- open loop created for a needs-action item
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, account, message_id)
);

CREATE INDEX IF NOT EXISTS idx_mail_user_account ON mail_messages (user_id, account);
CREATE INDEX IF NOT EXISTS idx_mail_user_needs_action
    ON mail_messages (user_id, needs_action);
CREATE INDEX IF NOT EXISTS idx_mail_user_received ON mail_messages (user_id, received_at);

-- Negative triage corrections, learned from dropped email todos. When the user
-- drops a todo that mail intake created (a `needs_action` verdict), that's a
-- signal triage may have over-flagged. But a drop is ambiguous — it can equally
-- mean "I avoided something I should have done" (exactly what avoided_todos
-- exists to catch) — so we record the *context* of each drop and let the
-- prompt-builder decide what's signal: only quick drops (`days_open` small) and
-- repeat-offender senders become "don't flag this" hints. Those are folded back
-- into the triage system prompt as negative few-shot examples, so triage evolves
-- toward the user's corrections (mirrors `kind_feedback` for the commitment
-- classifier). One row per drop — repetition is what makes a sender a hint.
CREATE TABLE IF NOT EXISTS triage_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    todo_id       INTEGER REFERENCES todos (id),  -- the dropped todo (kept for provenance)
    message_id    TEXT,                            -- originating mail message_id
    sender_email  TEXT,
    sender_name   TEXT,
    subject       TEXT,
    summary       TEXT,                            -- triage summary at flag time
    category      TEXT,                            -- triage category at flag time
    urgency       TEXT,                            -- triage urgency at flag time
    days_open     REAL,                            -- age (days) when dropped — the quick-drop discriminator
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_triage_feedback_user ON triage_feedback (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_triage_feedback_sender
    ON triage_feedback (user_id, sender_email);

-- Log of nudges the system decided to send. The escalation checks
-- (/webhooks/outing/check and /webhooks/departure/check) each fire a nudge once
-- per newly-crossed level and hand the message to n8n to deliver; until now that
-- message was fire-and-forget. Recording it here gives every surface a "what did
-- Prefrontal last tell me?" feed — the widget shows the most recent one so a
-- missed notification is still visible at a glance. One row per fired nudge.
CREATE TABLE IF NOT EXISTS nudges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT    NOT NULL,            -- 'outing' | 'departure'
    level      TEXT,                        -- escalation level at fire time (kind-specific)
    message    TEXT    NOT NULL,            -- the delivered nudge text
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- When this nudge stops being relevant, so a surface can hide a stale one.
    -- Defaults to a couple hours out (DEFAULT_NUDGE_TTL_HOURS) when the caller
    -- gives none, so an outing "still on track?" doesn't linger for hours after
    -- you're back. A departure nudge instead sets it to the commitment's
    -- start_at: "leave now" is pointless once the meeting has started. NULL
    -- (no expiry) only occurs on legacy rows predating the default.
    expires_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_nudges_user ON nudges (user_id, created_at);

-- Cached LLM profile narrative — the prioritized coaching prose produced by the
-- summarizer (prefrontal/memory/summarizer.py). Generating it needs an Ollama
-- (or, later, Anthropic) round-trip, which is too slow to run on every
-- `GET /profile` poll, so the nightly `prefrontal summarize` writes it here and
-- the endpoint serves it. One row (id = 1). `structured_hash` is a fingerprint of
-- the structured profile the prose was derived from, so callers can tell when the
-- cache has gone stale (the underlying patterns/state changed) without a full
-- copy comparison.
CREATE TABLE IF NOT EXISTS profile_cache (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id),  -- one cached narrative per user
    text            TEXT    NOT NULL,                    -- the narrative to serve
    source          TEXT    NOT NULL,                    -- llm | heuristic
    model           TEXT,                                -- model name when source = llm
    structured      TEXT,                                -- the structured profile fed to the model
    structured_hash TEXT,                                -- fingerprint of `structured` at cache time
    generated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Task decompositions — a tiny first step (≤ max_first_step_minutes) plus the
-- remaining steps, for todos big enough to stall on (≥ decomposition_threshold).
-- The first step is the initiation lever; the rest stays collapsed so the list
-- doesn't re-trigger paralysis. One row per todo (regenerate = replace).
--
-- `done_steps` is a JSON array of completed step indices, where index 0 is the
-- first_step and 1..N are `steps`. Checking off a step is its own little win —
-- visible progress is what keeps a decomposed task moving. Regenerating the
-- decomposition replaces the row, which resets progress (the steps changed).
CREATE TABLE IF NOT EXISTS todo_decompositions (
    todo_id            INTEGER PRIMARY KEY REFERENCES todos(id) ON DELETE CASCADE,
    first_step         TEXT    NOT NULL,
    first_step_minutes REAL,
    steps              TEXT,   -- JSON array of the remaining ordered steps
    source             TEXT,   -- llm | heuristic
    done_steps         TEXT,   -- JSON array of completed step indices (0 = first_step)
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Feedback on a *dismissed* decomposition. A breakdown can be wrong two ways, and
-- the reason decides how it feeds learning (mirrors triage_feedback):
--   not_useful — the steps don't make sense / don't help. Folded back into the
--                decomposer prompt as negative examples ("avoid this style").
--   not_needed — the task didn't need breaking down at all. Repeated not_needed
--                dismissals suppress the auto-decompose on new todos (learning
--                *when not to* break down); on-demand "Break it down" still works.
--   llm_declined — the *model* judged the task not worth breaking down during the
--                avoided-task sweep. Recorded so the sweep doesn't re-ask, and as
--                a data point on the model's own judgments.
-- One row per decision — repetition is what makes a user signal reliable.
CREATE TABLE IF NOT EXISTS decomposition_feedback (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    todo_id          INTEGER REFERENCES todos (id),  -- provenance (the todo may later close)
    title            TEXT,                            -- todo title at dismiss time
    reason           TEXT NOT NULL,                   -- not_useful | not_needed
    source           TEXT,                            -- the dismissed breakdown's origin: llm | heuristic
    first_step       TEXT,                            -- snapshot of the dismissed first step
    steps            TEXT,                            -- JSON snapshot of the dismissed remaining steps
    category         TEXT,                            -- todo category at dismiss time
    estimate_minutes REAL,                            -- todo estimate (the auto-decompose discriminator)
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decomp_feedback_user
    ON decomposition_feedback (user_id, created_at);

-- User-curated named places — aliases that map a recurring destination to fixed
-- coordinates without any network geocoding (e.g. "gym", "the office", "mom's").
-- Checked against a commitment's location/title *before* the geocoder, so the
-- common destinations resolve instantly and offline. `name` is the normalized
-- match key; `label` keeps the original spelling for display.
CREATE TABLE IF NOT EXISTS places (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    name       TEXT    NOT NULL,           -- normalized match key (lowercased)
    label      TEXT,                       -- original display name
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name)
);

-- Geocode cache — a normalized free-text location string mapped to coordinates
-- (or a recorded miss: lat/lon NULL). Lets the calendar sync resolve the same
-- address once instead of re-calling the geocoder every poll, and keeps a
-- definitive "not found" so a bad address isn't retried forever. Local after the
-- first lookup; the network geocoder is only consulted on a cache miss.
-- The geocode cache is a pure address->coordinates lookup with no user data, so
-- it stays global (shared across users) — one lookup serves everyone.
CREATE TABLE IF NOT EXISTS geocode_cache (
    query        TEXT PRIMARY KEY,        -- normalized location string
    lat          REAL,                     -- NULL = looked up, not found
    lon          REAL,
    last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- == Shared household sheet (the Parent Context Pack backbone) ================
--
-- These three tables are household-scoped (household_id NOT NULL), NOT user-
-- scoped: two co-parents in one household see the same rows. Reached through the
-- household-scoped store methods (repos/household.py), which inject
-- `WHERE household_id = ?` the way the per-user methods inject `WHERE user_id`.
--
-- child_id convention: SQLite treats NULLs as distinct in a UNIQUE constraint,
-- which would let duplicate household-wide rows through. So household-wide
-- facts/agreements use the sentinel child_id = 0 (children.id is a positive
-- AUTOINCREMENT, so 0 never collides with a real child). See docs/household-sheet.md.

-- The household roster: stable identity only (name + birthday). Everything else
-- about a member is a fact (household_facts), not a column here. `kind` splits the
-- roster into the kids ('child', the default) and pets ('pet'); pets reuse the
-- same facts / appointments / shopping plumbing (all keyed by children.id) but are
-- surfaced under their own section. `species` labels a pet (dog/cat/…); NULL for a
-- child.
CREATE TABLE IF NOT EXISTS children (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    name         TEXT NOT NULL,
    birthday     DATE,
    kind         TEXT NOT NULL DEFAULT 'child',   -- 'child' | 'pet'
    species      TEXT,                            -- pet species (dog/cat/…); NULL for a child
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, name)
);

CREATE INDEX IF NOT EXISTS idx_children_household ON children (household_id);

-- The categorized key/value grid — sizes, routines, food/allergies, health,
-- school, contacts. Every write carries provenance (updated_by/updated_at): the
-- raw material for making invisible load legible and for the v2 delta digest.
CREATE TABLE IF NOT EXISTS household_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- children.id, or 0 = household-wide
    category     TEXT NOT NULL,                    -- controlled vocab (see FACT_CATEGORIES)
    item         TEXT NOT NULL,                    -- normalized field, e.g. "shoe size"
    value        TEXT,                             -- free text, e.g. "13"
    updated_by   INTEGER REFERENCES users(id),     -- provenance — who last set it
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, child_id, category, item)
);

CREATE INDEX IF NOT EXISTS idx_household_facts ON household_facts (household_id, child_id);

-- Standing behaviour plans both parents follow (so the kids get one answer, not
-- two). Light structure: a plain-language `body` plus an optional `structured`
-- JSON for star/points charts (thresholds -> rewards, earn-only rules).
CREATE TABLE IF NOT EXISTS household_agreements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- children.id, or 0 = whole household
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'consistency',  -- reward | consistency | routine
    body         TEXT,                             -- the plan in plain language (Markdown)
    structured   TEXT,                             -- optional JSON: star thresholds + prompt schedule
    updated_by   INTEGER REFERENCES users(id),
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_prompted_at DATETIME,                      -- last time the award prompt fired (dedup, see structured.prompt)
    UNIQUE (household_id, child_id, title)
);

CREATE INDEX IF NOT EXISTS idx_household_agreements ON household_agreements (household_id, child_id);

-- The star/points ledger behind an agreement's reward chart. An agreement's
-- `structured` JSON declares the *goals* (thresholds -> rewards); this table is
-- the running *earnings* against them — one row per grant, so who awarded what
-- (and when) stays legible, exactly like a fact's provenance. The running total
-- for a chart is SUM(delta) over its agreement_id; a threshold is "reached" when
-- a grant carries the total across it, which is when both parents get told.
-- `delta` is normally positive (earned); a negative delta is a correction and is
-- rejected by the write layer when the chart is earn_only. child_id mirrors the
-- owning agreement so the ledger never disagrees with the chart it belongs to.
CREATE TABLE IF NOT EXISTS household_stars (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    agreement_id INTEGER NOT NULL REFERENCES household_agreements(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- children.id, or 0 = whole household
    delta        INTEGER NOT NULL,                 -- stars earned (+) or corrected (-)
    note         TEXT,                             -- optional "what for" note
    awarded_by   INTEGER REFERENCES users(id),     -- provenance — which parent awarded it
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_household_stars ON household_stars (household_id, agreement_id);

-- LLM-as-sensor candidates (learning §2). The learning loop only learns from
-- signal already shaped as episodes / coaching_state; this widens what can be
-- observed by letting the model turn *free text* ("I always blow off admin on
-- Mondays") into a *candidate* structured update — a coaching_state key/value or
-- an episode. Crucially these never land authoritatively: a candidate sits
-- `pending` until a human accepts it (then applied with source 'llm_inferred',
-- distinct from 'inferred'/'explicit') or rejects it. The model proposes; the
-- deterministic/human-confirmed write path still decides. See prefrontal/sensor.py.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    kind        TEXT    NOT NULL,                       -- 'state' | 'episode'
    payload     TEXT    NOT NULL,                       -- JSON: the proposed update
    rationale   TEXT,                                   -- why (a quote/paraphrase of the text)
    source      TEXT    NOT NULL DEFAULT 'llm_inferred',
    status      TEXT    NOT NULL DEFAULT 'pending',     -- pending | accepted | rejected
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_proposals_user ON proposals (user_id, status);

-- Ambiguity clarifications (task-initiation lever; see prefrontal/clarify.py). A
-- vague todo/commitment title ("Tax", "Mom") stalls because it can't be named, so
-- the system proposes ONE clarifying question with a few candidate readings and
-- surfaces it inline. Like a sensor proposal it stays `pending` until the human
-- answers: `resolved` records the chosen reading in `answer` (+ a recognized
-- `task_type`, which unlocks a guided playbook overlay); `dismissed` marks the
-- item not-actually-ambiguous. The target is referenced loosely by
-- (target_type, target_id) — a calendar re-sync can replace a commitment row, so
-- there's deliberately no hard foreign key to it.
CREATE TABLE IF NOT EXISTS clarifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    target_type TEXT    NOT NULL,                       -- 'todo' | 'commitment'
    target_id   INTEGER NOT NULL,                       -- the item's row id (loose ref)
    title       TEXT    NOT NULL,                        -- snapshot of the title at detection
    question    TEXT    NOT NULL,                        -- the clarifying question shown inline
    options     TEXT    NOT NULL,                        -- JSON: [{label, task_type?}] candidate readings
    source      TEXT    NOT NULL DEFAULT 'heuristic',    -- llm | heuristic (how it was phrased)
    status      TEXT    NOT NULL DEFAULT 'pending',      -- pending | resolved | dismissed
    answer      TEXT,                                    -- the chosen reading (resolved)
    task_type   TEXT,                                    -- recognized playbook key (resolved), if any
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_clarifications_user ON clarifications (user_id, status);
-- At most one *pending* question per item; answered/dismissed rows don't block a
-- future one (though the sweep skips any item with history via clarified_target_ids).
CREATE UNIQUE INDEX IF NOT EXISTS idx_clarifications_pending
    ON clarifications (user_id, target_type, target_id) WHERE status = 'pending';

-- Weekly "how did the invisible load feel for you?" self-reports — one row per
-- parent per ISO week. Deliberately subjective and non-judgmental: we store how
-- each parent *felt* (light / balanced / heavy), never who did what, and surface
-- a gentle shared note once both have replied. Opt-in (households.checkin_*).
CREATE TABLE IF NOT EXISTS household_checkins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    week         TEXT NOT NULL,                    -- ISO week key, e.g. "2026-W27"
    user_id      INTEGER REFERENCES users(id),     -- which parent replied
    response     TEXT NOT NULL,                    -- 'light' | 'balanced' | 'heavy'
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, week, user_id)
);

CREATE INDEX IF NOT EXISTS idx_household_checkins ON household_checkins (household_id, week);

-- The shared shopping list — the template's "Shopping Lists" sheet. A running
-- list of things to buy, child-tagged, that either parent can add to and check
-- off (so nobody buys the same thing twice). Deliberately NOT per-user todos:
-- those are per-user-scoped and carry scheduling weight a shared checklist
-- doesn't want. Provenance on both add (added_by) and buy (got_by). See
-- docs/household-sheet.md.
CREATE TABLE IF NOT EXISTS household_shopping (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- children.id, or 0 = household-wide
    item         TEXT NOT NULL,                    -- what to buy
    spec         TEXT,                             -- size / brand / details
    where_to_buy TEXT,                             -- where to get it
    got          INTEGER NOT NULL DEFAULT 0,       -- 0 = still needed, 1 = bought
    added_by     INTEGER REFERENCES users(id),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    got_by       INTEGER REFERENCES users(id),     -- who checked it off
    got_at       DATETIME                          -- when; a bought row is swept ~24h after this
);

CREATE INDEX IF NOT EXISTS idx_household_shopping ON household_shopping (household_id, got);

-- Self-serve household invites. A member generates a short shareable code (also
-- rendered as a /kids?invite=CODE link); the other parent redeems it to join,
-- with no operator step. One-time (redeemed_by stamps who used it), expiring, and
-- revocable (delete an unredeemed row). See docs/household-sheet.md §8.
CREATE TABLE IF NOT EXISTS household_invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    code         TEXT NOT NULL UNIQUE,             -- short human-typeable code
    created_by   INTEGER REFERENCES users(id),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at   DATETIME NOT NULL,                -- redeemable until this (UTC)
    redeemed_by  INTEGER REFERENCES users(id),     -- who joined with it (NULL = unused)
    redeemed_at  DATETIME
);

CREATE INDEX IF NOT EXISTS idx_household_invites_code ON household_invites (code);

-- Routines — a named grouping of chores with ONE accountable owner (RACI "A":
-- the parent who holds the mental load and is answerable for the whole thing,
-- distinct from whoever is "responsible" for doing each chore). A routine
-- carries the schedule its chores inherit (days + optional due time), so
-- "Monday pickup prep" is defined once and every chore under it runs on that
-- cadence unless the chore overrides. Accountability itself is load — the
-- balance view counts it as a second facet ("carrying") alongside doing. See
-- docs/household-sheet.md.
CREATE TABLE IF NOT EXISTS household_routines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id   INTEGER NOT NULL REFERENCES households(id),
    title          TEXT NOT NULL,                    -- "Monday pickup prep"
    accountable_id INTEGER REFERENCES users(id),     -- RACI "A" — mental-load holder (NULL = unassigned)
    days           TEXT NOT NULL DEFAULT '',         -- weekday ints CSV "0,1,2"; empty = every day
    due_time       TEXT NOT NULL DEFAULT '',         -- "HH:MM" local, or '' = not time-tied
    impact         TEXT,                             -- why it matters ("mornings run late without it")
    enabled        INTEGER NOT NULL DEFAULT 1,       -- 0/1
    updated_by     INTEGER REFERENCES users(id),
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, title)
);

CREATE INDEX IF NOT EXISTS idx_household_routines ON household_routines (household_id, enabled);

-- Recurring shared chores — the "someone has to do this every day, and if it
-- slips it lands on the other parent" load. Unlike a star chart (about the
-- kids), a chore is about the *co-parents'* shared load: it's owned by one
-- member, recurs on chosen weekdays at a due time, and carries the plain reason
-- it matters ("makes the morning harder"). The notification flow reads it: a
-- lead-time reminder to the owner, and — if the due time passes undone — a gentle
-- heads-up to the *other* parent so the slip isn't a morning surprise. Completion
-- is logged per local day (household_chore_log) both for provenance ("who did
-- it") and to answer "done today?"; two date cursors on the row dedup the
-- reminder and the miss-handoff to once per local day, mirroring
-- household_agreements.last_prompted_at. See docs/household-sheet.md.
CREATE TABLE IF NOT EXISTS household_chores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id  INTEGER NOT NULL REFERENCES households(id),
    title         TEXT NOT NULL,                    -- "run the dishwasher"
    owner_id      INTEGER REFERENCES users(id),     -- RACI "R" — whose job it is to do it (NULL = either parent)
    routine_id    INTEGER REFERENCES household_routines(id),  -- part of a routine (NULL = stands alone)
    days          TEXT NOT NULL DEFAULT '',         -- weekday ints CSV "0,1,2"; empty = inherit routine / every day
    due_time      TEXT NOT NULL DEFAULT '',         -- "HH:MM" local; '' = inherit routine, or not time-tied
    remind_before INTEGER NOT NULL DEFAULT 30,      -- minutes before due to nudge the owner
    impact        TEXT,                             -- why it matters ("makes the morning harder")
    enabled       INTEGER NOT NULL DEFAULT 1,       -- 0/1
    updated_by    INTEGER REFERENCES users(id),
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_reminded_on TEXT,                           -- local "YYYY-MM-DD" the reminder last fired (dedup)
    last_missed_on   TEXT,                           -- local "YYYY-MM-DD" the miss-handoff last fired (dedup)
    UNIQUE (household_id, title)
);

CREATE INDEX IF NOT EXISTS idx_household_chores ON household_chores (household_id, enabled);

-- One row per chore per local day it was completed — the provenance ledger
-- behind "done today?" (like household_stars is behind a chart's total). done_by
-- records which parent actually did it, so the loop closes even when the *other*
-- parent picks up a slipped chore. The UNIQUE key makes "mark done" idempotent.
CREATE TABLE IF NOT EXISTS household_chore_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    chore_id     INTEGER NOT NULL REFERENCES household_chores(id),
    done_on      TEXT NOT NULL,                     -- local "YYYY-MM-DD" it was completed
    done_by      INTEGER REFERENCES users(id),      -- provenance — who actually did it
    done_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, chore_id, done_on)
);

CREATE INDEX IF NOT EXISTS idx_household_chore_log ON household_chore_log (household_id, chore_id, done_on);

-- Triage audit/idempotency log (docs/triage-agent.md §7.3). One row per triaged
-- inbound signal: what it was classified as, why, and the row it routed into.
-- Triage routes into the existing core tables (todos/commitments/episodes/state)
-- and needs only this ledger of its own. Per-user scoped like every other table;
-- the partial unique index makes re-delivery of the same signal (matched by
-- source + external_id) a no-op, so a flaky poll never double-creates a todo.
CREATE TABLE IF NOT EXISTS triage_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    received_at  DATETIME NOT NULL,
    source       TEXT NOT NULL,                     -- Signal.source
    external_id  TEXT NOT NULL DEFAULT '',          -- provider id, for idempotent re-delivery
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    urgency      TEXT NOT NULL,
    route        TEXT NOT NULL,
    reason       TEXT NOT NULL,                      -- always populated — never a silent drop
    confidence   REAL NOT NULL,
    decided_by   TEXT NOT NULL,                     -- "heuristic" | "llm"
    routed_ref   TEXT                               -- "todo:42" / "commitment:7" / NULL on drop
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_triage_external
    ON triage_log (user_id, source, external_id) WHERE external_id <> '';
CREATE INDEX IF NOT EXISTS idx_triage_user ON triage_log (user_id, received_at);

-- NOTE: the coaching_state defaults that used to be seeded here are now seeded
-- per user at provision time (DEFAULT_COACHING_STATE in store.py) plus each
-- enabled module's default_state — so "a fresh user looks like a fresh install"
-- is one code path. schema.sql stays purely structural.
