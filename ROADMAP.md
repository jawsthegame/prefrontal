# Roadmap

Prefrontal is in early development. This file tracks what's **planned or still
open** — the gap between "scaffolded" and "finished" — so it stays visible to
contributors. For what's already shipped, see [`CHANGELOG.md`](CHANGELOG.md).
(See also `prefrontal modules -v` for per-module intervention status.)

## What's next (prioritized)

A digest of the open threads detailed in the sections below, roughly in priority
order. Nearly every capability area has shipped its core; what remains is
closeout, consolidation, and a few net-new surfaces.

1. **Context Pack surface tailoring** (see "Beyond v1 › Context Packs"). The
   **Caregiver** pack, the dedicated `care` commitment kind, the `/care` surface,
   the **situation-tool registry** (with the Parent pack's school-run tool), and the
   **care-recipient names roster** (the caregiver mirror of `child_names`, driving
   deterministic `care` classification) have now shipped — and the **Parent pack's
   situation-tool set is now complete**: the **sick-day replan** (`sick_day`, panic
   triage + a hard/soft split of today) and the **pack-the-bag checklist**
   (`pack_the_bag`, `decompose_task` over the kids' next events) join the school-run
   tool in the `packs` router. What remains is breadth on the same seams for *other*
   packs: **situation tools beyond Parent** (a Caregiver surface), **more packs**
   (Grad student / New job), and **more tailored surfaces** — plus the smaller
   `/care` follow-up of a conditional nav link (revealed when the pack is on,
   mirroring the operator Admin link).
2. **Close the learning loop's remaining causal check** (see "Learning &
   adaptation" §2). The channel-signal auto-act shipped (§4 now damps a
   non-predictive channel toward pooled); what's left is the harder **sensor
   causal check** — after a state proposal is accepted, did the downstream numeric
   calibration actually improve? — which stays design-blocked on the
   broader-allowlist / generalized-check question, so last.
3. **Close the last Shortcuts → native intent gaps** (see
   [`docs/shortcuts-to-native.md`](docs/shortcuts-to-native.md)). Essentially done:
   the native **location pipeline is complete** (epic #569), the three non-location
   App Intents (`CaptureImpulse`, `TripRetro`, reflective pause) **shipped**, the
   **onboarding + user-facing docs are repointed** at the native app (skill,
   README, deployment, guide, iOS README — Shortcuts kept as the free-signing
   fallback), and the **server self-description + `source` provenance enum**
   (`app_intent`/`geofence`/`shortcut` on `POST /webhooks/shortcut`) **shipped**.
   What's left is only the one case that can't go native — the **Set Alarm**
   deep-link (no public alarm API pre-AlarmKit/iOS 26; keep the Shortcut, file an
   AlarmKit follow-up) — plus the client-side nicety of having the iOS App Intents
   actually send `source: "app_intent"` so the provenance is populated.

**Recently closed out:**

- **Native-CoreLocation location pipeline (epic #569)** — the location half of the
  Shortcuts → native migration. Shipped: the significant-location-change position
  feed (#562), native arrival → outing-return (#563), `CLVisit` arbitrary-venue
  monitoring (#564), permission-UX hardening (#566), and the docs retiring the four
  location Shortcuts for app users (#567); web-configurable location tunables (#565),
  and a server last-known-location staleness guard (#568) all shipped. A
  native-app user needs **no** location Shortcuts. See `CHANGELOG.md`.
- **Focus-balance follow-ups** — all three shipped: the **Trip retro** combined
  endpoint + Shortcut (`POST /webhooks/trip/retro`), **configurable quick-file
  domains** (`trip_quick_domains`), and **domain-at-declaration** for outings
  (`/webhooks/outing/start` pre-files the sphere). See "Focus balance — follow-ups".
- **Unified triage** — the standalone mail path now feeds the *one* shared triage
  pipeline. Mail keeps its specialized classifier but routes/audits through
  `triage.apply` via a `Signal`/`TriageDecision` adapter, so there's a single place
  that creates the todo and a single `triage_log`. The related **Google Apps Script
  work-email digest** remains the open ingestion *source* (it can post normalized
  `Signal`s to `POST /triage` today). See "Beyond v1 › Triage agent".
- **Coaching agent** — complete: the optional **LLM phrasing pass** on ambient
  cues (`coach_llm_phrasing`, profile-grounded, heuristic fallback), the
  **encouragement layer** folded in as a cue producer (`encouragement_cues`) so
  recovery routes through the shared engine, and **`/webhooks/outing/check`
  deprecated** now that `coach/check` runs the identical anchor decision. See
  "Beyond v1 › Coaching agent".

**Shipped:** the native **WidgetKit + ActivityKit Live Activity** (once deferred
for needing a Swift app / Xcode / Apple Developer account) now ships in
`ios/PrefrontalWidgets/`, along with Home Screen / Lock Screen glances. The legacy
Scriptable widget it once stood in for has been retired.

The sections that follow track each capability area in detail, marking shipped
progress (✅) inline against what remains open.

## Known stubs in the current code

- ~~**n8n inbound handlers**~~ ✅ **discharged** — `POST /webhooks/n8n` now
  normalizes the body into a `Signal` and runs the Triage agent (classify →
  route → maybe nudge), joined by `POST /triage` and `GET /triage/recent`. See
  "Recently shipped" in `CHANGELOG.md` and `docs/triage-agent.md`.
- **Module interventions** — all seven modules have `status="active"`
  interventions: **Location-Aware Task Anchor** (escalation, location-gating,
  auto-close), **Hyperfocus** (protect/interrupt focus sessions), **Time
  Blindness** (`estimate_correction` + `departure_buffer` — departure timing +
  outcome capture — plus `elapsed_time_callouts`: an opt-in "you've been on this
  N min" time check fired from `evaluate()` during a focus block, deduped per
  elapsed bucket, off unless `elapsed_callout_minutes` > 0 — and `morning_prep`:
  a night-before nudge to set an alarm ahead of an early start), **Task Paralysis**
  (`tiny_first_step` / `auto_decompose` / `body_double_nudge` — see below),
  **Impulsivity** (`reflective_pause` + `capture_and_defer` + `switch_rate_feedback`
  active), **Self-Care** (`meal_check` + `water_check` basic-needs nudges,
  opt-in), and **Closed-Loop Trip Tracking** (`label_prompt` / `reflection_capture`
  / `focus_balance` / `away_proposal`). With `switch_rate_feedback` now live (per-session `switch` episodes → a
  `context_switch` pattern → the briefing's switch-rate line), **every declared
  module intervention is `active`** — none remain `planned`. Run
  `prefrontal modules -v` for the live per-intervention status.

## Per-module next steps — ✅ all nine shipped

Every module's *declared* interventions were already wired, so this was one
**net-new** capability per challenge module — the honest next surface for each,
composing existing primitives. **All nine have now shipped** (see `CHANGELOG.md`
for the release note); the descriptions below are kept as the design of record.

- **Time Blindness — learn the morning-routine lead.** ✅ `morning_prep`'s one-tap
  "Set alarm" backs a *static* `morning_routine_minutes` (default 60) off the
  leave-by to suggest a wake time. But departure-outcome capture
  (`POST /webhooks/departure/left` → `classify_departure`) already scores whether
  you actually left on time. Feed those on-time/late verdicts on early starts back
  into a *learned* per-person routine lead — consistently late off a 60-minute lead
  means your real get-ready span is longer — so the suggested wake time reflects how
  long you actually take, the same "it gets better the longer you use it" loop the
  time bias already rides.
- **Task Paralysis — a real body-double session.** ✅ `body_double_nudge` only
  *suggests* scheduling a start-together window; nothing runs one. Add an actual
  timed start-together session — a short co-working timer that opens on the one
  tiny first step and checks in at the end — optionally paired with a household
  member (the co-parent scope already exists) so two people genuinely start
  together. Turns the suggestion into the scaffold it names.
- **Hyperfocus — learn a personal soft-block length.** ✅ The soft check (90 min) and
  hard ceiling (180 min) are static defaults for everyone. Closed focus sessions
  already carry planned-vs-actual durations *and* the one-tap `worth_it` /
  `should_have_stopped` / `pulled_off` exit ratings — enough signal to learn where
  *this* person's productive focus tips into diminishing returns, so
  `alignment_check` lands at their real inflection point instead of a generic 90.
  Bounded and never below a floor, mirroring the self-care cadence learner's shape.
- **Impulsivity — a captured-impulse retro.** ✅ `capture_and_defer` parks impulses to
  the inbox but never closes the loop its own docstring promises ("the ideas are
  safe if you jot them down"). A periodic, ambient review that resurfaces what you
  parked during focus blocks — most were noise you can clear in one tap, a few were
  real and promote to todos — proves the capture worked and empties the inbox, so
  parking stays trustworthy enough to keep choosing over chasing.
- **Location-Aware Task Anchor — learn per-errand default windows.** ✅
  `infer_time_window` re-asks the LLM/heuristic for *every* windowless outing and
  ignores your own history for that same errand, even though returned outings log
  real durations keyed by intention (and the `activity:outing` bias already
  calibrates the projection). Learn a per-intention baseline ("coffee" ≈ your
  historical ~N min) and pre-fill it as the stated window, so a recurring run
  escalates against your actual pace from minute one rather than a generic guess.
- **Closed-Loop Trip Tracking — multi-stop trips.** ✅ The trip state machine is
  strictly home → away → home, so a chained run (home → store → school → home)
  collapses into one undifferentiated blob. Detect an intermediate *dwell* (a stop
  of N+ minutes away from home) to split a trip into its legs, so the label ask,
  category, and focus-balance attribution reflect the real errands instead of a
  single lump — the passive-capture counterpart to declaring separate outings.
- **Self-Care — auto-satisfy from other signals.** ✅ Every basic-needs check is
  purely interval-driven and blind to what the rest of the system already knows: a
  just-returned outing/trip categorized *food* (or a calendar lunch) should count
  toward or defer the meal check rather than asking "have you eaten?" the minute you
  walk back in; a logged workout trip could satisfy `movement`. Cross-reference
  trips / outings / commitments to suppress or pre-satisfy a check, so the nudges
  read as attentive rather than oblivious.
- **Delegation follow-through — stalled hand-off escalation.** ✅ A human delegation
  stuck `forwarded` with no movement just re-surfaces on the same slow cadence
  forever; `before_collect` already logs the `ignored` outcomes but nothing acts on
  chronic non-movement. After K missed check-ins, escalate the copy from "heard
  back?" to a *decision* prompt — take it back, re-delegate, or drop it — so a dead
  hand-off gets resolved instead of quietly rotting on the parked list.
- **Projects — staleness → next action.** ✅ A staleness nudge asks "still on it?",
  but a project usually goes quiet because its next step is fuzzy (task paralysis at
  the project scale). Pair the re-surfacing with a decomposed tiny first step for
  the project's oldest open todo (reusing `decompose_task`), so re-engaging is one
  concrete action to start rather than a yes/no that leaves the actual friction
  untouched.

## Module 1 — Location-Aware Task Anchor: follow-ups

- **Web UI to name/manage curated places** ✅ — curated places (matched by a
  geofence and departure travel-time) are now list/add/edit/delete from the
  Settings "Places" card, not just `prefrontal place add`. New
  `DELETE /places/{name}` + `ScheduleRepo.delete_place`; add/relabel/fix-coords
  reuse the upserting `POST /places` (a rename is add-new + delete-old client-side).
  See `CHANGELOG.md`. *(Next: let the phone name a just-visited `CLVisit` venue
  straight into a curated place.)*
- **Location-gating the escalation** ✅ — when a location check places the user
  within the home radius (`home_radius_m`), `/webhooks/outing/check` suppresses
  the nudge and passively closes the outing as returned, so coming home early (or
  forgetting "I'm back") never triggers a call. Needs `current_lat`/`current_lon`
  in the poll body to activate.
- **Abandoned auto-close** ✅ — outings left open past `abandon_after_ratio`× the
  window (default 3×) are auto-closed as `abandoned` and logged as a drift `miss`
  (no fabricated duration), so they stop lingering active.
- **Feed outings into learning** ✅ — outing returns log `task` episodes that the
  pattern pass folds into `time_estimation` and the bias multiplier.
- **Finer `context_key`** ✅ — outings now have their own bias bucket so coffee runs
  calibrate separately from other tasks. A new **activity** dimension in the pattern
  pass (`compute_bias_by_activity` → `time_estimation_bias:activity:outing`) splits
  the `type:task` pool by the activity read off the episode `context`, and the outing
  nudge projection (`location_anchor.evaluate` + `/webhooks/outing/check`) resolves
  `activity="outing"` (falling back to global). See the Pattern-computation pass
  entry above. Covered by `tests/test_patterns.py`.

## Focus balance — follow-ups

The life-domain lens over out-of-home time shipped end to end
(`prefrontal/focus_balance.py`; the 5 spheres shop/work/home/kids/personal, the
weekly rollup behind `GET /balance` / `prefrontal balance` / the briefing line /
the profile section, the opt-in "light on kids/personal" nudge the Parent pack
seeds, declared outings folded in alongside passive trips, and one-tap 🏠/🧒/🙋
domain buttons on the trip-label ask). Open threads:

- **Per-trip `context_key` bucket** ✅ (mechanism) — the pattern pass gained an
  **activity** dimension that buckets `task` estimation pairs by the activity read
  off the episode `context` (`outing` / `focus` / `trip` / …), so out-of-flow time
  no longer pools into one `type:task` multiplier. **Outings** get the full win —
  they carry a predicted/actual pair, so `time_estimation_bias:activity:outing`
  populates and the outing projection now calibrates against outing history (see the
  Module-1 item above). **Trips** carry `predicted_value=None` by design (a passive
  trip is undeclared — nothing was estimated), so they form no estimation pair and
  never touched the shared bias in the first place; the activity bucket is there for
  them the moment a trip ever carries a prediction, but there's no current pollution
  left to separate. Covered by `tests/test_patterns.py`.
- **Fuller one-tap label/reflect Shortcut** ✅ — `POST /webhooks/trip/retro` is the
  combined endpoint: label + category + domain + reflection in one call (each
  optional; `trip_id` defaults to the newest trip awaiting a label, so a bare-tap
  Shortcut needn't carry it), reusing the exact `label_trip`/`set_trip_domain` +
  `apply_reflection` logic the three single endpoints do and returning one speakable
  `confirmation`. The **Trip retro** iOS Shortcut recipe (`deploy/ios-shortcut.md`)
  closes the whole retrospective from the notification without opening the
  dashboard. Covered by `tests/test_trips.py`.
- **Configurable quick-file domains** ✅ — the one-tap trio is no longer hard-coded
  to home/kids/personal. The `trip_quick_domains` coaching key picks which ≤3 of
  shop/work/home/kids/personal the ambient trip-label ask's buttons file (a
  shopkeeper can surface `shop`); `resolve_quick_domains` validates/caps/defaults it,
  the `trip_tracking` module stamps the resolved set on the cue, and both delivery
  paths build the buttons from it via `notify.trip_label_actions`
  (`DOMAIN_BUTTON_LABELS` covers all five spheres). Unset/invalid falls back to the
  default trio. Covered by `tests/test_focus_balance.py`.
- **Declared-outing domain at the point of declaration** ✅ — `/webhooks/outing/start`
  now resolves the life-sphere *at declaration*: an explicit `domain` wins, else it's
  inferred from the intention text (`infer_domain_from_text` — "swim with the kids" →
  kids, ambiguous ones left unassigned), persisted on the outing, echoed in the
  `OutingStarted.domain` field, and named in the confirmation ("Filed under kids.").
  The "Going out" iOS Shortcut recipe gains a domain **Choose from Menu**. So more
  outings arrive pre-filed for the focus-balance rollup rather than needing a
  retrospective tag. Covered by `tests/test_location_anchor.py`.

## Learning & adaptation — the road past v1

Prefrontal's "it gets better the longer you use it" loop is real but narrow:
`episodes` → `recompute_patterns()` (deterministic stats) → `coaching_state` /
`patterns` → `summarize_profile()` (LLM renders prose) → injected into every
agent prompt. The only thing that *adapts* is the deterministic layer; the LLM
sits at the end and renders, deliberately kept out of the write path so the
profile can't be hallucinated into.

The honest constraint is that **learning quality is capped by observable,
structured signal — not by model quality or prompt design.** The steps below are
ordered by leverage; each is independent but builds on denser capture.

1. **Finish dense capture (largely done).** Todo closes, outings, focus, mail,
   the one-tap `POST /webhooks/shortcut` endpoint, and now **automatic departure
   outcomes** all log episodes. Departure capture closed the last big gap — did
   the user actually leave on time for a commitment? A "leave Home" geofence hits
   `POST /webhooks/departure/left`, which attributes the departure to the
   commitment it was for (`attribute_departure`), scores it on-time vs late
   against the computed leave-by (`classify_departure`), and logs a `departure`
   episode (`record_departure_outcome`) — `actual_value` left `None` so it feeds
   `drift` without polluting the shared `time_estimation_bias`. **Passive
   location capture has now shipped too** (`prefrontal/trips.py`,
   `prefrontal/modules/trip_tracking.py`): once a home coordinate is set
   (`POST /webhooks/home`), each `POST /webhooks/location` ping is folded into a
   closed-loop **trip** state machine — leaving the home radius opens a trip,
   returning closes it — logging a `task` episode with the real duration but no
   prediction (so it never touches the shared bias). Unlike the coffee-shop
   *outing*, a trip is undeclared, so it's reviewed after the fact: the
   `trip_tracking` module asks (ambiently) for a **label** and **category**, and
   an honest plain-English **"how it went"** note (`POST /webhooks/trip/reflect`)
   is classified into an outcome that *resolves* that pending episode — so
   self-report becomes drift signal — while the raw note is also handed to the
   LLM-as-sensor (§2) to propose deeper structured updates. **Focus balance now
   layers a life-domain lens on top** (`prefrontal/focus_balance.py`): the label
   ask also files each trip into a life-sphere — `domain` (shop/work/home/kids/personal),
   the same work/home axis todos and mail carry (`POST /webhooks/trip/domain` to
   edit) — and `build_focus_balance` rolls out-of-home time up per domain over a
   week. It surfaces as `GET /balance`, `prefrontal balance`, a briefing line, and
   a profile section; optional per-domain weekly `focus_target:<domain>` coaching
   keys turn a shortfall into a gentle, opt-in "light on kids/personal this week"
   ambient nudge — well-being, not productivity. The **Parent pack** seeds the
   kids/home/personal targets and the `focus_balance_nudge` flag, so installing it
   is what turns the guardrail on. The rollup covers **both** halves of out-of-home
   time now: passive trips *and* declared outings that returned (which never
   overlap in time), the outing's sphere set at declaration (`domain` on
   `/webhooks/outing/start`), via `POST /webhooks/outing/domain`, or inferred from
   its intention text. And the trip-label ask carries **one-tap 🏠/🧒/🙋 buttons**
   (`trip_domain_*` through `/nudge/act`) so a sphere can be filed straight from the
   notification — and the **Trip retro** Shortcut (`POST /webhooks/trip/retro`)
   now closes label + domain + reflection in a single interaction. *(Next: a
   per-trip `context_key` bucket so trips calibrate separately from other tasks.)*
2. **LLM-as-sensor, not LLM-as-author.** ✅ (v1) — `prefrontal/sensor.py` turns a
   free-text note ("I always blow off admin on Mondays") into *candidate*
   structured updates: a `coaching_state` key/value or an episode. Crucially the
   model only **proposes** — `extract_candidates` emits JSON validated against a
   small **allowlist** (`PROPOSABLE_STATE_KEYS` / `PROPOSABLE_EPISODE_TYPES`, and
   it strips any fabricated predicted/actual numbers), candidates land as
   **pending** rows in a new `proposals` table, and only a human accept writes
   them — stamped `source="llm_inferred"` (distinct from `inferred`/`explicit`).
   No model reachable ⇒ *no* candidates (an honest no-guess fallback), mirroring
   the summarizer's grounded-prompt shape flipped to JSON. Driven by
   `prefrontal note "…"` and `prefrontal proposals list|accept|reject`; covered by
   `tests/test_sensor.py`, and now over **HTTP** for the phone/n8n/dashboard:
   `POST /observe` (note → pending proposals), `GET /proposals?status=` (the
   review queue), and `POST /proposals/{id}/accept|reject` (apply/dismiss). The
   endpoints reuse the exact sensor path — allowlist, `source="llm_inferred"`,
   pending-only resolve — in a new `routers/sensor.py`. A **`/review` web page**
   closes the loop for the phone: jot a note (→ `POST /observe`) and accept/reject
   the pending proposals one-tap (→ the `/proposals` endpoints), on the shared
   theme/nav shell (`review.html`, served by `system.py`, linked in every
   surface's nav). A **conversation/transcript source** now sits alongside the
   single note: `extract_candidates_from_transcript` reads multi-turn
   `{speaker, text}` turns (`render_transcript` flattens them), framing the prompt
   to attribute signal to *the user* and never to other speakers, through the
   exact same allowlist/validation/pending gate. Reachable via `POST /observe`
   (a `transcript` array, which takes precedence over `text`) and
   `prefrontal note --transcript <turns.json>`. **Sensor calibration feedback**
   ✅ — the sensor now gets §4's honesty, applied to its own quality. Rather than
   the numeric walk-forward (the sensor's allowlisted keys aren't predicted/actual
   pairs — the original blocker), it measures **precision**: `compute_sensor_calibration`
   tallies accepted-vs-rejected proposals from the `proposals` table — overall and
   per `state`-key / `episode`-type target — gated on a sample count like §4.
   `recompute_sensor_calibration` runs on each `prefrontal learn` pass and persists
   the verdict (`sensor_accept_rate`, `sensor_calibration_samples`,
   `sensor_rejected_targets`). Because accept/reject is kind-agnostic it works
   uniformly over state and episode proposals, needing no broader allowlist. It
   **closes the loop**: chronically-rejected targets are named back into the
   extraction prompt (`avoided_state_keys`), so the sensor stops volunteering
   settings the user reliably declines (a soft de-emphasis — the key stays on the
   allowlist and a human still confirms). Surfaced in the profile and
   `prefrontal proposals stats`; covered by `tests/test_sensor.py`. *(Still open:
   the harder causal check — after a state proposal is accepted, did the downstream
   numeric calibration (§4) actually improve? — which is a different question from
   sensor precision and remains blocked on the broader-allowlist / generalized-check
   design.)*
3. **Recency weighting / decay.** ✅ — `compute_patterns()` and `compute_bias()`
   now weigh each episode by an exponential decay on its age (`decay_weight`,
   `0.5 ** (age_days / half_life)`), so a recent shift in behavior moves the
   estimates faster than a year of stale data resists it. Confidence uses the
   *effective* sample size (summed weights), so a group of only-stale episodes is
   trusted less than the same count of fresh ones. Half-life is the
   `learning_half_life_days` coaching key (default 30d; set `0` to weigh all
   history equally — the pre-decay behavior). Covered by `tests/test_patterns.py`.
   **Learning bucketing** ✅ — both deferred pieces now ship (the §5 context
   bucketing they waited on has landed):
   - **Sliding window** — `learning_window_days` drops episodes older than the
     window *entirely* before anything is computed (a hard cutoff on top of the
     softer decay), via `filter_to_window` in `recompute_patterns`, so ancient
     history can't drag on estimates once you've clearly moved on. `0` (default)
     = no window (prior behavior). Applied once up front, so patterns, the global
     + per-context biases, and the §4 walk-forward all see the same slice and the
     raw sample floors count only what's in it. The `learn` line reports how many
     episodes the window excluded.
   - **Per-context half-lives** — a context can override the global decay via
     `learning_half_life_days:<suffix>` (`morning`, `type:focus`, `energy:high`,
     `category:admin` — mirroring the bias key tails), so a churny context follows
     faster and a stable one slower; unset ⇒ that context uses the global (an
     explicit `0` = no decay for just that context). Resolved per bucket in the
     `compute_bias_by_*` passes. **Auto-derivation** now ships too (opt-in via
     `auto_context_half_life`): the learn pass measures each time-of-day band's
     *drift* (older-half vs recent-half bias, `derive_half_life`) and sets that
     band's `learning_half_life_days:<band>` to the global × a bounded factor —
     a churny context decays faster (down to 0.5×), a stable one slower (up to 2×),
     floored at 7 days — never overriding a hand-set value, printed in
     `prefrontal learn`. Auto-derivation now spans **every** context dimension
     (`derive_context_half_lives`): time-of-day band, episode `type`, `energy`, and
     `category`, each keyed by the same suffix its bias uses.
4. **Close the loop: measure whether adaptations help.** ✅ — `bias_calibration`
   (`prefrontal/memory/patterns.py`) runs a **walk-forward** check each learn
   pass: it learns the `time_estimation_bias` from the older predicted/actual
   pairs and measures whether applying it lowered the mean absolute error on the
   *newer* pairs it never saw (`raw_error` → `adjusted_error`, `improvement`,
   `helps`) — honest, not circular. The verdict is persisted
   (`bias_calibration_helps` / `_improvement` / `_samples`) and surfaced in the
   profile and in `prefrontal learn` output, so a bad adaptation is *visible*
   rather than silently asserted. And it now **auto-acts**: a "not helping"
   verdict shrinks the global `time_estimation_bias` toward the neutral 1.0 on the
   same pass (`decay_bias_toward_neutral`, `bias_decay_on_miss` coaching key —
   default halve the deviation, set `0` for the old report-only behavior), records
   `bias_calibration_decayed`, and the profile/CLI report what was done ("auto-
   decayed 1.4 → 1.2") — so a stale multiplier stops overshooting instead of
   waiting for recency decay to erode it. Covered by `tests/test_patterns.py` +
   `tests/test_summarizer.py`. The **channel-choice** adaptation now gets the same
   treatment: `channel_calibration` walks forward over delivered nudges and checks
   whether predicting each ack with its channel's train ack-rate beats a single
   pooled rate (`baseline_error` → `adjusted_error`, `helps`) — so a channel signal
   that's really noise is *visible* (persisted as `channel_calibration_*`, printed
   in `prefrontal learn`) rather than silently driving escalation. And it now
   **auto-acts** too ✅ — a "not helping" verdict damps the per-channel
   `channel_response` rates toward the pooled rate (`decay_channel_rate_toward_pooled`
   / `pooled_channel_rate`, `channel_decay_on_miss` key — default halve the
   deviation, `0` for the old report-only behavior), so a noisy channel signal stops
   crossing `choose_channel`'s ignore-threshold and driving escalation, recording
   `channel_calibration_decayed` for the CLI. Covered by `tests/test_patterns.py`.
   *(Drift is a surfaced diagnostic, not a predictive adaptation, so it has no
   held-out error to walk-forward — left as-is.)*
5. **Context-conditioned patterns.** ✅ (time of day + task type + energy + category) — the learned
   time-estimation bias is now computed **per time-of-day band** (morning /
   afternoon / evening) as well as globally, because the same person often
   mis-estimates very differently across the day. `compute_bias_by_band` buckets
   episodes by their local hour (recency-weighted, same sample floor) and
   `recompute_patterns` stores a `time_estimation_bias:<band>` per band with
   enough signal; `resolve_bias(store, local_hour=…)` prefers the band, falling
   back to the global multiplier then `1.0`. Wired into the flagship consumer —
   `/todos/now` calibrates with *this hour's* bias — and surfaced in the profile
   ("By time of day: morning 1.8x…") and `prefrontal learn`. Covered by
   `tests/test_patterns.py` + `tests/test_summarizer.py`. The point-in-time fit
   consumers now adopt it too — `/todos/fit` and `prefrontal fit` calibrate with
   *this hour's* band, like `/todos/now`. The whole-day consumers adopt it as
   well: `suggest_for_windows` takes an optional `bias_fn(local_hour)`, and the
   morning **briefing** and the **encouragement** re-fit size *each* free window
   with the bias for its own time of day — the 9am gap with the morning bias, the
   4pm gap with the afternoon one — instead of one flat multiplier for the day.
   A second **task-type dimension** now conditions the same way: `compute_bias_by_type`
   buckets the predicted/actual episodes by `episode_type` and
   `recompute_patterns` stores a `time_estimation_bias:type:<type>` per kind with
   enough signal, so a focus block is calibrated separately from a departure
   buffer. `resolve_bias(store, local_hour=…, episode_type=…)` prefers the
   time-of-day band, then the type bias, then the global multiplier — so the
   todo-fit path (`/todos/now`, `/todos/fit`, `prefrontal fit`, briefing,
   encouragement) passes `episode_type="task"` and gets the focus-session-learned
   `task` bias as a fallback whenever its band has no data yet. Surfaced in the
   profile ("By task type: …") and `prefrontal learn`. Two further dimensions,
   **energy** and **category**, condition the same way once the data exists to
   feed them: a focus session may now be **linked to the todo it's working**
   (`todo_id` on `focus_sessions`, an optional field on `POST /webhooks/focus/start`),
   and on close that todo's `energy`/`category` are stamped onto the `task`
   episode (new nullable `episodes.energy`/`episodes.category`, back-filled by the
   migrate pass). `compute_bias_by_energy`/`compute_bias_by_category` then bucket
   those tagged pairs into `time_estimation_bias:energy:<load>` /
   `:category:<cat>`. `resolve_bias` now takes `energy`/`category` too, with the
   precedence *band → energy → category → type → global* — each layer a fallback
   for the one above, so supplying the finer context never regresses a caller
   whose band already resolves. `fit_todos` grows a per-todo `bias_fn`, and the
   point-in-time pickers (`/todos/now`, `/todos/fit`, `prefrontal fit`) pass
   `task_bias_resolver(store, local_hour=…)` so each candidate is padded by *its
   own* energy/category. Surfaced in the profile ("By energy: …", "By category:
   …") and `prefrontal learn`. Untagged history (free-text focus, legacy
   episodes) simply falls through to the coarser dimensions. The whole-day
   consumers adopt it too: `suggest_for_windows` takes a `resolver_for_hour`
   factory (`local_hour -> (todo -> multiplier)`), so the morning **briefing** and
   the **encouragement** re-fit size each free window by its time of day *and*
   each candidate by its own energy/category — the same `task_bias_resolver` the
   point-in-time pickers use. The **`context_switch`** derivation has now landed
   too: focus close logs a per-session `switch` episode (impulses vs deferred), and
   `patterns.py` derives mean impulses/deferrals per focus block — feeding the
   Impulsivity profile line and the briefing's `switch_rate_feedback`.
6. **Adaptive self-care cadence (with an honesty check).** ✅ — the self-care
   checks now *learn* their interval from how you actually respond
   (`adapt_self_care` / `adapt_self_care_interval` in
   `prefrontal/modules/self_care.py`, run in the nightly `learn`):
   - **Response latency is captured.** `mark_self_care_prompted` stamps the
     delivery time when a meal/water cue fires (from both `/webhooks/coach/check`
     and `prefrontal coach --deliver`); the Ate/Drank/Snooze tap records the
     nudge→tap latency in its `self_care` episode.
   - **Honesty check (the key guard).** When the *timed* confirms are mostly
     **instant** (within `INSTANT_CONFIRM_SECONDS`, ~reflexive dismissals), the
     learner *holds* the cadence rather than reading them as "needs it less" —
     so a scaffold that's just being swatted away is never quietly removed.
   - Otherwise: a high **snooze** rate (explicit "too often") *widens* the
     interval; genuine engagement (few snoozes + enough plausibly-timed confirms)
     *eases off* a little. v1 only ever widens, bounded to `MAX_INTERVAL_FACTOR`×
     the default, and never overrides an interval the user set explicitly. The
     verdict prints in `prefrontal learn`. Covered by `tests/test_self_care.py`.
   - **Unanswered nudges now count too.** `sweep_unanswered_self_care` (run each
     coaching tick, from `/webhooks/coach/check` and `prefrontal coach --deliver`)
     logs a self-care nudge left un-acted past `SELF_CARE_ACK_WINDOW_MINUTES` as an
     `ignored` episode; the learner treats snooze *and* ignore as the same "not
     now" widen signal — catching the common case of ignoring rather than snoozing.
   - **Widens are verified before compounding.** Once an interval is past its
     default, a *further* widen is only taken if the push-back actually eased since
     the last one (`_widen_helped` — recent half of responses resists less than the
     older half); otherwise it holds. So the "nudge less" loop can't keep backing
     off on the unproven theory that less is better when the last step didn't help.

## Beyond v1 (from the README architecture)

- **Triage agent** ✅ **shipped** — the source-agnostic classify/prioritize/route
  step for any inbound signal (mail, calendar change, n8n event, manual capture)
  is built: `prefrontal/triage.py`'s `classify` (heuristic-first, LLM-assisted
  for ambiguous cases) + `apply` route into the existing tables (commitment /
  todo / episode / state), fire `triage.urgent` on urgent items, and log every
  decision to `triage_log` (idempotent on `(source, external_id)`). Wired at
  `POST /webhooks/n8n`, `POST /triage`, and `GET /triage/recent`; surfaced in the
  briefing ("worth a look") and a dashboard panel; n8n templates in
  `deploy/n8n/triage-{ingest,urgent}.workflow.json`. Design of record:
  [`docs/triage-agent.md`](docs/triage-agent.md). **The mail path is now absorbed
  into the one shared pipeline** ✅ — mail keeps its specialized classifier
  (`triage_message`: retention, categories, `waiting_on`, denylist/corrections)
  but routes/audits through `triage.apply` via a `Signal`/`TriageDecision` adapter
  (`prefrontal/mail/ingest.py`), so there's a single place that creates the todo
  and a single `triage_log` — one triage, not two. Two seams enable it without
  re-inferring the mail verdict: `apply` honors a caller-supplied `routed_ref`
  (linking an existing todo when a delegation loop closes) and the `todo` route
  creates a pre-built payload verbatim (skipping a second `augment_todo`).
  *(Next: fold `retriage_messages` in too — today it re-classifies in place and
  deliberately emits no `triage_log`; and land the Google Apps Script work-email
  digest as another `Signal` source into `POST /triage`.)*
- **Coaching agent** — **the engine, tick endpoint, and both v1 evaluators have
  shipped** (see `CHANGELOG.md`: `prefrontal/coaching.py` + `Module.evaluate`
  + Task Paralysis & Location Anchor evaluators + `prefrontal coach` +
  `POST /webhooks/coach/check`; `outing/check` now shares the engine's
  `evaluate_outing`). Specced in [`docs/coaching-agent.md`](docs/coaching-agent.md):
  a tick-driven decision engine that asks each module's `evaluate()` hook "what's
  due?", then decides whether to fire, what to say, and on which channel (learned
  `channel_response` + quiet hours + debounce), logging the outcome back as an
  episode. **Outcome logging (§8) has now shipped** — a delivered interactive
  nudge is tracked by the `(context, target)` a one-tap ack arrives on
  (`note_delivered`); a tap through `/nudge/act` records an *acknowledged*
  `channel_response` episode (`resolve_ack`), and a nudge left unanswered past an
  ack window is swept into a *miss* (`sweep_stale_nudges`, run each tick). Both
  feed the per-channel ack-rate `patterns.py` already derives, so the nightly
  `learn` makes `choose_channel`'s "bump the channel you ignore" real instead of
  floor-only. `POST /webhooks/coach/ack` is the explicit hook for delivery layers
  that report their own outcomes; covered end-to-end by `tests/test_coaching.py`.
  **The three closeout items have now shipped** (spec §12 step 5 + §13): the
  optional **LLM phrasing pass** — `phrase` warms `ambient` cues through the model
  on the opt-in `coach_llm_phrasing` key, profile-grounded, with a heuristic
  fallback (nudge/urgent/critical stay deterministic); the **encouragement layer**
  folded in as one more cue producer (`encouragement_cues`, collected by
  `run_coaching_tick`) so recovery routes through the shared channel/debounce/quiet-hours
  gate; and **`/webhooks/outing/check` deprecated** now that `coach/check` runs the
  byte-identical anchor decision (kept for existing n8n workflows, removed once the
  tick has run clean in the field). The coaching agent is now feature-complete.
- **Delivery layer + interactive nudges (ntfy)** — **the action-button core has
  shipped** (see `CHANGELOG.md`: signed `/nudge/act` + the `actions` fields
  on the outing/focus/departure nudges). Unlike Pushover — whose only
  interactivity is an "open this URL" link — ntfy notifications support inline
  `http` action buttons that fire a request *directly from the notification*,
  with no app switch, so **Wrap up / I'm back / Abandon / Made it / Missed it**
  — and now **Stay on task / Park it / Switch anyway** on the reflective pause —
  are one background tap.
  - **first-class Python Pushover / Ntfy / TTS publishing** ✅ — a native
    delivery client (`prefrontal/integrations/delivery.py`) now publishes a
    coaching `Decision` directly, no n8n publish node required. `NtfyClient` /
    `PushoverClient` / `TTSClient` (local macOS `say`) are the transports;
    `DeliveryClient.deliver` maps the engine's channel class
    (`digest`→held, `push`/`sound`/`voice`) to one, preferring ntfy so the
    inline action buttons render (Pushover carries the first action as a tap
    URL). Per-user routing (`resolve_route`) reads `ntfy_topic` /
    `pushover_user_key` / `tts_enabled` from `coaching_state` over operator
    defaults in `Settings` (the multi-tenant §6.5 delivery fields); all-empty is
    the local-first no-op (nothing leaves the box), and a down transport is
    reported, never raised. Quiet-hours + debounce are **not** re-done here — the
    engine's `suppressed`/`record_fired` already gated the decision; this layer
    only routes and sends. Wired into `prefrontal coach --deliver` (a launchd
    tick can now deliver without n8n); covered by `tests/test_delivery.py`.
  - **native Twilio voice call for the `critical`/`voice` channel** ✅ — the last
    thing the n8n delivery workflows still owned. `TwilioVoiceClient`
    (`prefrontal/integrations/delivery.py`) places the outing 150% escalation call
    natively via Twilio's REST API using **inline TwiML** (`<Say>`), so no public
    callback URL is needed — as self-contained and local-first as the other
    transports (no-ops when unconfigured; errors reported, never raised; shares the
    account creds + `normalize_phone` with the invite `TwilioSmsClient`). The
    `voice` branch now tries local **TTS** first (you're at the machine), then a
    **call** when Twilio is configured (you're out — the true critical case), then
    falls back to a max-priority push, so a `voice` cue is never dropped. Account
    creds + caller-ID are the operator's; the recipient (`twilio_to` / `TWILIO_TO`)
    is per-user routing like an ntfy topic and is withheld on a multi-user box so a
    call never rings the wrong phone. Covered by `tests/test_delivery.py`.
  - **launchd `coach --deliver` schedule** ✅ — `deploy/com.prefrontal-coach.plist`
    + `deploy/coach.sh` run the native tick every 60s (`coach --deliver
    --all-users`), fanning over every enabled module and delivering what fired —
    replacing the coach-check, hyperfocus-check, departure-reminder, and
    panic-check n8n poll workflows in one job. `coach.sh` also runs `prefrontal
    focus arm --all-users` first (shared `arm_focus_session`, idempotent), so the
    **focus-arm-check** workflow is native too — a live calendar focus block
    auto-starts a session with no poll. Per-user delivery via `resolve_route`
    (a user without their own target is computed but never delivered to the
    operator's device). Household sweeps and calendar/mail sync have their own
    native launchd jobs too (`com.prefrontal-{household,calendar,mail}.plist`).
  - **native morning briefing** ✅ — `prefrontal briefing --deliver [--all-users]`
    renders each user's digest and publishes it through their own `resolve_route`
    (channel `push`), so `deploy/com.prefrontal-briefing.plist` + `deploy/briefing.sh`
    deliver it daily at 7am with no `GET /briefing` → ntfy n8n hop — retiring the
    **morning-briefing** workflow. Covered by `tests/test_cli.py`.
  - **proactive panic nudge — first step inline + open-triage button** ✅ — the
    overwhelm nudge already carried the first step in its message; it now also
    returns an `actions` list (`panic_actions`, a signed-free ntfy `view` button)
    that deep-links to `GET /dashboard?panic=1`, which auto-opens the panic
    overlay on load. So the nudge delivers the one step to start *and* is one tap
    from the whole triage. `POST /webhooks/panic/check` returns the buttons (empty
    unless a public origin is set), and `deploy/n8n/panic-check.workflow.json` now
    publishes to ntfy with them (retiring its Pushover-only publish). Covered by
    `tests/test_panic.py` + `tests/test_notify.py`.

  - **ntfy is now the default delivery across the board** ✅ — every remaining
    Pushover-based n8n workflow was converted to publish to ntfy (the
    `prefrontal-me` topic): departure-reminder, coffee-shop-nudge (the 50%/100%
    pushes; the 150% Twilio call has since moved native too — see the
    `TwilioVoiceClient` bullet above), hyperfocus-check, calendar-sync
    (double-booking + possible-conflict alerts), and morning-briefing — joining
    panic-check, coach-check, interactive-nudge, and encouragement which already
    did. Each nudge workflow passes the endpoint's signed `actions` through so the
    one-tap buttons render. This also **cleaned up the Pushover-era workarounds**:
    - the `shortcuts://run-shortcut?name=End%20focus` deep link in
      hyperfocus-check is gone — the workflow now publishes the endpoint's real
      signed **Wrap up** button (background `GET /nudge/act`, no app-switch, no
      brittle name-match);
    - nudge copy that tiptoed around Pushover's lack of buttons is fixed (the
      hyperfocus check now says "tap Wrap up", not "open End focus");
    - the native delivery client (`delivery.py`) attaches buttons for the
      self-care **meal/water** cues too, not just outing/departure/focus;
    - the two-call `switch` → `resolve` flow / multi-tap menu was already single
      inline buttons for ntfy (the Shortcuts menu path remains for Pushover).

    Pushover isn't removed — the native `DeliveryClient` still supports it as a
    fallback (no inline buttons; the first action rides as a tap URL), and the
    `.env.example` documents both. Local-first stays the default (ntfy is
    self-hostable). Covered by `tests/test_delivery.py` + `tests/test_notify.py`.

  Local-first stays the default (ntfy is self-hostable). Pairs with the
  coaching-agent delivery routing and per-user delivery (multi-tenant) below.
- **Ingestion** — core mail monitoring has **shipped** (see `CHANGELOG.md`:
  `prefrontal/mail/` with IMAP fetch + n8n/Apps-Script batch sync), and mail
  ingestion is now **folded under the shared Triage agent** ✅ — `ingest_messages`
  routes/audits through `triage.apply` (`docs/triage-agent.md`). Still open: the
  Google Apps Script work-email digest as an alternative `Signal` source.
- **Optional Anthropic provider** ✅ — inference stays local by default, with an
  opt-in Anthropic API path selectable **per agent** via `ANTHROPIC_AGENTS`
  (`prefrontal/integrations/provider.py`: `ProviderResolver`). The reasoning-heavy
  agents — `assistant`, `summarizer`, `briefing`, `sensor`, `triage` — each
  resolve to Claude when opted in and a key is configured, otherwise the local
  Ollama client, all behind the shared `generate(prompt, system=)` interface.
  Selection is availability-only (no network) and safe: a shared `ProviderError`
  base means every agent falls back to Ollama when Claude is unavailable *or* a
  call fails, and keeps its deterministic fallback when both are down. The snappy
  in-loop inferences (window/title/kind, decomposition) stay local by design for
  latency. See "Inference providers" in `docs/guide.md`.
- **Encouragement & recovery layer** ✅ **(shipped — see `CHANGELOG.md`)** —
  the deterministic detector + recovery plan + `GET /encouragement` are live
  (`prefrontal/encouragement.py`, spec `docs/encouragement.md`), opt-in and
  tone-calibrated, alongside **panic mode** for the acute-overwhelm case. The
  **morning-briefing tone variant** (spec §6.2) is now shipped too
  (`encouragement.briefing_note`, woven into `briefing.py`): the brief closes with
  a day-shaped line — reassurance on a packed/rough or recently-rough stretch, and
  a relax-vs-accomplish choice on a wide-open day (`open_day_choice`,
  `POST /briefing/open-day`, `prefrontal open-day`) — in place of the usual bias
  nag. The final follow-on has now shipped: `assess_day` is wrapped as a
  **coaching-agent cue producer** (`encouragement_cues`, collected by
  `run_coaching_tick`), so the recovery message's delivery routes through the
  shared channel/debounce/quiet-hours engine — one `assess_day` feeding both the
  `GET /encouragement` read and the coaching tick (coaching-agent spec §9). The
  tick advances the once-per-day cursor only when the cue actually fires, so a cue
  held by quiet hours re-offers when the window opens.
- **Native Lock Screen Live Activity (live outing timer)** ✅ **shipped** — a
  glanceable widget refreshed only on iOS's ~15-min timeline cadence left an active
  outing's elapsed time stale between refreshes. The native **WidgetKit +
  ActivityKit Live Activity** now in `ios/PrefrontalWidgets/SessionLiveActivity.swift`
  counts up in real time on the Lock Screen and in the Dynamic Island: started on
  `outing/start`, updated as the escalation `level` changes, ended on return —
  reading the same `/outings` data and per-user token. It ships alongside the
  native Home Screen / Lock Screen glances and supersedes the retired Scriptable
  widget.
- **Context Packs — composition over primitives, not new modules.**
  Challenge-area modules answer *how your ADHD shows up* (time blindness,
  hyperfocus, impulsivity, …); a **Pack** answers *what life you're managing* —
  **Parent**, **Caregiver**, **Grad student**, **New job**. Crucially a Pack is
  **not** a `Module` subclass: modules live on the executive-function-challenge
  axis, and a role like "parent" is an orthogonal *life-context* axis (a parent
  still has time blindness — "parent" isn't a peer of "hyperfocus"). It's a higher
  **composition layer** that (1) switches on a relevant subset of challenge
  modules, (2) seeds domain vocabulary — commitment `kind`s (today `self`/`fyi`;
  add e.g. `child`), todo `categories`, default windows — and coaching prefs,
  (3) registers a few **situation tools** that are thin compositions of existing
  primitives (todos, commitments, departure, panic, `decompose_task`, the
  NL-assistant `ALLOWED_OPS` whitelist, delivery), and (4) tailors surfaces like
  the `/kids` household dashboard. Proactive cues route through the shipped `Module.evaluate`
  tick engine — a Pack lights up nudges by enabling modules that already evaluate,
  or by contributing its own cue source. **Example — Parent:** school-run
  departure (departure + a geocoded `place`), pack-the-bag checklist
  (`decompose_task`), "who's covering pickup?" (conflict detection + the `kind`
  classifier over a shared calendar), "kid's home sick → replan the day" (panic +
  todo re-fit into the changed windows). Deliberately mostly declarative + a small
  tool registry, so a Pack is cheap to add and shareable ("install the Parent
  pack") — the same opt-in, modular ethos as challenge modules, on the orthogonal
  context axis. *(Shipped: the **Parent-pack backbone** (household scope, the
  shared co-parent sheet, star charts, shopping list, delta digest, weekly
  check-in, self-serve invites, `/kids`) **plus the `Pack` registry/abstraction
  itself** (`prefrontal/packs/`): a declarative `Pack` — modules it switches on,
  todo categories, commitment kinds, coaching/window defaults — a registry enabled
  via `PREFRONTAL_PACKS` (opt-in, none by default), `prefrontal packs -v`, and the
  **`Parent`** pack as the first instance (turns on `time_blindness` +
  `task_paralysis`; declares `school`/`childcare`/`household` + the `child` kind;
  seeds parent time windows), **plus the `Caregiver` pack as the second**
  (`prefrontal/packs/caregiver.py`: aging parent / ill partner / disabled family
  member — turns on `time_blindness` + `task_paralysis` + `self_care`; declares
  `medical`/`admin`/`caregiving` categories; seeds daytime medical/admin windows,
  **arms the meal/water checks** with `self_care=on` since caregiver self-neglect
  is the signature risk, and protects `focus_target:personal`; sandwich generation
  can stack `parent,caregiver`). **Overlap precedence is defined**:
  `resolve_pack_vocabulary` unions categories/kinds and merges coaching defaults
  earlier-pack-wins in `PREFRONTAL_PACKS` order (seeding is absent-only, preserving
  that winner). **The `care` commitment kind, the `/care` caregiver surface, and the
  pack situation-tool registry have now shipped too** — `Pack.situations` carries
  read-only `SituationTool`s (a store→dict handler over an existing primitive),
  surfaced/run through the `packs` router (`GET`+`POST /packs/situations[/{tool}]`)
  gated on the owning pack. The **Parent** pack now carries its **full** tool set:
  **school-run** (the departure engine narrowed to `child` commitments),
  **pack-the-bag** (`decompose_task` broken over the kids' next events into a
  get-ready checklist), and **sick-day replan** (`build_panic` triage + a hard/soft
  split of today's commitments into must-cover vs. can-drop). To let a tool reach an
  LLM lever, the `SituationTool` handler now takes an optional `client`
  (`handler(store, *, client=None)`), resolved by the router; deterministic tools
  ignore it. Still open: situation tools for *other* packs (a Caregiver surface),
  more packs (Grad student / New job), and more tailored surfaces.)*
- **Shared household sheet — co-parent facts, agreements & load-balancing.**
  ✅ **Shipped** (`prefrontal/household.py`, `webhooks/routers/household.py`,
  `memory/repos/household.py`; `prefrontal household …`) — kept here for the
  design rationale. The first concrete driver for **household scope**: per-user
  data is `store.scoped(user_id)`, but co-parents need shared read/write. The data model comes straight from a real
  co-parent tracker (the *Kids Info Tracker* template — its own tagline names the
  goal: *"everything you need to step in and help — any time, no asking
  required"*):
  - **Facts** — per-kid, categorized key/values: sizes (tops / bottoms / shoes +
    brand notes), routines (wake / breakfast / ready-by), food (allergies +
    severity + EpiPen), health (pediatrician / dentist / insurance / meds),
    school & activities. Each stamped `updated_by`/`updated_at`.
  - **Agreements** — standing behavior plans with light structure: a star/points
    chart with *earn-only* rules and reward thresholds (e.g. 10⭐ → small, 30⭐ →
    big), plus the consistency notes both parents follow so the kids get one
    answer, not two.
  - **Key contacts** (role / name / phone / email) — a facts facet; and **running
    shopping needs** (item · child · size/spec · where to buy · got-it) reuse
    child-tagged **todos** rather than a new list.

  It renders at the **`/kids` dashboard** (with a read-only partner glance at
  `/family`) as a single visible sheet (*kids' facts · agreements · upcoming appts
  · recently changed*). Edit it in plain English through the **NL assistant**
  (`set_fact`/`set_agreement` in `ALLOWED_OPS`); appointments reuse commitments +
  the `child` `kind`. The point is **balancing invisible load**: provenance makes
  "who's carrying the mental load" legible, and the delta digest pushes the
  changes that matter to the *other* parent ("Sam's shoe size → 13; dentist Tue 3pm
  — you're on pickup"), so it's a load-balancer, not a passive shared note. It's
  the backbone of the **Parent** Context Pack. Full design:
  [`docs/household-sheet.md`](docs/household-sheet.md). *(Shipped: a real
  `household` entity users belong to; last-write-wins + provenance; the visible
  shared sheet **and** the proactive delta-digest, load-balance view, weekly
  mental-load check-in, shopping list, self-serve invites, and **recurring shared
  chores** — owner-assigned daily jobs whose lead-time reminder goes to the owner
  and whose miss-handoff goes to the *other* parent, so a slipped chore isn't a
  morning surprise.)*

- **"Have you eaten?" — a self-care nudge that pierces flow.** ✅ **(shipped — see
  `CHANGELOG.md`)** — the meal check is live as the `self_care` module: from a
  tunable start hour it re-asks *"have you eaten?"* even mid-focus, with one-tap
  **Ate** / **Snooze** on ntfy, capped once a day and gated on responsive hours.
  Off unless the `self_care` coaching key is `on`. **Water** shipped alongside meal
  (`water_check`, to a daily target), confirm/snooze **log `self_care` episodes**,
  and an adaptive-cadence learner tunes the interval. The basics pack has since
  grown to six: **meds** (target 1, opt-in), an open-ended **bio-break** reminder,
  a **wind-down / sleep** check (an evening once-a-day "start winding down"
  nudge, off by default like meds, that leans on the engine's quiet-hours gate so
  it never nags into the night), and a **movement / stretch** check (a "time to
  move" nudge anchored to the morning-coffee floor). *(The open "does it broaden into a self-care/basics
  pack?" question is effectively answered — the five checks cover the basics; a
  formal `Pack` bundle is possible but not required to use them.)*
## Iceboxed (out of scope)

- **Shopify MCP — record-shop assistant / used-collection buying.** ❄️
  **Permanently iceboxed.** A "Record Shop" small-business Context Pack (Shopify
  catalog/inventory/sell-through → reorder + used-lot appraisal) is too niche to
  justify the surface area (Shopify auth/scopes, a comps data source, a shop-vs-
  personal data boundary). Prefrontal stays focused on the ADHD executive-function
  mission. Left here as a deliberately-closed idea, not a backlog item.

Contributions toward any of these are welcome — see `CONTRIBUTING.md`.
