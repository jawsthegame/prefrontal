# Module interventions

Every executive-function support in Prefrontal is delivered by a **module**. Each
module owns one challenge and declares its **interventions** — the concrete
behaviours it performs, each with a trigger and a wired/planned status. This
document is generated from the modules' own `interventions()` declarations on
`main` (`prefrontal/modules/*.py`); it is the human-readable form of
`prefrontal modules -v`.

All interventions below are **active** (wired end-to-end) unless noted.

---

## Hyperfocus
*Sustained intense focus that is a strength when aimed at the right task but a liability when misdirected or when it overruns basic needs and commitments.*

| Intervention | What it does | Trigger |
|---|---|---|
| `protect_window` | Suppress non-critical nudges while aligned hyperfocus is healthy. | an aligned session under the hard-interrupt ceiling |
| `alignment_check` | One gentle check once a block overruns its planned (or default) span. | elapsed time passes the planned duration / soft block |
| `biological_break` | Alignment-independent break for water/food/movement past the ceiling. | elapsed time exceeds the hard-interrupt length |
| `abandoned_auto_close` | Auto-close a session left open far past the hard ceiling. | elapsed time exceeds the abandon ratio with no exit tap |
| `suggest_start` | Gently offer to start a focus block at a ripe moment (opt-in). | no block yet today, nothing running, in the focus-friendly hours |
| `recap_prompt` | At day's end, offer to log a block you forgot to start (opt-in). | evening, nothing running, and no block logged today |

## Impulsivity
*Acting before reflecting — abrupt context switches and snap decisions that pull attention off the intended task.*

| Intervention | What it does | Trigger |
|---|---|---|
| `reflective_pause` | Insert a short, dismissible pause before an impulsive switch. | a context switch away from an active intended task |
| `capture_and_defer` | Log the new impulse to the inbox so it's safe to return to later. | a captured idea/task during a focus block |
| `switch_rate_feedback` | Surface how often switches happened today vs the baseline. | the end-of-day check-in |

## Location-Aware Task Anchor
*Leaving with a short, stated intention and losing track of time — errands that quietly stretch well past the time you meant to be out.*

| Intervention | What it does | Trigger |
|---|---|---|
| `soft_nudge` | Soft push at 50% of the stated window ("still on track?"). | elapsed time reaches 50% of the stated window |
| `firm_nudge` | Firm push at 100% of the stated window ("time to wrap up"). | elapsed time reaches 100% of the stated window |
| `voice_call` | Twilio voice call at 150% of the stated window. | elapsed time reaches 150% of the stated window |
| `location_gating` | Suppress nudges and, on arriving home, ask whether to end the outing — auto-closing as returned after a grace period, backdated to first arrival. | a location check places the user within the home radius |
| `abandoned_auto_close` | Auto-close an outing left open far past its window. | elapsed time exceeds the abandon ratio with no return |

## Self-Care
*Basic needs — eating, hydrating — dropped during hyperfocus. Not an executive-function challenge per se but a downstream casualty of one: the deeper the flow, the easier a meal or a glass of water is to skip.*

| Intervention | What it does | Trigger |
|---|---|---|
| `meal_check` | From `meal_start_hour`, ask "have you eaten?" and re-ask every `meal_reask_minutes` until confirmed — even mid-focus. One-tap Ate / Snooze on ntfy. | mid-morning onward, until you confirm you've eaten |
| `water_check` | From `water_start_hour`, a "drink some water" reminder every `water_interval_minutes` until you hit `water_daily_target`. One-tap Drank / Snooze on ntfy. | through the day, on an interval, up to the daily target |
| `meds_check` | From `meds_start_hour`, ask "taken your meds?" and re-ask every `meds_reask_minutes` until you hit `meds_daily_target`. Off unless `meds_enabled` — medication is personal. One-tap Took / Snooze on ntfy. | from your meds hour, until you confirm the day's dose(s) |
| `biobreak_check` | Between `biobreak_start_hour` and `biobreak_end_hour`, a "take a bathroom break" reminder every `biobreak_interval_minutes`. A reminder, not a quota: no daily count silences it — the end hour bounds it, and Went just defers the next. On with `self_care`; toggle with `biobreak_enabled`. One-tap Went / Snooze on ntfy. | on an interval through the day, within a set time window |
| `winddown_check` | From `winddown_start_hour` (evening), a "start winding down for bed" reminder every `winddown_reask_minutes` until you confirm. Off unless `winddown_enabled` — a bedtime is personal. Leans on responsive hours so it never nags past your quiet-hours start. One-tap Winding down / Snooze on ntfy. | evening run-up to bed, until you confirm you're winding down |
| `movement_check` | From `movement_start_hour`, a "time to move" nudge for a few minutes of movement / stretching, re-asked every `movement_reask_minutes` until you confirm. The daily-movement *floor* — a tiny, always-doable minimum that survives the worst day so the habit never hits zero. Off unless `movement_enabled` — an exercise cadence is personal; defaults to the morning (07:00), anchored to the first coffee, an existing daily pause you habit-stack onto. One-tap Stretched / Snooze on ntfy. | from your movement hour, until you confirm you've moved |

## Task Paralysis
*Difficulty initiating tasks despite intent — the task stalls before it ever begins, especially for ambiguous or large work.*

| Intervention | What it does | Trigger |
|---|---|---|
| `tiny_first_step` | Reframe a stalled task as one <5-minute concrete first action (`POST /todos/{id}/decompose`; also fed to panic and the `/todos/now` widget). | on demand, or a task you're about to start |
| `auto_decompose` | Break a task the user is *avoiding* into a tiny first step + collapsed remaining steps — but only if the **model judges** it worth decomposing (it can decline). Runs on the coaching tick (`sweep_avoided_decompositions`), not at creation. **Off by default** (`auto_decompose_enabled`); opt in via Settings. The on-demand 'Break it down' button works regardless. | a todo that has reached "avoided" status (when opted in) |
| `body_double_nudge` | Surface a task you keep bailing on and suggest a start-together window (`GET /todos/stuck`; named in the profile). | repeated misses on the same task |
| `clarify_ambiguous` | Notice a vague todo/commitment ('Tax', 'Mom') that can't be started because it can't be named, ask ONE inline clarifying question, and — once it resolves to a recognized task — offer a guided walkthrough (`prefrontal/clarify.py`). The detection sweep runs on the coaching tick (`sweep_ambiguous_items`) and fills the dashboard clarify card; `POST /clarifications/check` is the on-demand twin. | an ambiguous item on the schedule or todo list |
| `refocus` | When you're actively working on a lower-priority todo than one you're avoiding-and-haven't-started (`focus_conflict`), gently flag it and suggest switching — honest prioritization in the moment, the push twin of the dashboard's focus-conflict banner. Held during protected hyperfocus (`is_focus_protected`) and quiet hours; deduped per conflict so it fires once, not every tick. | a started todo ranks below a task you're avoiding |

> **Decomposition, in full:** automatic breakdown is **off by default** — the user
> opts in via Settings (`auto_decompose_enabled`), and only then does the
> coaching-tick sweep run. Once on, breakdowns are generated only for a todo being
> *avoided*, and the model can reply "not worth breaking down." The user can
> dismiss a breakdown as **not needed** (repeated → auto-decompose switches back
> off) or **didn't help** (fed back as negative examples to the decomposer).
> On-demand **Break it down** always works regardless of the setting. See
> `todos.decompose_task` / `sweep_avoided_decompositions` / `auto_decompose_enabled`
> and the `decomposition_feedback` table.

## Time Blindness
*Difficulty sensing elapsed time and estimating task/travel duration, leading to chronic underestimation and late departures.*

| Intervention | What it does | Trigger |
|---|---|---|
| `estimate_correction` | Multiply the agent's raw time estimates by the learned bias. | any prediction of task or travel duration |
| `departure_buffer` | Add a learned buffer and nudge earlier than the naive leave time. | an upcoming calendar event with a location |
| `elapsed_time_callouts` | Periodic "you've been on this N minutes" callouts during a block. | an active focus/work block |

## Closed-Loop Trip Tracking
*Trips taken without declaring them — you leave, you come back, and the time and context are gone. Passive tracking captures the round trip; a quick label, category, and honest "how it went" turns it into signal.*

| Intervention | What it does | Trigger |
|---|---|---|
| `label_prompt` | Ask for a label + category on a completed, undeclared trip. | a location loop closes (left the home radius, then returned) |
| `reflection_capture` | Accept a plain-English "how it went" note, classify it into an outcome, and feed it back into stats + the learning engine. | the user reflects on a completed trip |
| `focus_balance` | A gentle weekly heads-up when out-of-home time skews away from a life-sphere you've set a target for (kids/personal) — well-being, not productivity. Opt-in via the `focus_balance_nudge` state flag. | a targeted life-domain falls under half its weekly aim |

---

### How interventions run

- **Coaching tick** (`POST /webhooks/coach/check`, `prefrontal coach`) fans out over every enabled module's `evaluate()`, collects the due **cues**, applies channel choice + quiet-hours/debounce suppression, and delivers each on its channel.
- **Real-time escalations** (outing/focus nudges) fire from their own `/webhooks/*/check` endpoints on their elapsed-time thresholds.
- **Learning** feeds back through `episodes` → `patterns` (the nightly `prefrontal learn`), so estimates, channel choice, and cadences self-tune from outcomes.

Module enablement is controlled per deployment (`enabled_modules`) and per-user state; a disabled module's interventions simply don't fire.
