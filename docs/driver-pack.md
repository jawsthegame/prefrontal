# Driver pack / Driving-mode module — design spec

Status: **not started — feasibility spec.** This scopes the "driving mode
automation" idea: fire when iOS **Driving Focus** turns on, and if you cut it
early (or the drive ends wrong) notice and learn from it. It also answers the
honest question that prompted it — *is there enough here for a whole pack?* —
before any code is written. If this and the code/roadmap ever disagree, this
file wins until the work lands.

It follows the shape of the wired lifecycle modules — **Hyperfocus** focus
sessions ([`hyperfocus.py`](../prefrontal/modules/hyperfocus.py)) and
**Closed-Loop Trip Tracking** ([`trips.py`](../prefrontal/trips.py)): a small
`start` / `check` / `stop` webhook lifecycle, a dedicated table, a pure testable
core, cues through the coaching engine, and a feedback loop into the profile.

---

## TL;DR — the verdict up front

**Reframe it as a small `driving` *module*, not a Parent-pack-sized *pack*.**

- The **Parent pack** is thin because a big *backbone* (the household sheet)
  shipped first and the pack just composes over it. There is **no comparable
  driving backbone**, so a "driver pack" today would be ~20 declarative lines
  wrapping one new behavior. The substance lives in the behavior — that makes it
  a **module**, with an optional pack wrapper later.
- The idea has a genuine, buildable core **if scoped to what iOS exposes.** iOS
  Shortcuts can fire a personal automation when a Focus **turns on or off** — so
  "Driving Focus was cut" is observable. iOS does **not** expose "the user
  tapped the screen while driving" to any third-party automation, so "ding me if
  I *use the phone*" is **not** directly buildable. The achievable, still-valuable
  slice is: **detect and learn from cutting Driving Focus early**, which is
  exactly the "you manually turned it off" half of the original ask.
- The **safe** place for the instant *ding* is the **iOS automation itself**
  (Play Sound / Speak Text runs locally, zero latency, no distracting round
  trip). Prefrontal supplies the **intelligence and memory**: was this a real
  arrival or a mid-drive cut, and the weekly pattern — not a real-time nag while
  you're at the wheel.

**Recommendation:** build the module (2 webhooks + 1 table + a classifier + one
briefing line + an iOS recipe — roughly `trip_tracking`-sized). Defer the pack
wrapper until a second commute feature exists for it to compose with. §9 has the
minimal ship list.

---

## 1. What the user asked for

> Having an automation fire when driving mode is on in iOS, and then if you use
> the phone or manually turn it off, it warns you or dings you somehow.

Three parts: **(a)** know when driving started, **(b)** notice misuse — *phone
use* or *manually turning it off*, **(c)** warn/ding. (a) and (c) are easy; (b)
is where the design has to be honest about the platform.

## 2. What iOS actually exposes (the feasibility gate)

| Signal | Available to Shortcuts? | Use |
|---|---|---|
| Driving Focus **turned on** | ✅ Personal Automation trigger | open a driving session |
| Driving Focus **turned off** | ✅ Personal Automation trigger | close it → classify |
| CarPlay **connected / disconnected** | ✅ Automation trigger | high-confidence "really driving" / "arrived" |
| Screen tap / app opened *during* the drive | ❌ not exposed | — (can't observe) |
| Continuing motion / location | ✅ via `POST /webhooks/location` | infer "still driving when Focus was cut" |

Consequences that shape the whole design:

1. **"Warn me if I use the phone" is not generally buildable.** There is no
   phone-usage event. Don't promise it. (You *could* have a specific app run a
   shortcut that pings "I'm in an app while a driving session is active," but
   that only covers apps you've wired, not casual use — noise, not a feature.)
2. **"Manually turned it off" *is* buildable** — that fires the Focus-off
   automation. This is the real target.
3. **The Focus-off event also fires on a normal arrival** (Driving Focus
   auto-ends when you stop / disconnect CarPlay). So the central job is
   **classification**: *arrived* (clean, silent) vs *cut early* (still moving /
   nowhere near a destination). Get this wrong and every safe arrival nags — the
   fastest way to get the feature turned off. See §5.

## 3. Where the "ding" belongs

Interrupting someone with a push **while they are driving** is itself the hazard
the feature exists to reduce. So split it:

- **Instant, local ding = the iOS automation.** The Focus-off automation can
  **Play Sound** or **Speak Text** ("Driving Focus off — are you parked?") the
  moment it fires, locally, before/without any network call. Zero latency, works
  offline, and it's the *user's own device warning them* — the least-bad thing to
  do mid-drive.
- **Intelligence + memory = Prefrontal.** The same automation POSTs the event;
  Prefrontal decides whether it was a real cut, logs it, and surfaces the
  **pattern** later (after arrival, or in the morning briefing) — never an
  interrupting push mid-session. This matches the project ethos: *capture then
  defer*, *learn from failure*.

This division means the core "warn me" behavior works even with Prefrontal
unreachable — and Prefrontal adds the part it's uniquely good at.

## 4. Data model

A dedicated `driving_sessions` table, additive via the guarded
`_ADDED_COLUMNS` / idempotent-schema migration path used by every other table:

| Column | Notes |
|---|---|
| `id`, `user_id` | standard, per-tenant scoped like every user-owned table |
| `started_at`, `ended_at` | UTC `TS_FMT` strings |
| `start_lat`/`start_lon`, `end_lat`/`end_lon` | snapshots from `get_location()` at open/close (nullable — location is best-effort) |
| `status` | `active` → `arrived` \| `cut_early` \| `abandoned` |
| `linked_commitment_id` | the departure this drive was *for*, if a leave-window matches (reuse `departure.attribute_departure`) |
| `linked_trip_id` | the closed-loop trip this drive belongs to, if trip tracking is on |
| `moved_after_stop` | did location keep changing after Focus-off? (the key cut-early tell) |
| `source` | `driving_focus` \| `carplay` — carplay disconnect is a stronger "arrived" signal |

No new episode *type* is required: a resolved session logs an existing
`episode` (see §7) so it flows into the same learning pass everything else does.

## 5. The classifier (the heart of it)

`classify_driving_stop(session, *, location, now, home, commitment)` → one of
`arrived` / `cut_early` / `unknown`. Pure and testable, mirroring
`trips.classify_reflection` / `departure.classify_departure`.

Precedence, high-confidence first:

1. **CarPlay disconnect** (`source="carplay"`) → `arrived`. Disconnecting the car
   is arrival by definition; never nag.
2. **At a destination** — end location within `home_radius_m` of home, or within
   a small radius of the linked commitment's `dest_lat/lon`, or a known `place`
   → `arrived`.
3. **Still moving** — a fresh location delta since Focus-off (poll `/driving/check`
   once ~60–90 s later, compare `end_lat/lon` to current) → `cut_early`. This is
   the "turned it off at a light / mid-drive" case.
4. **Far from anywhere known, no movement read** → `cut_early` (soft) — you
   stopped, but not where you were headed.
5. **No location signal at all** → `unknown` → *no nudge*; at most an ambient
   briefing ask ("did you cut Driving Focus early yesterday?"). Never guess-nag.

A `driving_cut_grace_minutes` (default ~2) coaching key absorbs the case where
you genuinely arrived and cut it a beat before the phone registered the stop.

## 6. Lifecycle endpoints

Mirrors `focus/{start,check,end}` — new `routers/driving.py`:

- **`POST /webhooks/driving/start`** — Focus-on automation. Opens a session,
  snapshots location, links the soonest departure commitment (`attribute_departure`)
  and any open trip. Idempotent: an already-active session is returned, not
  stacked (like `focus/arm`).
- **`POST /webhooks/driving/check`** *(polled by n8n, optional)* — the deferred
  brain. Runs the classifier on a session Focus-off opened as pending; fires the
  **deferred** cue once *arrival is confirmed* (so it never interrupts the drive);
  auto-closes a session left `active` far past a plausible drive as `abandoned`
  (the focus-abandon pattern). Read-only sibling `GET /driving` for the dashboard.
- **`POST /webhooks/driving/stop`** — Focus-off automation. Closes the session,
  runs `classify_driving_stop`, records the episode (§7). Returns a **speakable
  `confirmation`** so the automation can read it back locally (the §3 ding). On
  `cut_early` it does **not** push mid-drive — it stamps the session so
  `/driving/check` (or the next briefing) surfaces it once you've stopped.

One-tap escape hatch: **"I was a passenger"** — a signed `/nudge/act` action (the
same mechanism as Wrap-up / I'm-back) that reclassifies the last session and
suppresses its learning signal, so passenger drives don't train a false pattern.

## 7. Learning loop

A resolved session logs a `task` episode (no prediction, so it never pollutes the
shared `time_estimation_bias`, exactly like trips and departure outcomes):

- `arrived` → `outcome="success"` (or no episode — a non-event).
- `cut_early` → `outcome="miss"`, `notes="driving focus cut early"` — an
  impulse-control signal that feeds `drift` and the **Impulsivity** profile.

The `learn` pass needs no new math; the module's `profile_section` surfaces the
streak ("Cut Driving Focus early **3×** in the last 7 days"), and the **morning
briefing** gets one quiet line when the count is non-zero (silent otherwise, per
the briefing's "quiet when there's no signal" habit). That behavioral memory —
not a mid-drive buzz — is the real intervention.

## 8. Coaching cues (module `evaluate()`)

The `DrivingModule.evaluate()` returns, at most:

- an **`ambient`** cue for a `cut_early` session awaiting acknowledgement (rides
  the digest, deduped per session id) — *never* `urgent`/`critical` while a
  session is `active`, because the coaching engine would happily push it and that
  is the one thing we must not do mid-drive; and
- a weekly **`ambient`** "you've been cutting Driving Focus early" nudge when the
  count crosses a threshold, deduped to the ISO week (the `trip_tracking`
  focus-balance pattern).

Everything else — channel, quiet hours, debounce — is the coaching engine's job
(`URGENCY_FLOOR`, `suppressed`). Ambient → `digest`, so it can't interrupt.

## 9. Minimal ship list (module first)

1. `driving_sessions` table + additive migration; store CRUD.
2. `prefrontal/modules/driving.py` — pure core (`classify_driving_stop`,
   grace/threshold defaults, `evaluate`, `profile_section`), registered.
3. `routers/driving.py` — `start` / `stop` (+ `check`, `GET /driving`).
4. One briefing line + the profile section.
5. `deploy/ios-shortcut.md` recipe (§11) + an n8n `driving-check` workflow.
6. `tests/test_driving.py` covering the classifier's five branches.

Comparable in size to `trip_tracking`. Notably **not** required for v1: the pack
wrapper, real-time anything, the passenger button (nice-to-have).

## 10. The pack wrapper (optional, later)

If/when a second commute-context feature exists, a **`commute` pack** becomes
worthwhile — a thin `Pack` (see [`packs/base.py`](../prefrontal/packs/base.py)):

```python
COMMUTE_PACK = Pack(
    key="commute",
    title="Commute / Driver",
    description="Driving-focus safety and getting-out-the-door timing for a "
                "regular commute.",
    modules=("driving", "impulsivity", "time_blindness", "trip_tracking"),
    categories=("commute",),
    coaching_defaults=MappingProxyType({
        "driving_cut_grace_minutes": "2",
        "focus_target:commute": "…",   # if we fold commutes into focus-balance
    }),
)
```

That's the whole pack — which is precisely why it isn't the unit of work today.
Ship the module; add this when it has friends to compose with.

## 11. iOS automation recipe (the concrete glue)

Two **Personal Automations** (Shortcuts → Automation → +), each with *Ask Before
Running* **off** so they fire silently (iOS 17+ runs Focus automations
unattended):

**"Driving on"** — Trigger: *When Driving is turned on* →
1. **Get Contents of URL** → `POST http://<mac>:8000/webhooks/driving/start`,
   header `X-Prefrontal-Token`, JSON body `{"source": "driving_focus"}`.

**"Driving off"** — Trigger: *When Driving is turned off* →
1. **Speak Text** / **Play Sound**: "Driving Focus off — are you parked?" *(the
   instant, local ding — §3)*.
2. **Get Contents of URL** → `POST /webhooks/driving/stop` (same header).
3. (Optional) **Speak** the response's `confirmation`.

Optional high-confidence pair: duplicate both against *When CarPlay
connects/disconnects* with `{"source": "carplay"}` — a CarPlay disconnect is a
clean "arrived."

## 12. Risks & open questions

- **False nags on normal arrival** — the make-or-break risk. Mitigated by the
  §5 classifier (CarPlay + destination + movement) and by only ever surfacing
  *cut-early* patterns ambiently. Start conservative: `unknown` → silence.
- **Passenger drives** — you legitimately use the phone as a passenger. Mitigated
  by (a) never nagging on a single event, only repeat patterns, and (b) the
  "I was a passenger" one-tap that suppresses that session's learning.
- **Driving Focus auto-enables** (motion/Bluetooth/CarPlay) — good, means zero
  taps to start a session; but confirm the on-automation fires for *auto* enables
  on the target iOS version, not just manual ones.
- **No location feed** — degrades gracefully to `unknown`/silence; the feature is
  strictly better with the existing `/webhooks/location` ping running.
- **Scope creep into "phone use"** — resist it; the platform doesn't support it
  and a wired-per-app hack would be noise. Keep the promise to what §2 allows.
