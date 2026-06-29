-- Prefrontal behavioral memory schema.
--
-- The canonical, executable definition of the memory layer. The human-readable
-- companion lives in docs/schema.md; if the two disagree, THIS file wins.
--
-- Three tables:
--   episodes        raw outcome records, one per agent interaction cycle
--   patterns        derived summaries computed from episodes by the summarizer
--   coaching_state  persistent key/value preferences and working memory
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
    location     TEXT,
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

-- Seed rows. INSERT OR IGNORE keeps these as defaults without clobbering any
-- value the user or agent has since changed.
INSERT OR IGNORE INTO coaching_state (key, value, source) VALUES
    ('preferred_briefing_format', 'short',                    'explicit'),
    ('escalation_delay_minutes',  '5',                        'inferred'),
    ('responsive_hours_start',    '08:00',                    'inferred'),
    ('responsive_hours_end',      '14:00',                    'inferred'),
    ('preferred_reminder_channel','notification',             'inferred'),
    ('time_estimation_bias',      '1.4',                      'inferred'),
    ('active_escalation_path',    'notification,sound,tts',   'explicit');
