# ADHD Agent Memory Schema

## Overview

Three SQLite tables covering episodic memory, behavioral patterns, and coaching state. A summarizer agent compresses these into a profile document injected into every agent's system prompt.

The canonical, executable definition of this schema lives in
[`prefrontal/memory/schema.sql`](../prefrontal/memory/schema.sql). This document is the
human-readable companion — if the two ever disagree, the `.sql` file is the source of truth.

---

## Tables

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

> **Module-contributed keys.** Each enabled challenge-area module
> (`prefrontal/modules/`) seeds its own additional `coaching_state` rows when the
> database is initialized — e.g. `departure_buffer_minutes` (time blindness),
> `max_first_step_minutes` (task paralysis), `hyperfocus_block_minutes` /
> `protect_aligned_hyperfocus` (hyperfocus), `pause_seconds` (impulsivity).
> Seeding never clobbers an existing value. Run `prefrontal modules -v` to see
> what's active.

---

## Additional tables

Beyond the three core tables above, the schema (`prefrontal/memory/schema.sql`)
also defines:

- **`outings`** — active/historical "task anchors" (a stated intention + time
  window) for the Location-Aware Task Anchor module.
- **`commitments`** — upcoming schedule items synced from calendars (or added
  manually), used for double-booking detection and impact analysis.
- **`todos`** — open loops (not pinned to a clock time) with an estimate and
  priority, fitted into free windows between commitments (`prefrontal/scheduling.py`).

---

## System Prompt Injection

A summarizer agent runs periodically and writes a `profile.md` from the above tables. Every agent prepends this to its system prompt.

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
