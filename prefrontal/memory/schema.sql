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
--   focus_sessions       Hyperfocus deep-work blocks (protect / interrupt)
--   commitments          synced/manual schedule items (impact + double-booking)
--   todos                open loops fitted into free windows
--   todo_decompositions  tiny-first-step breakdown for stall-prone todos
--   dismissed_conflicts  soft double-bookings the user has waved off
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
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handle       TEXT    NOT NULL UNIQUE,        -- short login-ish name: 'tom', 'sam'
    display_name TEXT,                            -- shown in nudges/briefings
    token_hash   TEXT    NOT NULL UNIQUE,         -- sha256(token); raw token never stored
    status       TEXT    NOT NULL DEFAULT 'active', -- active | disabled
    is_operator  BOOLEAN NOT NULL DEFAULT 0,      -- may call admin endpoints
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_token ON users (token_hash);

-- Raw outcome records. One row per agent interaction cycle.
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    timestamp       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    episode_type    TEXT    NOT NULL,           -- departure | task | checkin | reminder
    predicted_value REAL,                       -- what the agent estimated
    actual_value    REAL,                       -- what actually happened
    acknowledged    BOOLEAN,                    -- did the user respond to the trigger?
    channel         TEXT,                       -- notification | sound | tts | sms
    context         TEXT,                       -- free text: location, time of day, task type
    outcome         TEXT,                       -- success | miss | partial
    notes           TEXT                        -- optional agent or user annotation
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
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outings_user_status ON outings (user_id, status);

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
    planned_minutes REAL,                              -- optional intended duration
    aligned         BOOLEAN NOT NULL DEFAULT 1,        -- is this the thing you meant to do?
    started_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status          TEXT    NOT NULL DEFAULT 'active',  -- active | ended | abandoned
    last_level      TEXT    NOT NULL DEFAULT 'none',    -- highest interrupt already fired
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

-- NOTE: the coaching_state defaults that used to be seeded here are now seeded
-- per user at provision time (DEFAULT_COACHING_STATE in store.py) plus each
-- enabled module's default_state — so "a fresh user looks like a fresh install"
-- is one code path. schema.sql stays purely structural.
