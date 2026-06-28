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
