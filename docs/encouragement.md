# Encouragement & recovery layer — design spec

Status: **built** (steps 1–4 of §11). Shipped: the standalone core
(`prefrontal/encouragement.py` — `assess_day`, `build_recovery`,
`render_encouragement`, `summarize_encouragement`, the debounce cursor), the
`GET /encouragement` + `POST /encouragement/sent` surface, the `prefrontal
encourage` CLI, and `deploy/n8n/encouragement.workflow.json`; covered by
`tests/test_encouragement.py`, off by default (the `encouragement` key). The
acute-overwhelm case ships separately as **panic mode** (`GET /panic`,
`POST /webhooks/panic/check`). **Not yet built:** the morning-briefing tone
variant (§6.2, step 5) and wrapping `assess_day` as a coaching-agent cue producer
(coaching-agent §9). This is the implementation spec for the layer; it supersedes
the short "Encouragement & recovery layer" sketch in
[`ROADMAP.md`](../ROADMAP.md). If this and the roadmap disagree, this file wins.

---

## 1. Goal & non-goals

**Goal.** When a day is going badly, shift Prefrontal's tone from *nudging* to
*reassurance + recovery*. A deterministic detector watches the signals Prefrontal
already computes — what slipped, rising drift, a missed *hard* commitment,
outings run long, important todos being avoided — and, once a day crosses a
"rough" threshold, emits a message that (a) acknowledges the rough day without
judgment and (b) gives a concrete get-back-on-track plan: re-fit the remaining
todos into the day's free windows, suggest deferring low-stakes commitments, and
name one small next step.

The whole point of the system is to nudge; this layer is the counterweight that
stops a pile-up of nudges from becoming a pile-on. It is the recovery half of the
coaching agent the README describes.

**Non-goals (v1).**

- **Not a challenge-area module.** The README modules each address one
  executive-function challenge (time blindness, task paralysis, …). Encouragement
  is a cross-cutting *response to outcomes*, so it lives as a standalone pure-core
  layer next to `briefing.py` / `impact.py`, not as a `Module` subclass. (It
  *reads* signals that modules influence; it doesn't own coaching state the way a
  module does, beyond its own opt-in/threshold keys — §7.)
  > **Relationship to the coaching agent.** [`docs/coaching-agent.md`](coaching-agent.md)
  > §9 frames this same layer as one more `evaluate`-style *cue source* inside the
  > (not-yet-built) coaching agent. This spec ships it **standalone first** — its
  > own core + `GET /encouragement` — so it can land before that engine exists.
  > The designs agree on behavior; when the coaching agent lands, `assess_day`
  > becomes a cue producer so delivery/channel/debounce route through the shared
  > engine. Either way there is **one** `assess_day`, not two implementations.
- **No new data sources.** Every input is already in the DB. If a signal isn't
  computed today, it's out of scope (noted in §3).
- **No new ML.** The trigger and the recovery plan are deterministic; the only
  model use is optional prose phrasing with a heuristic fallback, exactly like the
  briefing summarizer.
- **Not always-on cheerleading.** Off by default, opt-in per user, tone-calibrated
  to coaching prefs, and debounced to at most once per day (§5, §7). It must never
  become saccharine for users who don't want it.
- **No autonomous rescheduling.** The plan *suggests* deferrals and a first step;
  it doesn't move commitments or close todos on its own. (Acting on a suggestion
  goes through the existing todo/commitment endpoints.)

**Success test.** On a day with a missed hard commitment plus two `miss` episodes,
`GET /encouragement` returns `rough: true`, a warm acknowledgement, a re-fit of
the still-open todos into this afternoon's free windows, one named small step, and
fires its delivery webhook **once**; a second poll the same day returns
`already_sent: true` and does not re-deliver. On a normal day it returns
`rough: false` with no message.

---

## 2. Shape: a pure core + an optional prose pass

Mirror the established two-layer pattern (`briefing.py`, `summarizer.py`):

1. **`assess_day(store, now)` → `DayAssessment`** — deterministic. Reads the
   signals, scores the day, decides `rough`.
2. **`build_recovery(store, assessment, now)` → `RecoveryPlan`** — deterministic.
   Turns a rough assessment into concrete recommendations (re-fit, defer, first
   step).
3. **`render_encouragement(assessment, plan)` → `str`** — deterministic Markdown,
   tone-calibrated from coaching state. Testable, no model.
4. **`summarize_encouragement(store, …) → EncouragementResult`** — optional. Runs
   the rendered text through Ollama for warmer prose, falling back to the
   deterministic text on any model failure (`source: "llm" | "heuristic"`),
   identical in structure to `summarize_briefing` (`briefing.py:260`).

New file: `prefrontal/encouragement.py`. Pure functions; the endpoint, CLI, and
n8n wire it to live data and delivery. No store-method changes beyond two new
reads/writes for the debounce cursor (§5).

---

## 3. Signals (all already computed)

`assess_day` reads **today-scoped** signals (the day is going badly *now* — not a
7-day retrospective like the briefing's "slipped"). `now` is naive UTC per
`impact.utcnow()`; "today" is `[midnight, now]`.

| Signal | Source today | Weight | Notes |
|---|---|---|---|
| Missed **hard** commitment | `commitments` with `hardness="hard"` whose `start_at − lead_minutes` is past **and** the linked outing/episode resolved `miss` (or `impact.analyze_impact` flags it `at_risk` with no slack left) | **high** | The heaviest signal — a hard thing was actually blown. |
| `miss` episodes today | `store.episodes_since(midnight)` filtered to `outcome=="miss"` (same filter the briefing uses, `briefing.py:109`) | medium, per miss | Late departures, abandoned outings, ignored reminders. |
| Outings run long | today's outings auto-closed `abandoned` (`abandon_after_ratio`, ROADMAP §"Module 1") or returned with `actual ≫ window` | medium, per outing | Overruns compound time-blindness; already logged as drift `miss`. |
| Rising drift | `patterns` rows `pattern_type="drift"` with `observed_value` above a baseline and non-trivial `confidence` (`patterns.py:234`) | low (modifier) | Background trend, not a today-event — caps in, doesn't dominate. |
| Unresolved double-bookings today | `commitments.find_conflicts(today)` (the briefing's `conflicts`) | low | A structurally overloaded day. |
| Avoidance pile-up | `todos.avoided_todos(open_todos, now)` top scores (`todos.py:420`) | low | Important things repeatedly skipped — feeds the *plan* more than the *score*. |

**Out of scope (not computed today):** mood/self-report, sleep, anything needing a
new ingestion source. The detector uses only the rows above.

### 3.1 Scoring

```python
DEFAULT_ROUGH_THRESHOLD = 3.0
SIGNAL_WEIGHTS = {
    "missed_hard": 3.0,    # one missed hard commitment alone trips the threshold
    "miss_episode": 1.0,   # per miss today
    "long_outing": 1.0,    # per overrun/abandoned outing today
    "conflict": 0.5,       # per unresolved double-booking
}
```

- `rough_score = Σ(weight × count)`, plus a small drift **modifier** (e.g.
  `+0.5` when any `drift` pattern is meaningfully above baseline) that can nudge a
  borderline day over but can't trip the threshold on its own.
- `rough = rough_score >= threshold`, where the threshold is the
  `encouragement_threshold` coaching key (default `3.0`), so a single missed hard
  commitment, or three smaller misses, qualifies — but one stray late reply does
  not.
- `DayAssessment` carries the raw `signals` (each `{kind, detail, weight}`), the
  `rough_score`, the `rough` bool, and the contributing `episode`/`commitment`
  ids, so the endpoint and tests can assert *why* a day was flagged rather than
  just the boolean.

The weights/threshold are deliberately simple and tunable; the design intent is
"acknowledge a genuinely bad day," not "react to every miss."

---

## 4. The recovery plan

`build_recovery(store, assessment, now)` produces a `RecoveryPlan` with three
parts, each built **only** from logic that already exists:

1. **Re-fit the rest of the day.** Compute the free windows from `now` to the
   day's available-hours end and propose one open todo per window — exactly the
   briefing's spare-time logic: `free_windows(today_commitments, max(now,
   band_start), band_end)` then `suggest_for_windows(windows, open_todos, bias)`
   (`briefing.py:126-147`, `scheduling.py`). The bias keeps the fit honest. The
   message frames this as "here's what still fits," not "you must do all of this."
2. **Suggest a deferral.** From the still-ahead commitments, surface the
   lowest-stakes ones (`hardness != "hard"`) as *droppable/deferrable today* —
   especially any the `impact` pass flags `at_risk`. This is a **suggestion list**
   in the response; the layer does not move anything (acting goes through the
   existing commitment endpoints). Hard commitments are never suggested for
   deferral.
3. **One small next step.** Pick the single most-worth-doing item — the top
   `avoided_todos` entry if there is one, else the best fit for the *next* free
   window — and run it through `todos.decompose_task(title)` (`todos.py:320`) to
   get a tiny, ≤-few-minute first step. "Rough day — if you only do one thing:
   `<tiny step>`." This reuses the task-initiation work wholesale and is the
   emotional core of the recovery message: shrink the next action until it's
   unintimidating.

If the day is **not** rough, `build_recovery` returns an empty plan and the
renderer emits nothing (the endpoint returns `rough: false`).

---

## 5. Triggering & debounce

The layer is **pull-based and debounced**, not a background loop:

- **Debounce key.** A `last_encouragement_date` coaching-state key (UTC
  `YYYY-MM-DD`) records the last day a message was *delivered*. This mirrors the
  `last_conflict_signature` cursor pattern in `commitments.py:215`. If today's
  date already matches, `GET /encouragement` still returns the current assessment
  but with `already_sent: true` and the caller (n8n) skips delivery. This caps
  delivery at **once per day** regardless of how often the endpoint is polled.
- **Who polls.** An n8n flow polls `GET /encouragement` a few times across the
  afternoon/evening (when a day's badness is observable); on the first `rough &&
  !already_sent`, it delivers and the endpoint stamps the date. A `?deliver=1`
  (or a tiny `POST /encouragement/sent`) is what advances the cursor, so a plain
  read never burns the day's one message. (Decision: keep the read pure; advancing
  the cursor is an explicit write — see §10 open question on whether n8n or the
  endpoint owns the stamp.)
- **No new scheduler.** This rides the cadence Prefrontal already has (n8n
  workflows + the nightly `learn`/`summarize`); it adds no always-on watcher.

---

## 6. Surfaces

### 6.1 `GET /encouragement` (primary)

Model-free and fast at the endpoint, exactly like `GET /briefing`
(`app.py:831`) — n8n delivers `text` directly or feeds it to Ollama.

```jsonc
{
  "date": "2026-06-29",
  "rough": true,
  "rough_score": 4.0,
  "already_sent": false,
  "signals": [
    {"kind": "missed_hard", "detail": "'Dentist' — missed (45 min late)", "weight": 3.0},
    {"kind": "miss_episode", "detail": "abandoned outing: coffee", "weight": 1.0}
  ],
  "plan": {
    "refit": [{"start": "2026-06-29 15:00:00", "minutes": 40, "suggestion": "Call the plumber", "todo_id": 12}],
    "defer": [{"title": "Optional team coffee", "commitment_id": 88, "reason": "soft, at risk"}],
    "first_step": {"todo_id": 7, "title": "Email the school", "step": "Open Gmail and start a one-line reply", "minutes": 2}
  },
  "text": "Today got away from you — that happens...\n\nIf you only do one thing: open Gmail and start a one-line reply.\n..."
}
```

Auth: same `X-Prefrontal-Token` / `_verify_token` as every other endpoint
(`app.py:376`). (Carries over cleanly to per-user scoping in
[`multi-tenant.md`](multi-tenant.md) — it reads only the scoped store and writes
one scoped state key.)

### 6.2 Morning-briefing tone variant

When the **morning** already looks rough (rare but real — e.g. you wake to a
missed hard commitment from overnight, or a heavily double-booked day), the
briefing swaps its closing coaching note for a short encouraging variant instead
of the usual time-bias reminder (`briefing.py:236-246`). Implementation: the
briefing calls `assess_day`; if `rough`, `render_briefing` appends the
encouragement's acknowledgement + first-step line rather than the bias nag. This
is the "briefing variant" the roadmap mentions and is purely additive to the
existing renderer.

### 6.3 CLI

`prefrontal encourage` — print today's assessment + plan (deterministic), with
`--llm` for Ollama prose and `--db-path`/`-o` like the briefing command
(`cli.py:345`). Useful for testing the trigger against the live DB without n8n.

---

## 7. Configuration & tone calibration

New coaching-state keys (opt-in; the layer is **inert** unless enabled):

| Key | Default | Meaning |
|---|---|---|
| `encouragement` | `"off"` | `on`/`off` master switch. Off ⇒ endpoint returns `rough:false`, briefing variant never fires. |
| `encouragement_threshold` | `"3.0"` | `rough_score` cutoff (§3.1). |
| `encouragement_tone` | `"warm"` | `warm` (reassuring, second-person) / `plain` (matter-of-fact, fewer affect words) — for users who want recovery *content* without the softness. |

These are read via `store.all_state()` like every other coaching key; no new
storage shape. The optional `summarize_encouragement` system prompt is selected by
`encouragement_tone`, and the deterministic renderer likewise picks its phrasing
bank from it (so even the no-model path respects the tone). `preferred_briefing_
format` (`short`/`long`) is reused to control plan verbosity, so we don't add a
redundant key.

Because these are coaching-state keys, they become per-user automatically under
the multi-tenant spec (its §5 makes `coaching_state` per-user) — no extra work
there.

---

## 8. Why deterministic-first (and the LLM's narrow job)

The trigger and the plan are deterministic so that: (a) the layer is testable
without a model; (b) a user can trust *why* they got a message (the `signals`
array is auditable); and (c) it works when Ollama is down — the README's
local-first promise. The model's only job is phrasing warmth over a plan that's
already decided, and it always has a heuristic fallback. This matches how
`briefing.py` and `summarizer.py` already split structure from prose, and keeps
the failure mode "slightly less warm wording," never "no recovery plan."

---

## 9. Touch list (where the work lands)

| Area | Files | Change |
|---|---|---|
| Core layer | **new** `prefrontal/encouragement.py` | `assess_day`, `DayAssessment`, `build_recovery`, `RecoveryPlan`, `render_encouragement`, `summarize_encouragement`, `EncouragementResult`, weights/threshold constants, `ENCOURAGEMENT_SYSTEM_PROMPT` (warm/plain) |
| Debounce cursor | `prefrontal/memory/store.py` | none new — reuse `get`/`set_state` for `last_encouragement_date` (one read, one write) |
| Endpoint | `prefrontal/webhooks/app.py` | `GET /encouragement` (+ explicit cursor advance per §5); model-free like `/briefing` |
| Briefing variant | `prefrontal/briefing.py` | call `assess_day`; when rough, swap the closing coaching note (additive to `render_briefing`) |
| CLI | `prefrontal/cli.py` | `encourage` subcommand (`--llm`, `--db-path`, `-o`) |
| Delivery | **new** `deploy/n8n/encouragement.workflow.json`, `docs/deployment.md` | poll `/encouragement`, deliver once when `rough && !already_sent` |
| Config docs | `README.md`, `.env.example`/coaching docs | document the three opt-in keys |
| Pure cores reused, unchanged | `scheduling.py` (`free_windows`/`suggest_for_windows`), `impact.py` (`analyze_impact`/`at_risk`), `todos.py` (`avoided_todos`/`decompose_task`), `commitments.py` (`find_conflicts`), `patterns.py` (drift) | **no change** — the layer composes them |

---

## 10. Testing strategy

The highest-value tests are deterministic and model-free:

- **Threshold:** a fixture day with one missed hard commitment ⇒ `rough:true`; a
  day with one stray late reply ⇒ `rough:false`; three `miss` episodes ⇒
  `rough:true`. Assert `rough_score` and the `signals` contents, not just the
  bool.
- **Plan correctness:** given known commitments + open todos, `build_recovery`
  returns a re-fit that matches `suggest_for_windows` over the remaining
  windows, suggests only `soft` commitments for deferral (never `hard`), and a
  `first_step` whose `minutes ≤` the decomposition ceiling.
- **Debounce:** two reads the same day after a delivery ⇒ second has
  `already_sent:true` and the cursor doesn't advance twice; a new UTC day resets.
- **Tone/opt-in:** `encouragement="off"` ⇒ always `rough:false` and the briefing
  variant never fires; `plain` vs `warm` select different renderer phrasing and
  system prompts.
- **Fallback:** with Ollama stubbed to fail, `summarize_encouragement` returns the
  deterministic text with `source:"heuristic"` (mirror the briefing summarizer
  test).

---

## 11. Rollout sequence

1. `encouragement.py` core (`assess_day` + `build_recovery` + `render_encouragement`)
   + deterministic tests. No surface yet.
2. `GET /encouragement` (model-free) + the debounce cursor + endpoint tests.
   Opt-in key defaults to `off`, so deploying is a no-op until enabled.
3. CLI `encourage` + optional `summarize_encouragement` (Ollama + fallback).
4. n8n delivery workflow + deployment doc; enable for the live user and confirm
   the once-per-day delivery on a genuinely rough day (the §1 success test).
5. Briefing tone variant (smallest, most visible) once the assessment is trusted.

Steps 1–3 ship invisibly (feature off). Step 4 turns it on for one user; step 5
threads it into the daily briefing.

---

## 12. Open questions

- **Cursor ownership.** Does `GET /encouragement?deliver=1` advance
  `last_encouragement_date`, or a separate `POST /encouragement/sent` that n8n
  calls only after a successful push? The latter is cleaner (read stays pure, the
  stamp reflects *actual* delivery, not intent) and is the leaning; the former is
  one fewer round-trip for n8n. Decide when wiring the workflow.
- **Time-of-day gating.** Should the endpoint refuse to flag `rough` before, say,
  late morning, so an 8am blip doesn't trip it? Probably a soft rule
  (`miss`-heavy mornings are real), but worth a `min_hour` guard if testing shows
  early false positives.
- **Interaction with avoidance surfacing.** The briefing already lists "you keep
  putting off" (`briefing.py:218`). The encouragement first-step should pick from
  the *same* `avoided_todos` set but must not feel like a second scolding — the
  framing flips from "you're avoiding these" to "just start this one." Validate
  the combined briefing doesn't read as piling on.
- **Drift baseline.** What counts as "above baseline" drift for the modifier — a
  fixed `observed_value` cutoff, or relative to the user's own trailing average?
  Start fixed; revisit once there's real per-user drift history.
