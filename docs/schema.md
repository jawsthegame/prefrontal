# ADHD Agent Memory Schema

## Overview

Three **core** tables cover episodic memory, behavioral patterns, and coaching
state — the heart of the learning loop. A growing set of **feature** tables
(outings, focus sessions, commitments, todos, mail, and supporting caches) back
the individual modules and ingestion paths; they are documented under
[Additional tables](#additional-tables). A summarizer agent compresses memory
into a profile document injected into every agent's system prompt.

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
| `travel_speed_kmh` | `30` | inferred (departure-reminder travel estimate) |
| `travel_road_factor` | `1.3` | inferred (straight-line → road distance) |
| `departure_prep_minutes` | `5` | inferred (buffer added to travel) |
| `departure_heads_up_minutes` | `30` | inferred (gentle "leave soon" horizon) |
| `departure_soon_minutes` | `10` | inferred ("get ready" horizon) |
| `geocoding_enabled` | `0` | explicit (opt-in network geocoding; off by default) |

> **Runtime keys (not seeded).** `POST /webhooks/location` writes the phone's
> last-known position as `last_location_lat`, `last_location_lon`, and
> `last_location_accuracy_m`; the latitude row's `last_updated` is its freshness.
> The outing check and departure check read these when a poll body omits explicit
> coordinates. `last_departure_signature` records the last fired
> `(commitment, level)` so a standing departure reminder doesn't re-alert.

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
- **`focus_sessions`** — active/historical deep-work blocks (a stated task, an
  optional planned duration, and an `aligned` "is this what I meant to do?" bit)
  for the Hyperfocus module. Drives the asymmetric protect-vs-interrupt logic: an
  aligned block is shielded from other modules' nudges until it overruns its plan
  (a gentle check) or passes the hard ceiling (a biological break).
- **`commitments`** — upcoming schedule items synced from calendars (or added
  manually), used for double-booking detection and impact analysis. Optional
  `dest_lat`/`dest_lon` enable a local travel-time estimate for departure
  reminders (`prefrontal/departure.py`); without them the static `lead_minutes`
  buffer is used.
- **`todos`** — open loops (not pinned to a clock time) with an estimate and
  priority, fitted into free windows between commitments (`prefrontal/scheduling.py`).
- **`todo_decompositions`** — one row per todo big enough to stall on: a tiny
  first step (≤ `max_first_step_minutes`) plus the remaining steps as JSON, the
  task-initiation lever for the Task Paralysis module (`prefrontal/todos.py`).
- **`dismissed_conflicts`** — soft double-bookings the user has waved off, keyed
  by a signature of the event pair so a dismissal sticks across calendar re-syncs
  but lapses if either event moves (`prefrontal/commitments.py`).
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
