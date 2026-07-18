# Vacation mode

Status: **shipped** — manual toggle + suppression gate + auto-resume on return
(v1), the location-cued one-tap **entry suggestion** (v2), and the **native iOS
UI** (v3: Settings toggle, Today banner, one-tap notification button). Self-care
softening on vacation remains the one follow-up.
Author: drafted with Claude, 2026-07-17

## Question

Prefrontal's whole job is to *reach out* — departure reminders, self-care
checks, chore heads-ups, the morning brief. On vacation, most of that machinery
is noise: there's no 8:40 leave-by for the school run when you're on a beach four
timezones away. So we want a **vacation mode** that eases off.

The open design question (from the request):

> Not sure if it should be automatic by location — like it asks you — or if you
> switch the mode manually. What's more in line with our commandments?

## The short answer

**It's a false binary, and the commandments settle it.** The load-bearing path
should be **cue-based automatic detection — of both the leaving *and* the
return — surfaced as a one-tap confirmation**, with a manual toggle kept as an
always-available escape hatch. "Automatic by location, and it asks you" and
"switch it manually" aren't rivals; they're the primary path and its fallback.

The single most important reframe: **the failure mode that matters is not
forgetting to turn vacation mode *on*. It's forgetting to turn it *off*.** A
manual toggle you forget to flip back leaves the tool silent for weeks after you
get home — and per commandment 9's own corollary, *a muted tool is a dead tool*.
Location detection of the **return** is what makes vacation mode safe to have at
all. That alone tips the decision toward automatic.

## Why — commandment by commandment

Reading each relevant commandment (see
[`roadmap-vision.md` §2](../roadmap-vision.md)):

**5 — Cue-based, not time-based.** This is decisive. "Remember to switch on
vacation mode before you leave" *is* a time-based prospective-memory task — the
exact channel the commandment calls "the weakest," and the exact deficit the
whole product exists to externalize. Leaving your home radius is a **cue**, and
the system already detects it (`prefrontal/trips.py` `process_location`
emits `depart`/`return` edges across `home_radius_m`, with dwell/waypoint
tracking). The commandment doesn't merely *permit* location detection here — it
*mandates* cue over "ask the human to remember."

**7 — Never add maintenance burden; value with zero configuration.** A pure
manual toggle is upkeep in disguise: two prospective-memory acts per trip (on
before, off after). The *off* is the one that bites. Automatic resume-on-return
removes that upkeep entirely. This is the same commandment that the whole hosted-
onboarding effort exists to serve ([`hosted-onboarding.md`](hosted-onboarding.md))
— "the system carries the load" can't leak back out through a toggle the user has
to remember.

**9 — Silence is a feature; a badly-timed nudge erodes trust.** Points toward
erring quiet and toward keeping the confirmation lightweight. But it also warns
the *other* way: over-silencing (the forgotten-off case) kills trust just as
surely. The resolution: auto-detect the cue, but make entry a *suggestion the
user can wave off*, never a silent state change — and lean **eager on resume**,
because a returned-home phone is a high-confidence "vacation's over" signal.

**2 — Activation energy → zero; one tap; no forms.** A confirm-tap on a push
("Away till the weekend? I'll ease off the nudges — *Quiet down* / *No, I'm
local*") is one tap, right where attention already is. Digging into a settings
screen to flip a toggle from a standstill is *more* friction and, worse, requires
remembering the feature exists at all. The suggested-and-confirmed path is the
lower-activation one — the opposite of the naïve intuition that "manual is
simpler."

**1 — Externalize everything; if it's not visible, it doesn't exist.** Whatever
the state, it must be a visible banner ("🏝️ Vacation mode — nudges eased since
Tue · *Resume*"). The user must never *hold in their head* that they're in
vacation mode, nor wonder why it's gone quiet. This banner is also what makes a
false positive harmless: a long day-trip or a work offsite that trips detection
is one visible tap to dismiss.

**4 — Radically forgiving; design for the return, not the streak.** A vacation is
a *return* writ large. Coming home must **not** dump "here are the 40 things you
missed." Re-entry is a clean slate: resume quietly, at most one gentle "welcome
back — here's today," never a backlog avalanche. This is a requirement on what
resume *does*, independent of how it's triggered.

**10 — Don't automate nagging (households).** For a co-parent, vacation mode must
also pause *that person's* outbound chore relay and load-balance nudges while
they're away — without turning into "why isn't Tom doing his chores" surfaced to
the other parent. Away-ness suppresses the away person's obligations, it doesn't
reassign them.

## The synthesized design

### Trigger: detect the cue, confirm with one tap

Detection is **suggest, don't switch** on entry, and **lean-to-resume** on exit.
Any *one* of these cheap, already-available signals crossing threshold raises the
one-tap confirmation — none is required, and they compound confidence:

1. **Away-from-home dwell (primary).** Outside `home_radius_m` overnight for *N*
   consecutive nights. Reuses the trip/geo primitive that already tracks
   depart/return and dwell — no new sensor. (Overnight-away is what separates a
   vacation from a Tuesday errand; a single long day-trip never crosses it.)
2. **Calendar signal.** An all-day / multi-day event classified as OOO /
   "vacation" / "away," or a multi-day commitment far from home. The
   commitments + triage layer already classifies events.
3. **Mail signal (later).** A flight or hotel confirmation in the mail-triage
   stream. Cheap corroboration once the mail classifier is trusted for it.

Entry stays **conservative** (suggest, wait for the tap). Resume is **eager**:
returning inside the home radius is a strong "it's over" cue — still surface the
banner rather than hard-cutting, but default toward resuming.

### Manual toggle stays — as the escape hatch, not the primary path

Keep an explicit on/off (dashboard banner, iOS, `prefrontal vacation on|off`,
`POST /vacation`). It covers what location can't:

- **Staycations / at-home rest days** — home all week but off-duty; location
  will never catch this, so the human must be able to *assert* it.
- **Correcting a false positive** — the offsite that tripped detection: one tap.
- **Extending or ending early** — "one more day" / "back to it now."

But it is deliberately the *fallback*, not the mechanism. Making the human
remember the toggle is precisely the failure commandment 5 names.

### What vacation mode *does*: a receptivity profile, not a kill switch

Vacation mode is **not** "turn Prefrontal off." It's a **receptivity profile** —
which is exactly the shape the coaching engine already has, so this is an
extension, not a bolt-on:

- `prefrontal/coaching.py` `suppressed()` already gates non-critical cues on
  **quiet hours**, focus protection, and debounce.
- `in_quiet_hours()` is already factored out so other pollers (panic,
  encouragement) can defer to responsive hours.
- `_bypasses_silence_gates()` already defines the set that *always* gets through
  (`critical` + user-initiated).

Vacation mode is **"quiet hours that (a) span days, (b) are cued by location not
the clock, and (c) carry a slightly more generous allowlist."** It slots in as
one more discretionary gate in `suppressed()` / `decide()`, parallel to the
receptivity back-off. Concretely:

**Suppress** (routine-life maintenance that assumes you're home and on-schedule):
departure reminders to *routine* commitments, chore reminders + the co-parent
relay, the morning *work* briefing, focus-balance nudges, ambiguity sweeps,
todo-fitting. Soften rather than kill self-care (you still need to eat and drink
on a trip — ease the cadence, don't silence it).

**Keep** (still real on a trip): genuinely time-critical *real-world* commitments
that live on the calendar (a flight, a dinner reservation *on* the trip) — these
are already `critical` and already bypass discretionary gates. Panic mode and
emotion support are on-demand and must never be gated. Vacation ≠ off.

Because the "always gets through" set is already the `critical` /
user-initiated distinction, vacation mode needs almost no new machinery: it's
`in_quiet_hours()`'s bigger sibling, and the modules don't each re-check it —
one central gate, per §6.

## Recommendation

Build the **detect-and-confirm** model:

1. Location/calendar cue → **one-tap confirmation** to enter (conservative).
2. Return-home cue → **lean toward auto-resume** (this is the safety-critical
   half — it's why automatic wins over manual).
3. A **visible banner** the whole time, tappable to resume/dismiss (commandment
   1 + false-positive safety).
4. A **manual `vacation on|off`** toggle as the escape hatch for staycations,
   corrections, and extend/end-early.
5. Vacation mode = a **receptivity profile** layered into `suppressed()`, reusing
   `critical`/`_bypasses_silence_gates` for the keep-list — not a new kill switch.
6. **Clean-slate re-entry** on resume: no missed-item avalanche (commandment 4).

This answers the original question directly: *yes, automatic by location, and it
asks you* — the ask is **triggered by the cue** rather than initiated by a human
who has to remember the feature exists. Manual survives as the fallback because
some vacations (the at-home kind) have no location cue to catch. The primary path
is cue-based because the alternative — relying on prospective memory to flip a
toggle both ways — is the exact deficit Prefrontal exists to carry.

## What shipped in v1

The load-bearing core, end-to-end:

- **The mechanism** — `prefrontal/vacation.py`: a pure state core
  (`activate` / `deactivate` / `vacation_status` / `resume_on_return`) over the
  per-user `coaching_state` KV store (no new schema), plus `is_on_vacation` for
  the tick.
- **The suppression gate** — `coaching.build_context` threads `on_vacation` onto
  the per-tick context; `coaching.suppressed` holds every discretionary cue while
  it's on, checked ahead of quiet hours and ignoring `quiet_hours_exempt`.
  `critical` and user-initiated cues bypass it unchanged (a flight, a self-started
  outing still land).
- **Manual control** — `GET` / `POST /vacation` and `prefrontal vacation
  on|off|status`, the always-available escape hatch.
- **Automatic resume** — the trip state machine (`trips.process_location`) calls
  `resume_on_return` on a return-home edge and echoes `vacation_resumed` out
  through `POST /webhooks/location`, so a forgotten manual off can't leave the
  user muted. A staycation never departs, so it's never auto-lifted.

## What shipped in v2

The location-cued **entry suggestion** — the "automatic by location, and it asks
you" half:

- **The cue** — the `trip_tracking` module raises one `vacation_suggest` cue when
  the open trip passes `vacation.suggest_threshold_minutes` (default **2 nights**,
  per-user via `vacation_suggest_after_nights`) and vacation isn't already on. It's
  a `nudge` (respects quiet hours + receptivity), **fired once per absence** via
  the engine's fire-once guard (`last_fired` on the trip-keyed dedup) — an ignored
  ask never re-nags (commandments 9 & 10).
- **The decision** — `vacation.should_suggest_vacation` is a pure gate (threshold ·
  not-already-on · not-already-asked). It **suggests, never switches**: a work
  offsite that trips the dwell gets one tap-to-dismiss ask, not a silent mute.
- **The confirm** — a one-tap `🏝️ Ease off` button (`vacation_confirm`, wired
  through the same signed `nudge_actions` path as the other one-tap nudges) calls
  `activate(source="auto")`. A stale tap after returning home is a friendly no-op.

## What shipped in v3 (native iOS)

The in-app surface, so vacation mode is usable from the phone rather than only
the CLI/API:

- **Settings toggle** — a "Vacation mode" section (`VacationSection` in
  `SettingsView`) reads `GET /vacation` and writes `POST /vacation`; the manual
  escape hatch for a staycation, with status copy showing since/source when on.
- **Today banner** — while active, a visible "🏝️ Vacation mode — nudges eased
  off" banner with a one-tap **Resume** (commandment 1: the quiet is never a
  mystery). Auto-resume on return still does the heavy lifting; this is the
  manual "resume now".
- **One-tap notification button** — registered the `vacation_suggest` push
  category (`PushNotifications.swift`) so the server's `🏝️ Ease off` button
  actually renders on the suggestion notification (it was previously a
  buttonless banner, like `away_proposal`).
- New `Vacation` model + `vacation()` / `setVacation()` client methods; decode
  tests in `PrefrontalTests/APIClientTests.swift`.

The Swift app builds on a Mac — the iOS CI (`ios.yml`: SwiftLint +
`swiftc -typecheck` + `xcodebuild test`) is the gate.

Deferred: self-care softening on vacation (the one remaining open question below).

## Open questions for a follow-up build

- **Dwell threshold *N*.** One night is too eager (an overnight visit isn't a
  vacation); two–three nights is likely right, and could self-tune from how often
  a suggestion is confirmed vs. waved off (the same learning-loop spirit as the
  receptivity model).
- **Self-care on vacation: soften or keep?** Leaning *soften* (still eat/drink,
  looser cadence), but this is a per-user call and maybe a module setting.
- **Timezone shifts.** A vacation often crosses timezones; responsive-hours math
  should follow the phone's current tz, which `clock.local_datetime` already
  threads — worth confirming the trip path carries it.
- **Household visibility.** Should a co-parent *see* "Tom's away till Sunday" on
  the shared sheet (helpful context) without it becoming a nagging surface
  (commandment 10)? Probably yes, as neutral status only.
