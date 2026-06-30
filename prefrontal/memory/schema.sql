-- Prefrontal behavioral memory schema.
--
-- The canonical, executable definition of the memory layer. The human-readable
-- companion lives in docs/schema.md; if the two disagree, THIS file wins.
--
-- Three core tables hold the learning loop:
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
-- Everything is idempotent: CREATE TABLE IF NOT EXISTS plus INSERT OR IGNORE on
-- the unique coaching_state.key, so applying this file repeatedly is safe.

PRAGMA foreign_keys = ON;

-- Raw outcome records. One row per agent interaction cycle.
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes (timestamp);

-- Derived summaries. Updated periodically by the summarizer agent from episodes.
CREATE TABLE IF NOT EXISTS patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type    TEXT    NOT NULL,           -- time_estimation | channel_response | drift | context_switch
    context_key     TEXT    NOT NULL,           -- what this pattern applies to (departure, morning, work_block)
    observed_value  REAL,                       -- average or median observed
    predicted_value REAL,                       -- what was being estimated
    variance        REAL,                       -- difference; positive means underestimate
    sample_size     INTEGER DEFAULT 0,          -- number of episodes this is derived from
    confidence      REAL    DEFAULT 0.0,        -- 0.0-1.0, low until sample size is meaningful
    last_updated    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- One row per (pattern_type, context_key) so the summarizer can upsert in place.
    UNIQUE (pattern_type, context_key)
);

-- Persistent preferences and working memory for the coaching layer.
CREATE TABLE IF NOT EXISTS coaching_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT    NOT NULL UNIQUE,       -- preference name
    value        TEXT,                          -- current value
    last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source       TEXT                           -- inferred | explicit
);

-- Active and historical "outings": a stated intention plus a time window, used
-- by the Location-Aware Task Anchor module to nudge the user back on track. One
-- row per declared outing ("getting coffee, back in 15 min").
CREATE TABLE IF NOT EXISTS outings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE INDEX IF NOT EXISTS idx_outings_status ON outings (status);

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

CREATE INDEX IF NOT EXISTS idx_focus_sessions_status ON focus_sessions (status);

-- Upcoming commitments — the schedule the system reasons about (e.g. for impact
-- analysis: "running over now puts your 10:30 at risk"). Populated from a
-- calendar by the sync endpoint, or added manually. `lead_minutes` is the
-- travel+prep buffer needed before `start_at`; `hardness` distinguishes a hard
-- deadline from a soft one. `external_id` is the calendar event id, used to
-- upsert on re-sync (partial-unique so manual rows can omit it).
CREATE TABLE IF NOT EXISTS commitments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT,                              -- calendar event id (NULL for manual)
    title        TEXT    NOT NULL,
    start_at     DATETIME NOT NULL,                 -- stored normalized to UTC
    end_at       DATETIME,
    location     TEXT,                              -- free-text location (e.g. "123 Main St")
    dest_lat     REAL,                              -- optional destination coordinates, used to
    dest_lon     REAL,                              --   estimate travel time for departure reminders
    lead_minutes REAL    NOT NULL DEFAULT 10,       -- travel+prep buffer before start
    hardness     TEXT    NOT NULL DEFAULT 'soft',   -- hard | soft
    source       TEXT    NOT NULL DEFAULT 'calendar', -- calendar | manual
    status       TEXT    NOT NULL DEFAULT 'active',   -- active | cancelled
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_commitments_external
    ON commitments (external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commitments_start ON commitments (start_at);

-- Open loops — "todos" that aren't pinned to a clock time but need doing
-- (call the dentist, plan a birthday). The scheduler fits these into free
-- windows between commitments. `estimate_minutes` is how long it'll take;
-- `priority` 0=low…3=urgent; `energy` and `deadline` are optional hints.
CREATE TABLE IF NOT EXISTS todos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (status);

-- Dismissed "possible conflicts" — soft double-bookings (a generic Busy/Block/
-- Hold overlapping a real event) the user has waved off. Keyed by a signature
-- of the event pair (external_id + start + title), so a dismissal sticks across
-- re-syncs but lapses if either event moves or is retitled (the key changes).
CREATE TABLE IF NOT EXISTS dismissed_conflicts (
    signature    TEXT PRIMARY KEY,
    dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    UNIQUE (account, message_id)
);

CREATE INDEX IF NOT EXISTS idx_mail_account ON mail_messages (account);
CREATE INDEX IF NOT EXISTS idx_mail_needs_action ON mail_messages (needs_action);
CREATE INDEX IF NOT EXISTS idx_mail_received ON mail_messages (received_at);

-- Cached LLM profile narrative — the prioritized coaching prose produced by the
-- summarizer (prefrontal/memory/summarizer.py). Generating it needs an Ollama
-- (or, later, Anthropic) round-trip, which is too slow to run on every
-- `GET /profile` poll, so the nightly `prefrontal summarize` writes it here and
-- the endpoint serves it. One row (id = 1). `structured_hash` is a fingerprint of
-- the structured profile the prose was derived from, so callers can tell when the
-- cache has gone stale (the underlying patterns/state changed) without a full
-- copy comparison.
CREATE TABLE IF NOT EXISTS profile_cache (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- single-row cache
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
    name       TEXT    NOT NULL UNIQUE,   -- normalized match key (lowercased)
    label      TEXT,                       -- original display name
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Geocode cache — a normalized free-text location string mapped to coordinates
-- (or a recorded miss: lat/lon NULL). Lets the calendar sync resolve the same
-- address once instead of re-calling the geocoder every poll, and keeps a
-- definitive "not found" so a bad address isn't retried forever. Local after the
-- first lookup; the network geocoder is only consulted on a cache miss.
CREATE TABLE IF NOT EXISTS geocode_cache (
    query        TEXT PRIMARY KEY,        -- normalized location string
    lat          REAL,                     -- NULL = looked up, not found
    lon          REAL,
    last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Seed rows. INSERT OR IGNORE keeps these as defaults without clobbering any
-- value the user or agent has since changed.
INSERT OR IGNORE INTO coaching_state (key, value, source) VALUES
    ('preferred_briefing_format', 'short',                    'explicit'),
    ('escalation_delay_minutes',  '5',                        'inferred'),
    ('responsive_hours_start',    '08:00',                    'inferred'),
    ('responsive_hours_end',      '14:00',                    'inferred'),
    ('preferred_reminder_channel','notification',             'inferred'),
    ('time_estimation_bias',      '1.4',                      'inferred'),
    ('active_escalation_path',    'notification,sound,tts',   'explicit'),
    -- Departure-reminder tuning (see prefrontal.modules.departure). Travel time
    -- is estimated locally: straight-line distance * road_factor / speed, then
    -- padded by time_estimation_bias and a prep buffer.
    ('travel_speed_kmh',          '30',                       'inferred'),
    ('travel_road_factor',        '1.3',                      'inferred'),
    ('departure_prep_minutes',    '5',                        'inferred'),
    ('departure_heads_up_minutes','30',                       'inferred'),
    ('departure_soon_minutes',    '10',                       'inferred'),
    -- Opt-in network geocoding (Nominatim) for commitment destinations. Off by
    -- default: local-first stays the default, and curated `places` + the static
    -- `lead_minutes` fallback work without it. Set to '1' to allow the calendar
    -- sync to resolve free-text locations to coordinates.
    ('geocoding_enabled',         '0',                        'explicit');
