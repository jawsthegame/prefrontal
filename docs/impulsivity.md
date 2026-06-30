# Impulsivity module — design spec

Status: **proposed**. This is the implementation spec for promoting the
Impulsivity module ([`prefrontal/modules/impulsivity.py`](../prefrontal/modules/impulsivity.py))
from a declared stub into a wired, end-to-end feature. It supersedes the
module's three `planned` interventions and the roadmap's "derive `context_switch`
once switch events are captured" note. If this and the code/roadmap disagree,
this file wins until the work lands.

It follows the shape of the one fully-wired module today — the **Location-Aware
Task Anchor** ([`location_anchor.py`](../prefrontal/modules/location_anchor.py)):
a pure, testable core in the module file; a dedicated table; a small set of
webhook endpoints; an iOS Shortcut + n8n surface; and a feedback loop through
`learn`/`summarize` into the behavioral profile.

---

## 1. Goal & non-goals

**The challenge.** Impulsivity here is *acting before reflecting* — abruptly
context-switching away from the task you meant to be doing to chase a newer,
shinier one. The damage isn't the new idea (it's often good); it's the
unplanned **switch** that fragments a focus block and leaves the original loop
open. The clinically-grounded support pattern is **friction plus capture**: a
brief, dismissible pause between impulse and action, and a safe place to *park*
the impulse so the anxiety of "I'll forget this" stops driving the switch.

**Goal.** When the user is in a stated focus block and feels the pull to switch,
Prefrontal interposes a short reflective pause that re-surfaces what they meant
to be doing, and offers a one-tap **capture-and-defer** so the impulse is logged
(and safe to return to) without abandoning the current task. Over time it learns
the user's switch rate and honor/defer ratio and reflects them back.

**Non-goals (v1).**

- **No automatic detection of switches from device activity.** This is a
  self-hosted system with no OS-level app/window monitor and no screen-time
  hook. We do not try to *infer* a switch from what app is foregrounded. The
  switch is **declared** by the user (one tap), exactly as outings are declared
  in the anchor module. Passive detection is a "beyond v1" idea (§12).
- **No hard blocking.** Prefrontal cannot (and won't pretend to) lock an app or
  prevent the switch. The pause is advisory friction; "switch anyway" is always
  one tap away. Coercion is off-brand and would just get the feature disabled.
- **No AI judgment of whether an impulse is "worth it."** Like the anchor's v1,
  the logic is deterministic: pause, remind, offer to capture. The local LLM is
  used only to *phrase* the reminder and to title a captured impulse, both with
  heuristic fallbacks — never to gate the switch.
- **No new always-on poller.** The anchor polls because elapsed time advances on
  its own. Impulsivity is **interactive** — it acts only when the user signals
  an impulse — so it needs no n8n cron beyond the optional end-of-day digest.

**Success test.** A user starts a focus block ("writing the Q3 report, ~90
min"). Twenty minutes in they feel the urge to reorganize their reference
folder. They tap one Shortcut; within a second they see *what they were doing*
plus a short countdown, and a menu: **Return**, **Capture & defer**, **Switch
anyway**. Choosing "Capture & defer" parks "reorganize reference folder" in the
inbox and returns them to the report. At day's end the briefing notes "6
switch-impulses today, you deferred 4 — better than your 1.4/day baseline of
honoring them." After a week of data, `prefrontal learn` has a `context_switch`
pattern and the profile says something true about the user's switching.

---

## 2. Core concept: the focus session

To pause a *switch away from a task*, the system must know what the current task
**is**. The anchor module solved the analogous problem with the `outings` table
(a stated intention + a time window you're "out" against). Impulsivity needs the
desk-bound twin: a **focus session** — a stated intention you're working
*against*, that a switch-impulse fires relative to.

A focus session is deliberately close to an outing in shape (intention + planned
minutes + open/closed lifecycle) but distinct in meaning (you're *at* the task,
not *away from home*), so it gets its own table rather than overloading
`outings`. The two never interact.

```
focus/start  ─►  active focus session  ◄─ focus/switch (the interception point)
                        │                        │
                        │                        ├─► Return       (no-op, log honor=false later)
                        │                        ├─► Capture&defer (→ impulse capture / todo)
                        │                        └─► Switch anyway (close session, log honored switch)
                        │
                     focus/end ─► closed session → `task` + `switch` episodes → learn → profile
```

---

## 3. Decision: capture-and-defer reuses `todos`

The open design choice is where a captured impulse lands. Two options:

| | Reuse `todos` (chosen) | New `captures` table |
|---|---|---|
| The impulse becomes… | An open loop, `source='impulse'` | A separate inbox needing its own triage |
| Fit into free time | Free — `fit_todos()` already ranks it | Build a second fitter |
| Augment (estimate/priority/deadline) | Free — `augment_todo()` runs on it | Re-implement |
| Avoidance detection | Free — it's just another todo | Re-implement, or never |
| Risk | Inbox noise pollutes the todo list | Two lists to reconcile |

**Decision: a captured impulse is a `todo` with `source='impulse'`.** It slots
straight into the open-loop machinery the repo already has (augment, fit,
avoidance, the briefing's "spare time" section). This matches the avoidance
feature's ethos — *prefer reuse over new tracking*. The one schema cost is a
`source` column on `todos` (it has none today; `commitments` already has one),
defaulting `'manual'` so existing rows are unaffected.

The capture is intentionally *low-friction and lossy*: we store the raw impulse
text immediately and let the existing `augment_todo()` pass (LLM + heuristic
fallback) infer estimate/priority/deadline asynchronously, so the capture tap
never blocks on inference. A captured impulse the user later decides was noise
is just `todo … drop`.

> **Note on titling.** The raw impulse ("ooh the thing with the fonts") is kept
> verbatim in `notes`; the local LLM proposes a clean `title` with a heuristic
> fallback (first ~8 words), mirroring how `augment_todo` already degrades.

---

## 4. Schema changes

All changes stay in [`schema.sql`](../prefrontal/memory/schema.sql), which
remains idempotent (`CREATE TABLE IF NOT EXISTS`, additive columns). Mirrors the
`outings` table closely so the store code and tests are familiar.

### 4.1 New table: `focus_sessions`

```sql
-- Active and historical focus sessions: the task the user has declared they're
-- working on right now. The Impulsivity module fires switch-impulses against an
-- active session (a desk-bound twin of `outings`). One row per declared block.
CREATE TABLE IF NOT EXISTS focus_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    intention          TEXT    NOT NULL,            -- "writing the Q3 report"
    planned_minutes    REAL,                         -- optional stated duration
    started_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status             TEXT    NOT NULL DEFAULT 'active',  -- active | completed | switched | abandoned
    switch_impulses    INTEGER NOT NULL DEFAULT 0,   -- times a switch was signalled
    switches_deferred  INTEGER NOT NULL DEFAULT 0,   -- of those, captured-and-deferred
    ended_at           DATETIME,                     -- set when the session closes
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_focus_status ON focus_sessions (status);
```

`status` semantics: `completed` (ended normally), `switched` (closed *because*
the user chose "switch anyway"), `abandoned` (auto-closed stale, §7.4). The two
counters are what the end-of-day feedback and the learning pass read.

### 4.2 Additive column on `todos`

```sql
-- (added to the existing todos table)
ALTER TABLE todos ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';  -- manual | impulse
```

Because SQLite has no `ADD COLUMN IF NOT EXISTS`, the store applies this as a
guarded migration (read `PRAGMA table_info(todos)`; add only if absent), the
same defensive pattern any future additive change will use. `schema.sql` itself
carries the column on the `CREATE TABLE` for fresh installs.

### 4.3 Episodes & patterns (no DDL change)

Switches are logged as **episodes** with a new `episode_type = 'switch'`
(`episode_type` is free text; the comment enumerating types is updated for
docs). The learning pass derives a **`context_switch`** pattern — a
`pattern_type` the `patterns` table comment *already lists* and that nothing
populates yet. This is the loop the roadmap flagged as blocked on "switch events
being captured"; this spec captures them.

---

## 5. The pure core (in `impulsivity.py`)

Following `location_anchor.py`, the module file holds small, dependency-free,
unit-testable functions. No HTTP, no DB — those live in the webhook/store layers.

```python
#: Default reflective pause, in seconds (already a coaching_state default).
DEFAULT_PAUSE_SECONDS = 20.0

#: A switch this soon after starting a block is the most disruptive — scale the
#: pause up early in a session and down once the block is mostly spent.
def pause_seconds(elapsed_minutes, planned_minutes, base_seconds) -> float:
    """Reflective-pause length for a switch-impulse.

    Longer when the user is early in a focus block (most to lose by switching),
    shrinking toward `base_seconds` as the block nears its planned end. With no
    planned duration, returns `base_seconds` flat. Bounded to [base, 3*base].
    """

def switch_response(action: str) -> str:
    """Validate/normalize the chosen action: 'return' | 'defer' | 'switch'."""

def build_pause_message(intention, elapsed_minutes, *, name="") -> str:
    """The reminder shown during the pause:
    'You're 20 min into "writing the Q3 report." Sit with this for a moment.'"""

def heuristic_capture_title(impulse_text: str) -> str:
    """First ~8 meaningful words, title-cased — the fallback when the LLM is down
    (mirrors location_anchor.heuristic_time_window's role)."""

def infer_capture_title(impulse_text, *, client=None, fallback=True):
    """LLM-titled capture with heuristic fallback (mirrors infer_time_window)."""

def switch_rate_feedback(sessions: list[dict]) -> str | None:
    """End-of-day line from today's closed sessions: total impulses, how many
    deferred vs honored, compared to the learned baseline. None if no data."""
```

These are pure functions over plain values/dicts, so the test file
(`tests/test_impulsivity.py`) can exercise every branch the way
`tests/test_location_anchor.py` does for escalation — no server, no DB fixture
beyond an in-memory store for the few store methods.

---

## 6. Store methods (`memory/store.py`)

Thin CRUD mirroring the `start_outing` / `active_outings` / `close_outing`
family:

- `start_focus(intention, planned_minutes=None) -> int`
- `active_focus_session() -> dict | None` — at most one active at a time;
  starting a new one auto-closes any prior active session as `abandoned`
  (you can't be in two focus blocks). Returns the row with a computed
  `elapsed_minutes`, like `active_outings()` does.
- `record_switch_impulse(session_id, *, deferred: bool) -> None` — increments
  `switch_impulses` and, if deferred, `switches_deferred`.
- `close_focus(session_id, status) -> dict` — sets `ended_at`, returns the
  closed row with computed `actual_minutes`.
- `recent_focus_sessions(limit=50)` and `todays_focus_sessions()` — for the
  profile section and the end-of-day feedback.
- `add_todo(..., source='manual')` gains the `source` kwarg; `open_todos()` /
  the `GET /todos` serializer surface it.

The "one active session" invariant is enforced in the store (not the endpoint)
so the CLI and any future caller get it for free — same place the anchor would
put it.

---

## 7. HTTP surface (`webhooks/app.py`)

New router tagged `impulsivity`, modeled on the `anchor` endpoints. All require
the bearer token like every other webhook.

### 7.1 `POST /webhooks/focus/start`

Body: `{ "intention": str, "planned_minutes": float | null }`. Starts a session
(auto-closing any stale active one). Returns `{ session_id, intention,
planned_minutes }`. This is interactive (an iOS Shortcut waits on it), so it
shares the anchor's short LLM timeout budget — but it does no inference, so it's
effectively instant.

### 7.2 `POST /webhooks/focus/switch` — the interception point

Body: `{ "session_id": int | null }` (defaults to the active session). The
moment the user feels the pull and taps "Switching?". Response:

```jsonc
{
  "session_id": 7,
  "intention": "writing the Q3 report",
  "elapsed_minutes": 20,
  "pause_seconds": 28,                 // from pause_seconds(), client honors this
  "message": "You're 20 min into \"writing the Q3 report.\" Sit with this for a moment.",
  "options": ["return", "defer", "switch"]
}
```

This call **does not** change state on its own — it records the *impulse*
(`record_switch_impulse(deferred=False)` increments the impulse counter) and
returns the directive. The user's subsequent choice is a separate call (7.3) so
a dropped connection mid-pause can't strand the session. If there's no active
session, returns `409` with a gentle "no focus block is running — start one
first?" (the Shortcut offers to start one).

### 7.3 `POST /webhooks/focus/resolve`

Body: `{ "session_id": int | null, "action": "return"|"defer"|"switch",
"impulse_text": str | null }`.

- **return** — no state change beyond what 7.2 logged; the impulse stands as
  *honored=false* (friction worked). 
- **defer** — `record_switch_impulse(deferred=True)`, then create a todo with
  `source='impulse'` from `impulse_text` (titled via `infer_capture_title`,
  raw text in `notes`), kicking the existing `augment_todo` path. Returns the
  new `todo_id`. *This is the capture-and-defer intervention.*
- **switch** — `close_focus(status='switched')`; the user is moving on
  deliberately. The closed session logs its episodes (§8).

A bare **capture without a switch context** is also useful (an impulse arrives
when no block is running). `POST /webhooks/focus/capture` `{ "impulse_text" }`
creates the `source='impulse'` todo directly — the same logic 7.3/defer reuses.

### 7.4 `POST /webhooks/focus/end` and `GET /focus`

- `focus/end` `{ session_id?, status='completed' }` — close a session you
  finished. Logs episodes (§8).
- `GET /focus` — the active session (intention, `elapsed_minutes`,
  `planned_minutes`, today's `switch_impulses`/`switches_deferred`), for the
  dashboard and a future widget — exactly parallel to `GET /outings`.

**Staleness.** A session left `active` far past its planned duration is
auto-closed as `abandoned` the next time any focus endpoint is hit (or by the
optional end-of-day job), mirroring the anchor's `abandon_after_ratio`
auto-close so sessions don't linger forever. Default ratio reuses a new
`focus_abandon_after_ratio` state key (default `4.0`); a windowless session uses
a flat `focus_default_minutes` (default `60`) only for the staleness clock,
never for a nudge.

---

## 8. Learning loop — capturing `context_switch`

This is the piece the roadmap said was blocked. On `close_focus`, the module
logs episodes (a `record_focus_close()` helper alongside the anchor's
`record_outing_return`):

- One `task` episode: `predicted_value=planned_minutes`,
  `actual_value=actual_minutes`, `outcome='success'` if the block ran to/over
  plan without a honored switch, else `partial`/`miss`. This feeds
  `time_estimation` like outings do — focus blocks are time estimates too.
- One `switch` episode **per session** carrying the session's switch counts:
  `predicted_value=switch_impulses`, `actual_value=switches_deferred`,
  `context='focus: <intention>'`. (Per-session rather than per-impulse keeps the
  episodes table proportional to sessions, and the counts are what the pattern
  needs.)

Then `patterns.py` gains a derivation (the roadmap's "derive `context_switch`
once switch events are captured"): over recent `switch` episodes, compute

- `observed_value` = mean switch-impulses per session (or per day, bucketed by
  `context_key`),
- `predicted_value` = mean deferred,
- so `variance` ≈ honor rate, and
- `confidence = n/(n+k)` exactly like the other patterns.

`prefrontal learn` already iterates pattern derivations; this slots in beside
`time_estimation`/`channel_response`/`drift` with no new command.

---

## 9. Profile section

`ImpulsivityModule.profile_section` is upgraded from "echo the configured pause"
to read real data, the way hyperfocus/anchor do:

- The configured pause and capture-then-defer stance (kept).
- From the `context_switch` pattern(s): "~5 switch-impulses per focus block;
  you defer ~60% (confidence 72%)." — only once confidence crosses a small
  floor, else stay quiet (return `None`-friendly), matching the repo's
  "don't assert low-confidence patterns" habit.
- From recent sessions: median focus-block length actually sustained before a
  honored switch — a concrete, motivating number.

This prose flows into `profile.md` via the summarizer and thus into every
agent's system prompt, so other modules' coaching is aware of the user's
switching tendency.

---

## 10. Surfaces: iOS Shortcuts, briefing, n8n, dashboard

**iOS Shortcuts** (the primary surface — this module is interactive). Two
shortcuts, documented in `deploy/ios-shortcut.md` alongside the existing
"Going out"/"I'm back":

1. **"Start focus"** — asks for the intention (and optionally minutes), POSTs
   `/focus/start`. One tap to declare what you're doing.
2. **"Switching?"** — the friction tap. POSTs `/focus/switch`, shows the
   returned `message`, runs a **Wait** action for `pause_seconds` (the pause is
   delivered client-side; the server only advises the duration), then presents a
   menu **Return / Capture & defer / Switch anyway**. "Capture & defer" asks for
   one line of text and POSTs `/focus/resolve` with `action=defer`. The whole
   thing is ~4 Shortcut actions and is the entire UX of the reflective pause.

**Morning briefing.** `briefing.py` gains a one-line `switch_rate_feedback()`
slice — "Yesterday: 6 switch-impulses, 4 deferred (baseline honors ~1.4/day)" —
in the same spirit as the avoidance section. This realizes the module's third
declared intervention, `switch_rate_feedback`, attached to the day's check-in.

**n8n (optional).** No new poller is required. An optional
`deploy/n8n/focus-end-of-day.workflow.json` cron can call a `GET /focus/digest`
(or read the briefing) and Pushover the switch-rate line in the evening — purely
additive, off by default.

**Dashboard.** `/dashboard` gains an "In focus" card when a session is active
(intention + elapsed + today's deferred/honored), reading `GET /focus`, parallel
to how it shows active outings.

---

## 11. Module wiring & config

- Flip the three `Intervention`s in `ImpulsivityModule.interventions()` from
  implicit `planned` to `status="active"` as each lands (`reflective_pause`,
  `capture_and_defer`, `switch_rate_feedback`), so `prefrontal modules -v`
  reports the real state — the same honesty the anchor's intervention list shows.
- `default_state` already carries `pause_seconds` (20) and `capture_then_defer`
  (true). Add `focus_abandon_after_ratio` (4.0) and `focus_default_minutes`
  (60). All seeded via the existing `Module.seed`, never clobbering user values.
- No config/registry changes — the module is already registered and selectable
  via `PREFRONTAL_MODULES`. When the module is **disabled**, the `/focus/*`
  endpoints should 404/short-circuit (check `enabled_modules`), so a disabled
  module is truly inert.

---

## 12. Testing

A `tests/test_impulsivity.py` mirroring `test_location_anchor.py`:

- **Pure core:** `pause_seconds` scaling and bounds (early vs late vs no plan);
  `build_pause_message` formatting; `heuristic_capture_title` truncation;
  `infer_capture_title` LLM-then-fallback (inject a fake/raising client like the
  anchor's `_Generator` tests); `switch_rate_feedback` text and the empty case.
- **Store:** start→active→one-active-invariant (starting a second abandons the
  first); `record_switch_impulse` counters; `close_focus` statuses and computed
  `actual_minutes`; the guarded `todos.source` migration is a no-op on a DB that
  already has the column.
- **Learning:** a sequence of closed sessions produces `switch` episodes and a
  `context_switch` pattern with the expected `observed_value`/confidence.
- **HTTP:** `focus/start` → `focus/switch` (asserts no state mutation beyond the
  impulse count, correct `pause_seconds`) → `focus/resolve` for each action
  (defer creates a `source='impulse'` todo; switch closes the session); 409 when
  switching with no active session; endpoints 404 when the module is disabled.

---

## 13. Migration & rollout

1. **Schema** — `focus_sessions` is new (idempotent create). `todos.source` is
   the only change to an existing table; applied as a guarded `ADD COLUMN` so
   existing single-tenant DBs upgrade in place with no data touched. `prefrontal
   init-db` is safe to re-run, as today.
2. **Backwards-compatible** — every change is additive. Disabling the module (or
   not enabling it) leaves the rest of the system byte-for-byte as it is now.
3. **Ship order** (each independently useful):
   1. Schema + store methods + pure core + tests (no surface yet).
   2. `/focus/*` endpoints + the two iOS Shortcuts → the live reflective-pause
      loop. *This is the first real-world test, à la the Coffee Shop Nudge.*
   3. Learning derivation + profile section (needs a few days of data first).
   4. Briefing line + optional n8n digest + dashboard card.

---

## 14. Open questions

- **Pause delivery.** v1 puts the pause client-side (a Shortcut **Wait**), which
  is honest and simple but skippable. Is that enough friction, or should the
  server withhold the "switch anyway" affordance until `pause_seconds` has
  elapsed (timestamp the `/focus/switch` response, reject an early `resolve
  action=switch`)? Server-enforced is stronger but adds round-trip state. Lean:
  start client-side, measure honor rate, tighten only if switches are rubber-
  stamped.
- **Impulse inbox vs todo list noise.** Reusing `todos` is high-reuse but a burst
  of captured impulses could swamp the open-loop list. Mitigation if it bites: a
  `GET /todos?source=impulse` filter and a dashboard "Inbox" sub-list, or a
  weekly "triage your captures" briefing nudge — both additive, deferred until
  observed.
- **Per-session vs per-impulse switch episodes.** §8 logs one `switch` episode
  per session for table economy. If finer timing analysis is ever wanted (e.g.
  *when* in a block switches cluster), switch to per-impulse — the counters make
  either reconstructable, so this is reversible.
- **Passive detection (beyond v1).** A Focus/Screen-Time or Home-Assistant signal
  could *prompt* "looks like you switched apps — capture that?" without blocking.
  Explicitly out of scope here; noted so the declared-switch design doesn't
  preclude it.
</content>
</invoke>
