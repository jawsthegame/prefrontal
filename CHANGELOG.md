# Changelog

Shipped work in Prefrontal. This is the release-notes companion to
[`ROADMAP.md`](ROADMAP.md), which now tracks only what's *planned* or open.
Entries are moved verbatim from the old roadmap, so a few inline "see below" /
"see §N" references point into `ROADMAP.md`'s forward-looking sections.

## Recently shipped

- **Web-configurable location settings** ✅ (#565, epic #569) — the location
  **tunables** now live on the web dashboard, keeping the phone's Settings to just
  the master on/off (only it can trigger the OS "Always" prompt). New
  `GET/POST /schedule/location-settings` reads/writes four coaching-state keys —
  `home_radius_m` (the existing shared key, also read by outing gating + trip
  detection), `geofence_radius_m`, `location_post_interval_s`, and
  `location_visits_enabled` (the API fields drop the `location_` prefix:
  `post_interval_s` / `visits_enabled`) — with a `LocationSettings` Pydantic schema
  (bounded, partial writes). A "Location" card in
  `settings.html` edits them; the iOS `LocationMonitor` fetches them on
  enable/launch into the App Group and applies them (geofence radius, significant-
  change floor, `CLVisit` on/off). Shape pinned by an OpenAPI snapshot + example
  fixture + contract test (`tests/test_contract_location_settings.py`), mirroring
  the `available_hours` pattern; endpoint behavior covered by
  `tests/test_location_settings.py`. iOS is client-only (build on a Mac).

- **Self-Care: evening nudges bypass the daytime quiet-hours window** ✅ — an
  evening nudge collided with the shared *daytime* responsive-hours window: with
  the seeded `responsive_hours_end` of 14:00 a 21:00 **wind-down** cue (and the new
  end-of-day **gap review**) was silenced before it ever landed. Both now **bypass
  quiet hours by default** and are individually configurable. A new
  `Cue.quiet_hours_exempt` lets a non-critical cue skip *only* the quiet-hours gate
  (still debounces, still respects focus protection) without escalating to the
  voice channel the way `critical` would. Wind-down bypasses via
  `winddown_bypass_quiet_hours` and now self-bounds with its own `winddown_end_hour`
  (default 23:00) — the bedtime it used to approximate by leaning on the engine's
  gate — so it can't nag into the small hours; turn the bypass off to restore the
  old "leans on responsive hours" behavior. The gap review bypasses via
  `self_care_review_bypass_quiet_hours` and can't nag regardless (once/day,
  evening-only). All settable from the Settings page and `POST /self-care` (per-check
  `bypass_quiet_hours`; a `review` block for enabled/hour/bypass) and surfaced on
  `GET /self-care`. Covered by `tests/test_self_care.py` /
  `tests/test_self_care_review.py`.

- **Self-Care: end-of-day gap review** ✅ — the self-care checks already *log*
  every Ate / Drank / Went click as a timestamped `self_care` episode; this reads
  those clicks back at day's end as a **timeline** and names the gaps a raw tally
  hides. Three findings beyond "did you hit the target": **late first** (you drank
  all six glasses, but the first wasn't until 3pm — flagged even when the quota was
  met, because *when* matters), **long gap** (six hours between two bio breaks vs. a
  2h cadence), and **shortfall** (a quota that finished the day short) — plus a
  plain **none** for an enabled check with nothing logged, and a "what went well"
  line so it's never pure scolding. The analysis is a pure read
  (`prefrontal/self_care_review.py`) shared by three surfaces: `prefrontal
  self-care review` (CLI) and `GET /self-care/review` (JSON) are always-available
  pulls, and an **opt-in evening push** (`self_care_review_enabled`, off by default
  like meds/wind-down; from `self_care_review_hour`, default 21:00) fires once at
  day's end — but only when there's a gap worth naming, so a clean day stays
  silent. Like wind-down it bypasses the daytime quiet-hours window by default (see
  the entry above) so an end-of-day recap actually lands; it can't nag regardless,
  since it fires at most once a day and only from the review hour onward. Declared
  as the `self_care_review` intervention; covered by `tests/test_self_care_review.py`.

- **Docs: retire the Shortcuts location automations for native-app users** ✅
  (#567, epic #569 capstone) — with the native CoreLocation feeds all shipped
  (#562/#469/#563/#564), `deploy/ios-shortcut.md` now leads with a banner + table
  mapping each of the four location Shortcuts ("Update location", "Leaving Home",
  "I'm back" arrival, coarse departure-by-location) — plus the Home Assistant
  Tier-2 continuous gating — to its native equivalent, and each of those sections
  carries a "native app: not needed" note. The Shortcuts stack stays documented
  for **web / ntfy-only** users (no app). `ios/README.md` now states the app fully
  replaces the location Shortcut automations. Docs-only.

- **iOS: location permission UX hardening** ✅ (#566, epic #569) — enabling
  location used to fire a bare `requestAlwaysAuthorization()` with no handling of
  what came back. The Settings "Location automations" section is now a dedicated
  `LocationSection` that observes `LocationMonitor`'s live `authorization`:
  always-on **priming** text explains what the one-shot system prompt is for; a
  **Permission** status row shows the true state (Always / While Using / Denied /
  …); a **While-Using** grant surfaces an "Upgrade to Always" button (the one
  re-prompt iOS allows), since background auto-logging needs Always; and a
  **denied/restricted** state shows an "Open iOS Settings" affordance. The opt-in
  toggle is reconciled against reality — a denial or a revoke made in the Settings
  app flips it off (monitoring stops) rather than leaving a silent no-op on.
  Client-only (build on a Mac); no server change.

- **iOS: `CLVisit` monitoring — arrivals/departures at arbitrary venues** ✅
  (#564, epic #569) — `LocationMonitor` now runs `startMonitoringVisits`
  alongside the #469 geofences and the #562 significant-change feed, so a stop at
  a venue beyond the ≤18 curated `/places` still feeds `/webhooks/location`
  (battery-cheap, wakes from terminated). Both edges post the visit coordinate;
  closing an outing stays home-only (the home ring, #563) — an arbitrary-venue
  arrival just refreshes position and lets the server decide. All three
  `/webhooks/location` feeds now share a `postLocationDeduped` guard (App-Group
  persisted, ~150 m / 120 s) so a `CLVisit` coinciding with a curated-place
  geofence crossing doesn't double-post. Client-only (build on a Mac); reuses the
  existing endpoint, no server change.

- **iOS: native arrival home closes the active outing** ✅ (#563, epic #569) —
  restores the Tier-1 "I'm back" Shortcut natively. `POST /webhooks/location`
  only *stores* the phone's fix; the server's passive home-return close runs on
  a coach tick and is confirmation-prompt + grace gated, so for a Shortcut-free
  native user arriving home neither closed the outing off the location post nor
  promptly. `LocationMonitor.didEnterRegion` now, on entering the **home** region
  with an outing active, posts `/webhooks/outing/return` to close it tap-free —
  gated on a read-only `GET /outings` check so a routine arrival home with no
  outing never posts a spurious return. Non-home arrivals still just refresh
  `/webhooks/location` (arbitrary venues are #564's `CLVisit` job). Client-only
  (build on a Mac); reuses existing endpoints, no server change.

- **iOS widget: a concrete "right now", and goodbye Scriptable** ✅ — the native
  WidgetKit medium widget's right column read "3 fit 45m" — a cryptic count under
  a "RIGHT NOW" heading that also doubled as "what's next". It now shows the one
  todo the server suggests starting (`todos/now.suggestion`), e.g. *Reply to
  landlord · ~15 min · 45m free* — the same initiation nudge the Today tab's
  "Right now" card gives. The next commitment moved to its own full-width
  **Next: … · 3:40 PM** footer, so the heading means one thing. Same concrete
  suggestion now backs the small widget and the Lock Screen rectangular/inline
  accessories, which lets the widget drop its extra `todos/fit` request (the
  suggestion already rides in the `todos/now` payload). Client-only (build on a
  Mac). Retired the legacy **Scriptable** widget (`deploy/scriptable/`) — the
  native app widget + Live Activity supersede it.

- **iOS: swipe to hide a "Did you make it?" item** ✅ — the calendar's
  recently-elapsed list assumed every past event wants a made/missed answer, but
  some were only FYIs or things you never had to go to. A left-swipe on any
  "Did you make it?" row now hides it (a full swipe fires outright; a short one
  reveals a **Hide** button) via the existing `POST /commitments/{id}/hidden` —
  which `previous_commitments()` already excludes, so the row clears immediately
  and stays gone across a calendar re-sync (and out of every upcoming surface).
  Added a reusable `SwipeToReveal` container in
  `ios/Prefrontal/Views/Shared.swift` (the card layout has no `List`, so no
  `.swipeActions`); it uses a simultaneous gesture so vertical scrolling still
  works over a swipeable row. Client-only (build on a Mac); the server endpoint
  is covered by `tests/test_commitments.py`.

- **iOS significant-location-change feed** ✅ (#562, epic #569) — `LocationMonitor`
  now runs `startMonitoringSignificantLocationChanges` alongside the #469
  geofences, so coarse (~500 m / cell-tower) position updates keep
  `/webhooks/location` fresh **between** curated places — what departure
  travel-time and trip stop-detection need — replacing the Shortcuts "Update
  location" automation. Battery-cheap and wakes the app from terminated; posts
  are throttled to a 5-minute floor (App-Group-persisted so the throttle survives
  a relaunch; becomes web-configurable under #565). Same Always-location opt-in,
  off by default. Client-only; build on a Mac.

- **Fix: stale "Recent nudges" (legacy NULL-expiry rows)** ✅ — `recent_nudges`
  kept rows with a NULL `expires_at` eligible forever, so nudges predating the
  expiry default (`DEFAULT_NUDGE_TTL_HOURS`) lingered on the iOS Today card and
  the widget indefinitely — the card read as "all things from long ago." NULL rows
  now fall back to the same TTL measured from `created_at`, so an ancient legacy
  nudge ages out like any other while active nudges (all of which carry an expiry)
  are unaffected. Covered by `tests/test_departure.py`.

- **iOS self-care local notifications** ✅ (#474, follow-up) — completes the
  offline-tolerant local nudges. `GET /self-care` now returns a per-check
  **`next_due`** (UTC) — the next future local time that check wants a nudge,
  computed from its start hour + cadence with the same pace model as the
  overdue/"behind" flag, capped to the check's window (its end hour, else the
  responsive-hours end) so nothing schedules overnight
  (`prefrontal/modules/self_care.py:_next_due`). The iOS app schedules a local
  `UNNotificationRequest` per due check on each Today refresh
  (`LocalNotifications.reconcileSelfCare`), so meal/water/meds/wind-down/movement
  nudges still fire off the tailnet; open-ended bio breaks (no fixed clock time)
  are excluded, and requests reconcile each refresh so a satisfied/off check drops
  out. Server side covered by `tests/test_self_care.py`; the client is Swift
  (build on a Mac).

- **iOS morning briefing — rendered Markdown** ✅ — the Today briefing card showed
  the server's raw Markdown (`## headers`, `- bullets`, `**bold**`) because
  SwiftUI's `Text` only parses Markdown from string *literals*, not a runtime
  `String`. Added a small `MarkdownText` view (`ios/Prefrontal/Views/Shared.swift`)
  that splits the digest into blocks, renders section headers/bullets, and parses
  inline emphasis via `AttributedString` — dropping the leading `# Morning briefing`
  title (the card already shows it). "Show more/less" now truncates by rendered
  line count. Client-only.

- **Trips & balance page — a visual surface for closed-loop trips** ✅ — closed-loop
  trips were tracked and surfaced through notifications, the briefing, and the JSON
  API, but nowhere to *look at*. This adds a read-only `/trips/board` page (in the
  shared nav as **Trips**): the open trip if you're out now, the recent history with
  duration / distance / category / life-sphere / self-reported outcome + the honest
  reflection note, the trips still awaiting a label, and the **focus-balance** rollup
  — horizontal bars per life-sphere (shop/work/home/kids/personal) with a weekly-aim
  marker and an amber "light on this" flag when a targeted sphere runs under half its
  aim, over a 7- or 30-day window. Same self-contained shell as the other web
  surfaces; reads `GET /trips` and `GET /balance` client-side. Labeling still happens
  from the one-tap trip notification. Covered by `tests/test_trips.py`.

- **iOS token hardening — shared Keychain** ✅ (#496) — the bearer token moves out
  of App Group `UserDefaults` (unencrypted in the container and in backups) into a
  shared **Keychain access group** both the app and the widget carry
  (`ios/Prefrontal/Config/KeychainStore.swift`), with
  `kSecAttrAccessibleAfterFirstUnlock` so the widget/background refresh can read it
  while locked. Existing installs migrate transparently on next launch
  (`SharedStore.migrateTokenIfNeeded()` copies the legacy value over and wipes the
  plaintext copy; the `token` getter falls back to the old value until then, so a
  widget refresh mid-migration still authenticates). The base URL + ntfy hints stay
  in the App Group (not secret). Needs the paid Apple Developer account (same tier
  App Groups already require). Closes the piece deferred from QR onboarding (#473).

- **Available hours by day of week** ✅ — a per-weekday availability schedule the
  user edits in Settings. Each weekday is either **off** or a single available
  window (`HH:MM–HH:MM`); the slot-finder (`/calendar/slots`) and the assistant's
  "find me a time" now search **only** inside a day's window and skip an
  unavailable day entirely, superseding the single flat off-zone complement
  per-day (`WindowConfig.band_for_weekday` → `find_slots(band_for_weekday=…)`).
  Stored as the schema-less `available_hours` coaching key (JSON mirroring the API
  shape, so an off day keeps its band for when it's toggled back on); unconfigured
  = the previous flat-band behaviour, so it's a no-op until set. New
  `GET/POST /schedule/available-hours` (partial writes; `AvailableHours` /
  `DayAvailability` schema). Editable on the **web dashboard** ("Available hours"
  card) and in the **iOS** Settings screen (a per-weekday toggle + two time
  pickers — the app's first server-backed editable preference). **Drift guard:**
  because the
  contract is hand-mirrored across Pydantic ↔ web JS ↔ Swift, a committed OpenAPI
  snapshot + example fixture (`tests/contracts/available_hours.*`,
  `tests/test_contract_available_hours.py`) fails CI on any structural change
  until the mirrors are updated in lockstep. Server + web + drift infra covered by
  `tests/test_available_hours.py`.

- **iOS geofencing — auto-log leaving home** ✅ (#469) — an **opt-in** location
  automation (Me ▸ Settings) that monitors your curated places (`GET /places`)
  with `CLCircularRegion` geofences (`ios/Prefrontal/Location/LocationMonitor.swift`).
  Leaving the place named **home** posts `/webhooks/departure/left` — the native
  replacement for the "when I leave Home" Shortcut automation — and any
  enter/exit posts the position to `/webhooks/location`, so departure timing and
  outing distance capture without a tap. Battery-cheap region monitoring (the OS
  wakes the app only on a crossing, even from terminated; `AppDelegate` re-attaches
  the delegate on launch). Requires Always-location (prompted only when you turn
  it on); off by default. Client-only.

- **In-app new-user Guide — a per-module walkthrough** ✅ — a `/guide` page (in
  the shared nav) that walks a first-time user through each of *their* enabled
  modules: what it helps with, what Prefrontal will do, and a reassuring "nothing
  to switch on." The walkthrough is derived from each module's own `challenge` +
  `interventions()` (new `Module.tutorial()` → `TutorialStep`s in
  `modules/base.py`), so it always matches what the deployment actually runs and a
  new module surfaces its guide automatically. Each module can be marked *Read*
  and the whole tour reset (`GET /guide/data`, `POST /guide/seen`, `POST
  /guide/reset`) — progress is per-user (`guide_seen` coaching state) and the guide
  is always re-readable, never a one-shot. Same content is available offline via
  `prefrontal modules --tutorial [key]`. Covered by `tests/test_guide.py`.

- **APNs — native iOS push, client side** ✅ (#467, part 2) — the app now opts a
  device into native Apple Push: on notification authorization it registers for
  remote notifications and `AppDelegate` posts the token to the new
  `POST /route/apns-token` (stored as the user's per-user `apns_token`). Nudges
  arrive as native notifications whose action buttons — `UNNotificationCategory`s
  mirroring `_NUDGE_BUTTONS` (I'm back / Ate / Wrap up / …) — fire the signed
  `/nudge/act` URL from the payload on tap, the native equivalent of ntfy's inline
  buttons. Adds the `aps-environment` entitlement; the server (part 1) falls back
  to ntfy for any device that hasn't registered. Endpoint covered by
  `tests/test_apns.py`; the client is Swift (build on a Mac).

- **APNs delivery transport (server side of native iOS push)** ✅ (#467, part 1) —
  a native-push transport alongside ntfy/Pushover/Twilio in
  `prefrontal/integrations/delivery.py`. When a user has registered an
  `apns_token` (per-user `coaching_state`, a targeting field withheld on
  multi-user boxes) **and** APNs is configured, `DeliveryClient` delivers via
  Apple Push — with the cue's `context_key` as the notification `category` (the
  app's action buttons) and the signed action URLs in the payload — falling back
  to ntfy if it doesn't land, so ntfy stays the default for everyone else.
  Token-based (ES256 .p8) provider auth in `integrations/apns.py`; creds via
  `APNS_*` env (operator-shared, like Twilio). Needs the `prefrontal[apns]` extra
  (APNs requires HTTP/2 via `h2`); without it the transport reports unavailable.
  Set a token with `prefrontal user route --apns-token`. Covered by
  `tests/test_apns.py`. The **client** half (device-token registration +
  `UNNotificationCategory` action handlers + entitlement) is part 2.

- **iOS Live Activities — outing/focus countdown on Lock Screen + Dynamic Island** ✅ (#466)
  — a running outing shows a self-ticking **"back by" countdown** and a focus
  session an **elapsed timer**, live on the Lock Screen and in the Dynamic Island.
  A shared `SessionActivityAttributes` (`Models/`) is rendered by
  `PrefrontalWidgets/SessionLiveActivity.swift`; `Activities/LiveActivityManager`
  **syncs** activities against the active session on each Today refresh (start when
  one appears, end when it's gone), so it works regardless of how the outing/focus
  was started (app, App Intent, widget, ntfy). The clock uses SwiftUI
  `Text(timerInterval:)` / `.timer`, so it stays live with **no push updates** —
  no APNs needed. `NSSupportsLiveActivities` added to the app Info.plist. Client-only.

- **iOS signing — set the Team ID once, for good** ✅ — the tracked
  `ios/Signing.xcconfig` shipped with an empty `DEVELOPMENT_TEAM`, so every fresh
  clone reset it and the `git update-index --skip-worktree` workaround didn't
  survive re-clones — you'd re-enter the team in Xcode and `xcodegen generate`
  would wipe it. Now `Signing.xcconfig` `#include?`s a **git-ignored**
  `Signing.local.xcconfig` (template: `Signing.local.xcconfig.example`): put your
  Team ID there once and it survives clones and regeneration, never committed, no
  skip-worktree. See `ios/README.md`.

- **Channel-choice auto-act — damp a non-predictive channel signal** ✅ (learning
  §4) — the channel-calibration walk-forward was report-only; now it closes the
  loop like the bias auto-act. When `channel_calibration` finds per-channel
  ack-rates *don't* predict held-out acks (a noisy signal that would still push
  `choose_channel` to bump a cue up a rung), the nightly `learn` pass damps each
  channel's stored `channel_response` rate toward the sample-weighted pooled rate
  (`decay_channel_rate_toward_pooled` / `pooled_channel_rate`) — collapsing the
  spread so the noise stops crossing the ignore-threshold. Controlled by
  `channel_decay_on_miss` (default 0.5 = halve the deviation; `0` = old report-only
  behavior), records `channel_calibration_decayed`, and `prefrontal learn` reports
  when it damped. This leaves only the harder, design-blocked **sensor causal
  check** open in the learning loop. Covered by `tests/test_patterns.py`.

- **iOS offline-tolerant local notifications** ✅ (#474) — while the app has
  network it now schedules a **local** "leave by" notification for the next
  departure (`/departure/next` → `leave_by`), so the alert still fires at that
  time even if the phone has since gone off the tailnet, where ntfy/APNs can't
  reach it (`ios/Prefrontal/Notifications/LocalNotifications.swift`). Reconciled
  on every Today refresh — a moved/cancelled departure replaces or clears the
  pending one rather than double-firing — and a no-op unless notifications were
  authorized during onboarding. Scoped to departures (the one nudge with a
  concrete fire time); self-care local scheduling is a follow-up (no next-due
  timestamp in the API yet). Client-only.

- **iOS widget-connection diagnostic** ✅ — a read-only **Diagnostics** section in
  Me ▸ Settings shows the App Group id, whether the shared store initialized, and
  whether the base URL + token are present in the shared container the widget
  reads. Pins down the "widget says Tap to connect while the app works" case: if a
  token shows here but the widget doesn't see it, the App Group capability isn't
  provisioned into the widget target (fix on both targets in Signing &
  Capabilities, same Team, then reinstall). Never prints the token itself.

- **Freeform calendar assistant — "find me a time"** ✅ — a conversational
  scheduler (`prefrontal/availability.py`): say *"find 45 min for coffee with Sam
  this week"* or *"when are my wife and I both free for dinner tomorrow evening?"*
  and get back open slots — or a single clarifying question when the ask is too
  vague (almost always a missing duration). It parses duration + timeframe +
  time-of-day + **who's involved** from free text (Claude/Ollama when available,
  else an offline heuristic that never needs a model), then finds slots over the
  existing engine (`find_slots`). The key move is **participant-aware
  constraints**: your own commitments always block, but a partner's whereabouts —
  the *FYI* events ("where someone else will be") — block **only** when the plan
  involves them. So "just me" ignores items that are only your wife's, while "the
  two of us" treats her calendar as a hard constraint too; the reply reports how
  many FYI items it ignored so you can pull them back in. Surfaced as
  `POST /assistant/find-time` (structured slots + a `question` + a ready-to-send
  `text`) and `prefrontal find-time "…"`. Read-only — it never writes.

- **iOS delegation — hand a todo to the AI agent or a VA** ✅ — the native Todos
  tab now has the delegation flow the web dashboard already offered (parity, not a
  new server feature). Each open todo gets a **Delegate** action → a sheet to pick
  **AI agent** (local model writes a brief + drafts + action items back onto the
  todo) or **email a VA** (recipient pick-list from `/todos/delegate-recipients` +
  cover note), with optional pasted context, calling `POST /todos/{id}/delegate`.
  The row shows a status pill (prepping → prepped/sent/returned/failed) and an
  inline panel with the brief, action items (your own get one-tap **＋ Todo**),
  draft messages, and **Mark returned** (`/todos/{id}/delegate/return`). Client-only.

- **iOS configurable Lock Screen self-care ring** ✅ (#465) — the circular Lock
  Screen accessory was hardcoded to water; it's now a separate **configurable**
  widget (`ios/PrefrontalWidgets/SelfCareCircleWidget.swift`) — long-press → Edit →
  pick which check it tracks (Water / Meals / Meds / Breaks / Wind-down /
  Movement) via an `AppIntentConfiguration` + `SelfCareCheck` app enum. The glance
  fetch now carries every enabled check (not just meal/water), and the old
  hardcoded circular was removed from the main static widget.

- **Per-module next steps — one net-new capability per challenge module** ✅ — with
  every module's declared interventions already wired, each challenge module gained
  one honest next surface, all composing existing primitives:
  - **Time Blindness — learned morning-routine lead.** `adapt_morning_routine`
    (nightly `learn` pass) reads recent early-start `departure` episodes and nudges
    the static `morning_routine_minutes` toward what would have made those mornings
    on-time — bounded, deadbanded, never overriding a hand-set value. The
    `morning_prep` "Set alarm" wake time now reflects how long you actually take.
  - **Task Paralysis — a real body-double session.** `start_body_double` opens a
    short, aligned focus session on the stalled task's tiny first step (composing
    the focus primitive, so the focus check/end gives the end check-in), and in a
    household invites a co-parent to start theirs too. `POST /todos/{id}/body-double`
    and `prefrontal body-double`.
  - **Hyperfocus — learned personal soft-block length.** `adapt_soft_block` learns
    `hyperfocus_block_minutes` from rated focus sessions (mean of
    `should_have_stopped` durations, else long `worth_it` blocks), so the soft
    `alignment_check` lands at *your* diminishing-returns point, not a generic 90 —
    bounded below the hard ceiling, never overriding a hand-set value.
  - **Impulsivity — captured-impulse retro.** A weekly, ambient review of still-open
    parked impulses (`parked_impulses` → `captured_impulse_retro_text`) closes the
    capture-and-defer loop — keep the real ones, drop the noise. `GET /impulses/parked`.
  - **Location-Aware Task Anchor — learned per-errand windows.** `learned_window` /
    `resolve_time_window` pre-fill a windowless outing with your typical duration for
    that errand (grouped by intention) before falling back to the LLM/heuristic, so a
    recurring run escalates against your real pace from minute one (`history` source).
  - **Closed-Loop Trip Tracking — multi-stop trips.** Passive dwell detection in
    `process_location` records a `trip_waypoints` row each time the phone lingers away
    from home past `trip_dwell_minutes`, so a chained errand run splits into its legs
    (surfaced in the label ask, the trip episode note, and the profile). New
    `trip_waypoints` table + dwell-candidate columns (auto-migrated).
  - **Self-Care — auto-satisfy from other signals.** `auto_satisfy_from_signals`
    (each tick) cross-references recent returned outings, completed trips, and past
    commitments against per-check keywords: a lunch outing / calendar lunch satisfies
    the meal check, a logged workout the movement floor — so the checks read as
    attentive, not oblivious.
  - **Delegation — stalled hand-off escalation.** After `delegation_stall_misses`
    (default 3) ignored check-ins on a forwarded hand-off with no movement, the copy
    escalates from "heard back?" to `stalled_handoff_message` — take it back,
    re-delegate, or drop it.
  - **Projects — staleness → next action.** A staleness nudge now pairs "still on
    it?" with a tiny first step for the project's oldest open todo
    (`project_open_todos` + a stored or heuristic `decompose_task`), so re-engaging
    is one concrete action rather than a yes/no.

- **iOS offline capture queue + background refresh** ✅ (#468) — off the tailnet,
  a capture used to just fail and vanish. Capture writes — Add Todo, self-care
  marks, Made it/Missed it — are now `queueable`: on a transport failure
  `APIClient` persists them to an App-Group-backed `OfflineQueue`
  (`ios/Prefrontal/Config/OfflineQueue.swift`, shared by app + widget + intents)
  instead of erroring, and replays them oldest-first when the app next
  foregrounds — plus opportunistically via a **Background App Refresh** task that
  also reloads the widget. Today shows an "N changes waiting to sync" banner while
  the queue is non-empty. Stateful lifecycle writes (focus/outing start/return)
  are intentionally not queued (a deferred replay would log a bogus session);
  delivery is at-least-once, acceptable for todos/self-care. Client-only.

- **iOS widget — one-tap end an active outing/focus** ✅ (#465) — completes the
  interactive-widget scope: when an outing or focus session is running, the Home
  Screen widget now shows a **I'm back** / **Wrap up** button that ends it in
  place via `ImBackIntent` / `EndFocusIntent` (already compiled into the widget
  target since #471) — no app launch. The glance fetch now also reads
  `/outings` + `/focus`; an active session takes visual priority over the
  departure/what-fits glance. Builds on the interactive self-care chips (#465)
  and the App Intents (#470).

- **iOS Control Center controls — Panic / I'm Back / Wrap Up Focus** ✅ (#471) —
  the widget extension now ships iOS 18 **Control Center controls**
  (`ios/PrefrontalWidgets/PrefrontalControls.swift`) for the no-input actions,
  each firing the matching App Intent without opening the app; add them in
  Settings ▸ Control Center or assign one to the Action Button. To share the
  intents with the extension, the action intents were split out of the app-only
  `AppShortcutsProvider` (now `Intents/AppShortcuts.swift`) into
  `Intents/PrefrontalIntents.swift`, compiled into both targets. Input actions
  (Add Todo / Going Out / Start Focus) stay in Siri/Shortcuts, which can prompt.
  Builds on #470.

- **iOS interactive widget — tap-to-log self-care** ✅ (#465) — the Home Screen
  widget's meal/water chips are now **interactive** (iOS 17 `Button(intent:)`):
  tapping logs a meal or a glass of water in place via a new `MarkSelfCareIntent`
  (`ios/Prefrontal/Intents/WidgetActionIntents.swift`) and WidgetKit reloads the
  timeline, so the count updates without opening the app. The intent authenticates
  off the App Group like every other widget read, and is compiled into *just* the
  widget target (not the app-only `AppShortcutsProvider`). Builds on the App
  Intents from #470.

- **iOS App Intents — Siri / Shortcuts / Spotlight / Action Button** ✅ (#470) —
  the native app's core actions are now first-class **App Intents**
  (`ios/Prefrontal/Intents/PrefrontalIntents.swift`), retiring the hand-built
  "Get Contents of URL" shortcuts and their pasted tokens: **Add Todo**, **Panic**,
  **Going Out** / **I'm Back**, **Start Focus** / **Wrap Up Focus**, and **Made It** /
  **Missed It**. Each authenticates like the widget (`APIClient(shared:)` off the App
  Group), so it runs in the background without launching the UI and reloads the
  widget timeline after a state change; parameterized intents (todo title, outing
  intention + window, focus task + length) prompt for their values. Siri phrases
  are registered via an `AppShortcutsProvider`, and any action is assignable to the
  Action Button. See `ios/README.md`.

- **iOS in-app depth — morning briefing + commitment outcomes** ✅ (#477) — the
  native app surfaced only a slice of what the API returns. Today now shows the
  **morning briefing** (`GET /briefing`): the server-rendered digest in a
  collapsible card with a 👍/👎 control (`POST /briefing/feedback`) that steers the
  briefing voice. Calendar now closes the behavioral-learning loop with
  **commitment outcome logging** — the `/commitments` payload's `previous` list
  (recently-elapsed events awaiting an answer) renders a "Did you make it?" card
  with one-tap **Made it / Missed it** (`POST /commitments/{id}/outcome`), and a
  recorded answer shows with a Change affordance that clears it. Client-only, no
  server changes. See `ios/README.md`.

- **Caregiver Context Pack — the second life-context pack** ✅ — after Parent,
  `PREFRONTAL_PACKS=caregiver` (`prefrontal/packs/caregiver.py`) installs the
  caregiving life-context for someone looking after an aging parent, ill partner, or
  disabled family member. Declarative like Parent: it switches on `time_blindness`
  (appointment/med timing) + `task_paralysis` (dreaded insurance/benefits admin) +
  `self_care`; declares `medical`/`admin`/`caregiving` todo categories with daytime
  windows; and — distinctively — **arms the meal/water checks** (`self_care=on`),
  because the person most likely to skip meals is the one doing the caring. It also
  protects the caregiver's own out-of-home time (`focus_target:personal` + the
  focus-balance nudge). Sandwich generation can stack both: `PREFRONTAL_PACKS=parent,caregiver`
  (overlap precedence is earlier-pack-wins, as before). No new machinery — it slots
  into the existing `Pack` registry/vocabulary backbone; the pack-specific
  situation tools, a caregiver surface, and a dedicated `care` commitment kind are
  the next slice. Covered by `tests/test_packs.py`.

- **iOS onboarding — scan a QR to connect** ✅ — the native app's whole first run
  was one screen where you hand-typed a tailnet URL and pasted a ~43-char token, and
  nothing tied it to the ntfy / Shortcuts / widget setup the operator hands over
  separately. It's now a four-step walkthrough — welcome → connect → notifications →
  done — that's **QR-first**: point the camera at the code on your setup sheet (or
  scan it with the iOS Camera app, which opens the new `prefrontal://connect` deep
  link) and the base URL + token fill themselves in; manual entry stays as a
  fallback. Connect validates against `/self-care` before advancing, so a bad token
  fails there with the server's own error. The notifications step prefills the
  user's ntfy topic (copyable) and requests native-alert permission. Operators
  produce the QR with `prefrontal user connect-link <handle> --qr [--rotate]` (QR via
  the optional `prefrontal[qr]`/segno extra; the plain link always prints). See
  `docs/design/ios-onboarding.md`. Covered by `tests/test_cli.py`.

- **Outings pre-file their life-sphere at declaration** ✅ — `/webhooks/outing/start`
  already accepted a `domain`, but nothing set one unless the caller passed it. It
  now resolves the sphere at declaration — an explicit `domain` wins, else it's
  inferred from the intention text (`infer_domain_from_text`: "swim with the kids" →
  kids; a domain-less "grab a coffee" stays unassigned rather than force-fit) —
  persists it on the outing, echoes it in the new `OutingStarted.domain` field, and
  names it in the confirmation ("Filed under kids."). So more outings arrive
  pre-filed for the focus-balance rollup instead of needing a retrospective tag; the
  "Going out" iOS Shortcut recipe gains a domain **Choose from Menu**. Correct a
  wrong guess via `/webhooks/outing/domain`. Covered by `tests/test_location_anchor.py`.

- **Configurable trip quick-file domains** ✅ — the trip-label ask's one-tap
  file-into-a-sphere buttons were hard-coded to 🏠 Home / 🧒 Kids / 🙋 Me (ntfy
  caps action buttons at 3). They're now driven by a `trip_quick_domains` coaching
  key: pick any ≤3 of shop/work/home/kids/personal and the buttons follow (a
  shopkeeper can surface 🛒 Shop / 💼 Work / 🙋 Me). `resolve_quick_domains`
  snaps synonyms onto the canonical vocabulary, de-dupes, caps at 3, and falls back
  to the default trio when unset/invalid; the `trip_tracking` module resolves it once
  per tick and stamps the set on the cue's `ref`, so both delivery paths (the
  `coach/check` fan-out and the native client) build the same per-user buttons via
  the new `notify.trip_label_actions` (with `DOMAIN_BUTTON_LABELS` covering all five
  spheres). Covered by `tests/test_focus_balance.py`.

- **One-tap trip retro — close label + domain + reflection in a single call** ✅ —
  a completed trip's retrospective used to need three separate posts
  (`/webhooks/trip/label`, `/webhooks/trip/domain`, `/webhooks/trip/reflect`). The
  new `POST /webhooks/trip/retro` bundles them: send any of label / category /
  domain / reflection in one request and it labels, files the life-domain, and runs
  the full reflection path (classify → resolve the trip's episode into drift signal
  → hand the note to the LLM-as-sensor for pending proposals), returning one
  speakable `confirmation`. `trip_id` is optional — it defaults to the most recent
  trip still awaiting a label, so a bare-tap Shortcut needn't carry the id. The
  **Trip retro** iOS Shortcut recipe (`deploy/ios-shortcut.md`) closes the whole
  retrospective from the notification without opening the dashboard. Each part
  reuses the exact `label_trip`/`set_trip_domain` + `apply_reflection` logic the
  single endpoints do (no behavior fork); the three endpoints stay for the one-tap
  domain buttons and partial edits. Covered by `tests/test_trips.py`.
  - Also fixes a latent import cycle (`delegation → sources → mail.imap →
    mail/__init__ → ingest → delegation`) that surfaced when `test_trips.py` was
    collected in isolation: `prefrontal/mail/ingest.py` now imports `delegation`
    and `triage` lazily (inside the functions that use them) instead of at module
    top, so the mail package no longer re-enters a partially-initialized
    `delegation` regardless of import order.

- **One triage, not two — the mail path is absorbed into the shared pipeline** ✅ —
  mail ingestion used to run its own classify→route→log as a parallel triage: it
  created the todo with `add_todo` and separately *mirrored* an audit row into
  `triage_log`. It now routes through the **one** shared `triage.apply`
  (`docs/triage-agent.md` reality note). Mail keeps its specialized *classifier*
  (`triage_message` — retention policies, categories, `waiting_on`, learned
  denylist/corrections); what changed is that an actionable verdict is expressed as
  a generic `Signal` + `TriageDecision` (adapters in `prefrontal/mail/ingest.py`)
  and handed to `apply`, so there's a single place that creates the todo and a
  single `triage_log`. Two small seams on the shared core make this lossless:
  `apply` now honors a caller-supplied `routed_ref` (mail linking an existing todo
  when it closes a delegation loop), and the `todo` route creates a caller-supplied
  pre-built payload verbatim (mail's specialized title/notes/priority/domain/
  project) instead of running `augment_todo` a second time. Behavior is preserved:
  only needs-action/delegation-matched mail is audited (informational mail stays in
  `/mail`), suppressed mail still logs a `drop`, the mail todo keeps its provenance
  (`source="manual"`, `[mail/<account>]` notes), and no `triage.urgent` nudge is
  introduced (mail passes no n8n client). The audit row's `received_at` now reflects
  the mail's own receipt time (as the generic path already does), rather than
  ingest time. `retriage_messages` (in-place re-classification that deliberately
  emits no new audit row) is intentionally left on its direct path. Covered by
  `tests/test_mail.py` + `tests/test_triage_apply.py`.

- **Coaching agent — the three closeout items land, so it's feature-complete** ✅ —
  the last of the coaching-agent spine (`docs/coaching-agent.md`) is in:
  - **LLM phrasing pass (§5)** — `prefrontal.coaching.phrase` now warms `ambient`
    cues through the model in Prefrontal's coaching voice, grounded in the user's
    structured profile (built once per tick and shared across cues), with a
    heuristic fallback to the deterministic `cue.text` on any provider failure.
    It's opt-in (the `coach_llm_phrasing` coaching key) and applies **only** to
    `ambient` cues — `nudge`/`urgent`/`critical` keep their deterministic templates
    so a synchronous model call never sits on a time-critical delivery path (§13).
    Resolved under the non-`KNOWN_AGENTS` agent name `coach`, so it stays local
    unless the operator opts every agent into Anthropic; the profile is only read
    when phrasing is on and an ambient cue is present, so the default path pays
    nothing. Covered by `tests/test_coaching.py`.
  - **Encouragement folds in as a cue producer (§9)** — the rough-day recovery
    layer is no longer a separate delivery path: `prefrontal.encouragement.encouragement_cues`
    wraps the **same** `assess_day` / `build_recovery` / `render_encouragement`
    core the standalone `GET /encouragement` endpoint uses (one implementation, not
    two) as an `evaluate`-style producer. `run_coaching_tick` collects it alongside
    the module cues, so the recovery message routes through the shared
    `choose_channel`, `suppressed` (quiet hours + debounce), and delivery path. The
    once-per-day cursor (`last_encouragement_date`) is advanced only when the cue
    actually *fires* — held by quiet hours, it re-offers when the window opens,
    exactly as the old `/encouragement` → `/encouragement/sent` contract behaved.
    Tone-calibrated prose rides the same `coach_llm_phrasing` key via
    `summarize_encouragement`; off, the deterministic render is delivered. The
    standalone endpoint stays (a pure read for dashboards, sharing the cursor so
    there's no double delivery). Covered by `tests/test_encouragement.py`.
  - **`/webhooks/outing/check` deprecated (§13)** — the endpoint is now marked
    `deprecated=True`: `coach/check` fans over every module and
    `LocationAnchorModule.evaluate` runs the byte-identical per-outing decision
    (`evaluate_outing` + `apply_outing_evaluation`, including the passive
    home-return close and abandon auto-close), and the native launchd `coach
    --deliver` tick already delivers the escalation. The old endpoint stays for
    existing n8n workflows and will be removed once the coaching tick has run clean
    in the field. Deployment note in `docs/deployment.md`.

- **Bio-break chip goes green once you confirm — until the next reminder** ✅ —
  the self-care card's bio-break check is *open-ended* (a recurring reminder, not
  a daily quota), so it never reached the "done" green state the other checks show
  — tapping **Went** left it looking the same as before. It now has a `satisfied`
  state: confirming greens the chip (with a ✓) and clears the amber "due" pulse
  until the next reminder comes due, at which point it reverts. `satisfied` is the
  open-ended analog of a quota check's `done` — set only by a confirm (a *snooze*
  deliberately doesn't green it, since snoozing isn't going), tracked via a new
  `biobreak_confirmed_until` cursor, and mutually exclusive with `overdue`. Removing
  the only logged Went (a mis-tap correction) un-greens it. Verified in a real
  browser (amber-due → tap Went → green ✓) and covered by `tests/test_self_care.py`.

- **Shared chores card shows today's chores by default** ✅ — the Household
  sheet's Shared chores card used to list *every* chore regardless of whether it
  ran that day, so a card with a dozen weekly/monthly chores buried the handful
  actually due today. It now shows only the chores scheduled for the selected day
  (today by default), with a **Show all** toggle to reveal the rest for editing.
  "Which day" stays server-owned (the deployment's timezone): `build_sheet` now
  stamps each chore with `scheduled_today` (its effective, routine-inherited
  schedule falling on today's local date), and `GET /household/chores/done` now
  also returns the day's `scheduled` id set so the day selector filters yesterday
  correctly too. A day with nothing scheduled says so, with an inline Show-all
  link. New `chore_ids_scheduled_on` helper backs the endpoint; covered by
  `tests/test_chores.py`.

- **Admin UI hides disabled users by default (+ re-enable)** ✅ — a disabled user
  is clutter in the operator's list, so the Users card now shows only **active**
  users, with a **“Show N disabled”** toggle to reveal the rest. Because hiding
  them would otherwise strand a mistaken disable at the CLI, disabling is now
  reversible from the UI: shown-disabled users get a **Re-enable** button backed by
  a new `POST /admin/users/{handle}/enable` (the inverse of `…/disable`,
  operator-only, idempotent). The status line reads `N active · M disabled`.
  Verified in a real browser (hidden by default → reveal → re-enable → toggle
  disappears). Covered by `tests/test_admin.py`.

- **Operator-only Admin link in the top nav** ✅ — reaching `/admin` meant knowing
  the URL. Every shared-nav page now carries an **Admin** link that's hidden by
  default and revealed only for operators: a small shared script (one file,
  injected into each shell — dashboard, calendar, household, kids/pets, insights,
  review, settings) calls `GET /admin/whoami` and shows the link when
  `is_operator` is true. A non-operator never sees it, and `/admin` stays
  operator-gated server-side regardless, so the link is convenience, not a
  security boundary. Verified in a real browser across operator / non-operator /
  signed-out.

- **Google sign-in email lives on the user record (self-serve, no env edit)** ✅
  — Google sign-in used to map a verified email → user only through the
  `GOOGLE_OAUTH_ALLOWED` **environment variable**, so letting a newly-provisioned
  co-parent sign in with Google meant editing env + restarting the box —
  completely disconnected from the admin UI that created them. The email now lives
  **on the user row** (a new nullable `users.email`, uniquely indexed, riding the
  same `backfill_added_columns` migration `household_id` used). The Google callback
  resolves the verified address against the DB first (`get_user_by_email`), falling
  back to the env allowlist so existing deployments keep working. Managed from the
  `/admin` UI — an email field on "Add a user" and an inline **✉ Google sign-in**
  editor per user — plus `POST /admin/users/{handle}/email` and the CLI
  (`prefrontal user add --email`, `prefrontal user email <handle> [email]`). Emails
  are normalized (lowercased/stripped) on one shared path so write and lookup can't
  drift, and are unique across users (a 409 / non-zero exit otherwise). Covered by
  `tests/test_admin.py`, `tests/test_oauth.py` (DB-email sign-in, disabled-user
  refusal), and `tests/test_cli.py`.

- **Operator user-management UI (`/admin`)** ✅ — provisioning a co-parent used
  to be CLI-only (`prefrontal user add` on the box), which is a real onboarding
  wall: the Household sheet's access-code gate wants each person's *own* user
  token, so a partner with no provisioned user just sees "That code didn't work."
  no matter what code they type. `/admin` closes that gap with a self-contained
  operator page (same theme/nav shell as `/settings`), driving the existing
  `require_operator`-guarded `/admin/*` endpoints: add a user (token shown
  **once**, with a Copy button), rotate/disable, create a household, and wire each
  user into it so both co-parents share the sheet. It reads `GET /admin/users`
  and a new `GET /admin/households` (households + their members, via
  `HouseholdRepo.list_households`) and renders per-user household badges + a
  picker. Auth is the same client-side pattern as every other surface — Google
  session or an operator access code — and the page distinguishes a bad code
  (401) from a valid-but-non-operator account (403) at the gate. A non-operator
  who reaches the page can't read or write anything (the endpoints are all
  operator-gated). Covered by `tests/test_admin.py` (list-households view,
  operator/401/403 gating, and the page serves unauthenticated).

- **Coaching engine: `location_anchor.evaluate` is side-effect-free (audit #407 H2)** ✅
  — the last leak from the coaching-abstraction audit. `Module.evaluate` is
  contracted to *return cues, never write*, but `LocationAnchorModule.evaluate`
  called `apply_outing_evaluation` inline (closing outings, logging episodes,
  recording nudges) — non-substitutable and unsafe for a dry-run, unlike its
  siblings. Fixed by moving the writes into the `after_fire` lifecycle hook (added
  in #407 H1): a shared `_active_outing_evals` does the pure per-outing decision,
  `evaluate` turns firing ones into cues, and `after_fire` re-runs the identical
  (deterministic) evaluation and applies the writes. Because `after_fire` runs
  every tick regardless of cues, the **cue-less** transitions the deferral worried
  about — passive home-return, auto-abandon — still apply (this is why it couldn't
  key off `decisions`). The `after_fire` hook now receives `ctx` (symmetric with
  `before_collect`), giving it `ctx.now` + location. The `/webhooks/outing/check`
  endpoint is unchanged and stays in parity via the shared `evaluate_outing` /
  `apply_outing_evaluation`. New tests cover a tick applying the level advance and,
  crucially, a cue-less auto-abandon still closing the outing. Closes #407.

- **Calendar sync tolerates a single bad event** ✅ — `sync_calendar` used to
  validate the whole batch up front and reject *all* of it if any one event failed
  (`normalize_event` raising), so a single malformed VEVENT — an unparseable time,
  a missing field — silently killed the user's entire feed until it aged out. Now
  each event is validated independently: a bad one is **skipped and logged**, the
  good ones sync, and `SyncSummary.skipped` / `skipped_titles` report what was
  dropped (surfaced by `prefrontal calendar sync` and the `/webhooks/calendar/sync`
  response). Document-level parse failures still fail loudly upstream in
  `parse_ics`. Closes the hardening issue that followed the titleless-VEVENT fix.

- **Delegation on the dashboard todo cards** ✅ — the delegate hand-off is now a
  first-class control on each todo, not just an API/CLI/assistant-box action. A
  **Delegate** button opens a small popover — *🤖 Prep with AI* (one-tap agent
  hand-off) or *✉ Email an assistant…* (reveals an address field; the SMTP outbox
  is auto-picked from the todo's account/domain). Once a todo has a delegation it
  shows a **status pill** (🤖 prepped / ✉ sent / ⚠ needs a hand / ↩ returned) and an
  expandable **prep panel** with the brief + any drafted messages, plus a *Mark
  returned* button — so the agent's work is actually readable in the UI, closing
  the gap where `GET /todos` returned the delegation but nothing rendered it. Also
  fixes the prep to use the longer-timeout **summarizer** client (the 10s inference
  client would often time out a brief+drafts generation to the heuristic outline).

- **Per-account SMTP for the email hand-off** ✅ — the delegation email route now
  supports **several named SMTP outboxes** instead of one. A delegated todo
  auto-sends from the account whose name matches its **mail account**, then its
  **domain** (a work-mailbox / work-domain todo → a `work` outbox), falling back to
  `default` — or the sole account when only one is configured
  (`sources.resolve_smtp_for`). The Settings page manages the list (add / edit /
  remove, with the user's mail-account names suggested for matching); `GET /smtp`
  returns all accounts (passwords never echoed), `POST /smtp` upserts one by
  `account`, and `DELETE /smtp/{account}` removes it. Sources stay Fernet-sealed
  per user.

- **Delegate a todo to an assistant (prep / follow-up hand-off)** ✅ — some open
  loops are less "do a tiny first step" and more "someone should go dig up the
  options, draft the email, and hand it back ready to send." `prefrontal/delegation.py`
  is that hand-off: a todo is delegated to a pluggable **handler** that does the
  prep and writes it onto a new `todo_delegations` row (one per todo, mirroring
  `todo_decompositions`). Two handlers ship, chosen from a registry (`HANDLERS`
  derived from `_HANDLERS`, so the API can't accept a handler it can't dispatch):
  `agent` — the local model writes a research **brief** + **draft communications**
  straight back on-box (status `prepped`, ready to review, house-style LLM call
  with a heuristic fallback so it still produces an outline offline); and `email`
  — the same brief is composed into a message and sent to a human VA over the
  user's own SMTP source (status `forwarded`; if SMTP isn't configured or the
  relay errors, the brief is still stored and the status is `failed` so nothing is
  lost). Lifecycle `forwarded → in_prep → prepped → returned/failed`, with a
  heads-up push (`deliver_to_member`) when it lands. This is Prefrontal's first
  **outbound-email** path (`prefrontal/integrations/smtp.py` — stdlib `smtplib`, a
  no-op when unconfigured, never raises); SMTP credentials live as a **per-user
  Fernet-encrypted `sources` row** (`kind="smtp"`), configured on the Settings page
  (`GET`/`POST /smtp`, the password sealed at rest and never echoed back). Surfaces:
  `POST /todos/{id}/delegate` + `/delegate/return` (the delegation rides along on
  `GET /todos`), a `prefrontal todo delegate` CLI command, and an NL `delegate_todo`
  op on the `/assistant` box ("have the assistant prep the dentist call" → an
  agent hand-off; "get my VA on X, email jane@…" → an email one). The assistant's
  execute layer now threads the selected model client through `execute_actions`
  so the op can write its prep brief.

- **Ambiguity clarification + guided playbooks (a Task-Paralysis lever)** ✅ —
  task paralysis has a quieter cause than size: you can't start what you can't
  *name*. A calendar event called "Tax" or a todo that just says "Mom" stalls
  because it could mean several different things. `prefrontal/clarify.py` gives the
  system a way to notice that ambiguity and hone it in before it becomes another
  avoided loop: a pure `ambiguity_score` heuristic (short / single-word /
  known-ambiguous titles, discounted when a clear action verb or a concrete detail
  is present) gates a local-model pass that proposes ONE clarifying question with a
  few candidate readings (`detect_clarification`, LLM-first with a hand-authored
  heuristic fallback so the "Tax" case works fully offline). The question lands as
  a **pending** `clarifications` row — the same propose-then-confirm safety model
  as the LLM sensor — and is surfaced inline in a dashboard "Needs clarification"
  card. Answering it records the chosen reading (a `todo`'s notes are honed
  non-destructively), and a reading that maps to a recognized **task type** (e.g.
  `tax_filing`) opens a step-by-step guided *playbook* in a dim-everything overlay
  (the same overlay pattern panic mode uses) — the "pop-up that guides me through
  the task." Dismissing marks an item not-ambiguous so the sweep never re-asks.
  the detection sweep (`sweep_ambiguous_items`) runs **on the coaching tick**
  (`POST /webhooks/coach/check` and `prefrontal coach --deliver`, beside
  `sweep_avoided_decompositions`), so the queue fills passively — bounded model
  calls per tick, and it never re-asks an item it has history for.
  `POST /clarifications/check` is the on-demand "check now" twin;
  `GET /clarifications`, `POST /clarifications/{id}/resolve|dismiss`,
  `GET /clarifications/playbooks/{task_type}` round out the HTTP surface, and a
  `prefrontal clarify check|list|resolve|dismiss|guide` CLI mirrors it for
  headless use (the resolve logic is shared via `apply_clarification_answer`, so
  HTTP and CLI can't drift). Declared as the Task Paralysis `clarify_ambiguous`
  intervention and surfaced in its profile section. Covered by
  `tests/test_clarify.py` + `tests/test_clarify_endpoints.py` + `tests/test_cli.py`.
  The registry has since grown to eight task types (tax filing, passport, DMV
  license, vehicle registration, insurance claim, home repair, finding a
  provider, appointments), and guides **localize to the user's home ZIP** when
  opted in: a step's `{area}` token renders as the `home_zip` (seeded to the
  deployment default, back-filled to existing users by the migration ladder)
  once `playbook_localization` is on — off by default, toggled via
  `prefrontal clarify localize on`, degrading to a generic phrase otherwise.
  Free-text answers map to a task type by the **most specific** keyword match, so
  a generic word can't hijack a specific reading.
- **Departure reminders on the coaching tick (toward retiring n8n)** ✅ — the
  `departure_buffer` intervention is now a coach cue: `TimeBlindnessModule.evaluate`
  emits the most-urgent due departure (reusing the same `plan_upcoming_departures`
  / `next_departure` / `build_departure_message` the `/webhooks/departure/check`
  endpoint and the widget's leave-by use), so a single native `prefrontal coach
  --deliver` tick sends "leave by" nudges without n8n polling that endpoint.
  Fire-once and escalation are the engine's job — the `dedup_key`
  (`departure:<id>:<level>`) makes each heads_up→soon→go transition a fresh fire,
  and `go` maps to `critical` so the final "head out now" bypasses quiet hours
  like the endpoint does. The `context_key="departure"` cue carries the signed
  **Made it / Missed it** buttons through the native delivery client. The endpoint
  stays for now (shares nothing that double-fires — the engine owns the coach-side
  dedup), to be deprecated once the tick has run clean. Covered by
  `tests/test_modules.py`. *(That fold toward n8n-free delivery has since landed:
  panic rides the same coach tick, the household sweeps have their own native
  launchd job (`com.prefrontal-household.plist`), and **a launchd `coach --deliver`
  schedule now drives the tick** (`deploy/com.prefrontal-coach.plist` +
  `deploy/coach.sh`, every 60s), replacing the coach-check / hyperfocus-check /
  departure-reminder / panic-check poll workflows in one job. With the native
  Twilio voice call below, the outing 150% escalation is native too — so the nudge
  workflows can all be deactivated; see "Delivery layer".)*
- **Parent pack / shared household sheet** ✅ — the co-parent surface shipped end
  to end (`prefrontal/household.py`, `webhooks/routers/household.py`,
  `memory/repos/household.py`; the `/kids` dashboard + `/family` glance;
  `prefrontal household add|join|leave|show|invite|redeem|star|prompt-check|
  checkin-check|digest-check|balance|shopping|chore|routine|chores-check`). It
  carries: a
  real **household scope** (`households` + `users.household_id`), **facts** &
  **agreements**, **star charts** with goals + dual-parent congratulation and
  scheduled award prompts, a shared **shopping list**, **recurring shared chores**
  (owner reminder + miss-handoff to the other parent), the objective
  **load-balance view**, the daily **delta digest** (push what your co-parent
  changed), an optional weekly **mental-load check-in**, **single-parent** support
  (load-balancing gated), and **self-serve invites** (create/redeem/revoke). n8n
  workflows drive the sweeps (`star-prompt-check`, `checkin-check`, `digest-check`,
  `chores-check`). Full design:
  [`docs/household-sheet.md`](docs/household-sheet.md).
- **Household routines (RACI) + accountability in the balance view** ✅ — chores
  now group under a **routine** (`household_routines`) with exactly one
  **accountable** owner (RACI "A" — the mental-load holder, distinct from the
  chore's "responsible" doer). A routine carries the schedule its chores
  **inherit** (a chore overrides only if it sets its own time; a chore can be
  *untimed* — a checklist item with no clock, `due_time=''`). Crucially, load
  balancing gained a **second facet**: `contribution_counts` now counts chores
  actually **done** (`household_chore_log.done_by`) — not just sheet edits — as the
  *doing* tally, and `accountability_counts` reports the *carrying* tally (enabled
  routines each parent holds). `balance_view(counts, carrying=…)` surfaces both,
  each with its own gentle caption. Endpoints `POST /household/routines`
  (+`/enabled`, `/remove`), `chore.routine_id`, and `prefrontal household routine`.
  Covered by `tests/test_chores.py` + `tests/test_household.py`. The **`/kids`
  dashboard** now surfaces it: a **Shared chores** card (one-tap "done today" that
  logs who did it, add form with owner/routine/time, routine badge + inherited
  schedule), a **Routines** card (accountable owner + add form), and the
  **carrying** facet in the "Sharing the load" panel alongside doing. The sheet
  payload gained a `members` list to populate the owner/accountable pickers.
- **LLM-as-sensor — free text → candidate updates** ✅ — `prefrontal/sensor.py`
  reads a plain note ("I always blow off admin on Mondays") and *proposes*
  allowlisted `coaching_state`/episode candidates that land as **pending**
  `proposals` rows; only a human accept applies them (`source=llm_inferred`).
  `prefrontal note` / `prefrontal proposals list|accept|reject`.
- **Work/life guardrails — per-account domains + time bands** ✅ — a todo now
  carries a life **domain** (`work`/`home`/…) that resolves to a time band and
  **outranks its category**, so a work email that triages as "communication" is
  still held to work hours and never squeezed into a home evening. `resolve_window`
  precedence is now *per-todo override → domain → category → source → default*
  (`prefrontal/scheduling.py`); the band is a **hard** gate (`todo_allowed_at`),
  matching the existing off-zone. Mail ingestion stamps the domain from the
  account: `PREFRONTAL_ACCOUNT_DOMAINS` (e.g. `work=work,personal=home`) maps each
  inbox to a domain, threaded through `ingest_messages`. A new nullable
  `todos.domain` column (created by the idempotent schema + migrate back-fill) with
  `add_todo(domain=…)` / `set_todo_domain`. Off by default (no accounts mapped ⇒
  unchanged scheduling). Covered by `tests/test_todos.py` + `tests/test_mail.py`.
  **Crunch mode** ✅ ships the escape hatch: a self-expiring `crunch_until`
  coaching-state timestamp (`prefrontal crunch on --hours N` / `off` / `status`)
  suspends the per-key bands so anything can surface any waking hour during a
  deadline stretch — the off-zone and travel-late gate still apply. Editing a
  todo's domain is now a first-class surface: `POST /todos/{id}/domain` (declared
  before the `{action}` catch-all) and `prefrontal todo domain <id> [value]`
  (omit the value to clear), both normalizing to lowercase and reusing
  `set_todo_domain`. The **dashboard** exposes it as an outlined pill on each todo
  (distinct from the solid category chip) with a picker — the in-use domains plus
  the canonical work/home, "New…", and "Clear" — and the domain is searchable; the
  **widget** gets it in the `/todos/now` suggestion payload so it can label the pick.
- **Self-care checks — "have you eaten?" + water** ✅ — the cues that deliberately
  pierce flow (a focus state is exactly when you forget to eat or drink), shipped
  as a sixth module, `prefrontal/modules/self_care.py`. A small registry of
  **basic-needs checks** rides the coaching tick (`evaluate()`), unified by a
  **daily target**: a **meal** is target 1 (from `meal_start_hour`≈11, re-ask
  every `meal_reask_minutes`≈40 until one "Ate" ends it for the day); **water** is
  target `water_daily_target`≈6 (from `water_start_hour`≈9, a "drink some water"
  reminder every `water_interval_minutes`≈90 where each **Drank** counts one and
  defers a full interval, done once the target's met). Cadence rides a per-interval
  *bucket* in the `dedup_key` so the engine fires each window once; responsive-hours
  + debounce come from the engine (no overnight nag). One-tap **Ate/Snooze** and
  **Drank/Snooze** ride the signed-action path (`meal_*`/`water_*` in
  `NUDGE_ACTIONS`, `meal`/`water` buttons in `notify.py`, `apply_self_care_action`
  in `/nudge/act`); progress is plain coaching-state cursors (`*_count` = `date|n`
  toward the target, `*_snoozed_until`), no schema change. Every confirm/snooze
  logs a `self_care` episode (seed for the cadence learner below).
  `/webhooks/coach/check` passes each cue's `actions` through and
  `deploy/n8n/coach-check.workflow.json` publishes them to ntfy, so the buttons
  render. **Off by default** — set the `self_care` coaching key to `on` (each check
  also has its own `meal_enabled`/`water_enabled`). Covered by
  `tests/test_self_care.py`. The **adaptive-cadence learner** ships (see "Learning
  & adaptation" §6): it now folds **unanswered** nudges in too —
  `sweep_unanswered_self_care` logs a nudge left un-acted past a window as an
  `ignored` episode, which the learner reads as a "wrong time / too frequent"
  signal alongside snoozes. A **meds** check now ships as a third basic (one-tap
  Took / Snooze, its own start hour / interval / daily target for multi-dose
  regimens) — **off even when self_care is on**, since medication is personal, so
  it's opt-in per person via `meds_enabled`. A **bio-break** check followed
  (open-ended: a plain interval reminder within a time *window*, bounded by an
  `biobreak_end_hour` rather than a daily quota — no count ever silences it, `Went`
  just defers the next). The **wind-down / sleep** check now rounds out the basics
  pack ✅ — a once-a-day (target 1) *evening* nudge to "start winding down for bed"
  from `winddown_start_hour`≈21, re-asking every `winddown_reask_minutes`≈30 until
  one **Winding down** settles it for the night (`winddown_started`/`winddown_snooze`
  in `NUDGE_ACTIONS`, a 🌙 button in `notify.py`, `winddown` in the delivery
  `_CONTEXT_KIND`/coach `_CUE_ACTION_KIND` maps). Unlike the daytime checks it lives
  right against the responsive-hours edge, and by design **leans on the engine's
  quiet-hours gate** — a wind-down cue outside responsive hours is `suppressed` like
  any other — rather than modelling a bedtime itself, so it never nags into the
  night. **Off even when self_care is on** (a bedtime is a personal preference, like
  meds): opt in via `winddown_enabled`. Covered by `tests/test_self_care.py`.
  *(The "does it broaden into a self-care/basics pack?" question is now moot in
  practice — meal + water + meds + bio-break + wind-down cover the basics; a formal
  `Pack` bundling them with seeded defaults is possible but not needed to use them.)*
- **Encouragement & recovery layer** ✅ — the counterweight to a system that
  nudges: when a day goes rough, shift tone from nudging to reassurance + a plan.
  `prefrontal/encouragement.py`'s deterministic `assess_day()` scores today's
  signals (a missed *hard* commitment is the heaviest at 3.0; an *overwhelmed*
  plate right now — reusing panic's `overwhelm_level()` — also 3.0; each `miss`
  episode 1.0; double-bookings 0.5; a small rising-drift modifier) and flags
  `rough` past a threshold; `build_recovery()` composes existing logic into a
  plan — re-fit the rest of the day (`suggest_for_windows`), suggest deferring
  only *soft* commitments, and one tiny first step over the most-avoided todo
  (`decompose_task`); `render_encouragement()` writes it warm or plain, and
  `summarize_encouragement()` adds the optional Ollama prose pass with heuristic
  fallback. Surfaced at `GET /encouragement` (model-free; `already_sent` reflects
  a once-per-day cursor advanced by `POST /encouragement/sent`), `prefrontal
  encourage`, and `deploy/n8n/encouragement.workflow.json`. **Off by default**
  (the `encouragement` coaching key); auditable `signals`; covered by
  `tests/test_encouragement.py`. Standalone per `docs/encouragement.md`; the
  coaching agent can later wrap `assess_day` as one more cue producer.
- **Coaching agent — engine core + first evaluator** ✅ — the decision engine
  that turns *what Prefrontal knows* into the right nudge (see the full spec,
  `docs/coaching-agent.md`). `prefrontal/coaching.py` holds the pure core:
  `Cue`/`CoachContext`/`Decision`, `collect_cues` (one bad module can't sink the
  tick), `choose_channel` (urgency floor → learned `channel_response` bump),
  `suppressed` (quiet hours + per-`dedup_key` debounce, no schema change — a
  `coach_fired:*` coaching-state stamp), and `decide`. `Module.evaluate(store,
  ctx)` is the new opt-in hook (default `[]`), with two v1
  producers: **Task Paralysis** fires a `tiny_first_step` nudge over the
  worst-avoided open todo (reusing `avoided_todos` + the stored decomposition),
  and **Location Anchor** now emits the outing escalation cues (soft→nudge /
  firm→urgent / call→critical) through the same engine — its per-outing decision
  and side effects were lifted into a shared `evaluate_outing` /
  `apply_outing_evaluation` that `/webhooks/outing/check` and the evaluator both
  call, so the legacy endpoint is byte-identical (the full suite is the parity
  net) and there's one source of truth. Run one tick with `prefrontal coach`
  (`--dry-run` to see cues pre-suppression), or poll **`POST /webhooks/coach/check`**
  — the tick endpoint that fans over every enabled module and returns each
  fire-worthy cue with its chosen channel, deduped so a standing cue won't repeat
  (`deploy/n8n/coach-check.workflow.json` delivers them via ntfy at a
  channel-matched priority). Covered by `tests/test_coaching.py` +
  `tests/test_location_anchor.py`. This is steps 1 + 2 + 3 + both evaluators of
  the spec's rollout; LLM phrasing and outcome-logging correlation remain (see
  "Coaching agent" below).
- **Interactive nudge action buttons (ntfy)** ✅ — one-tap, background nudge
  responses with no app switch. `sign_action`/`verify_action`
  (`prefrontal/webhooks/oauth.py`) extend the signed one-tap-link mechanism from
  *dismiss* to an allowlisted action set, and `GET /nudge/act` runs the tapped
  action through the same close/record logic as its full endpoint: **Wrap up**
  (focus end), **I'm back** / **Abandon** (outing close), **Made it** / **Missed
  it** (departure outcome), and **Stay on task** / **Park it** / **Switch
  anyway** on the impulsivity reflective pause — collapsing its old two-call
  `switch` → `resolve` menu into a single background tap.
  `prefrontal/webhooks/notify.py` builds the ntfy `http` action-button specs
  (empty unless a public origin + signing key are set), and the native delivery
  client attaches them to each cue on the `prefrontal coach --deliver` tick — so a
  tap fires `GET /nudge/act` in the background with no app switch. Idempotent per
  button; covered by `tests/test_notify.py` + `tests/test_nudge_act.py`. The old
  "publish to ntfy" n8n node (`deploy/n8n/interactive-nudge-ntfy.workflow.json`)
  was retired once first-class Python publishing landed.
- **Multi-tenant (multiple users)** ✅ — one deployment now serves several people.
  A `users` table with per-user tokens (`sha256(token)`, shown once like an API
  key) and a `user_id` foreign key on every user-owned table; a `MemoryStore`
  bound to one user via `.scoped(user_id)` so no read/write can forget the scope;
  request resolution (`resolve_user` → `ScopedRequest`) and operator-only
  provisioning (`provision_user`, `POST /admin/users`). `coaching_state`/patterns
  are per-user, so each person learns independently. See `docs/multi-tenant.md`.
  *(This was the big "Beyond v1" item; it's now the default architecture.)*
- **Panic mode** ✅ — an overwhelm circuit-breaker for when you're too buried to
  think. `prefrontal/panic.py`'s deterministic `build_panic()` gathers everything
  actually bearing down — across calendar, todos, and mail — and ranks it into
  three buckets: **already behind** (commitments past their safe-departure or a
  hard meeting underway, overdue todos), **bearing down soon** (departures within
  ~2h, todos due today, urgent mail), and **piling up** (no hard clock — avoided
  todos, high-priority mail). Each item is tagged with where it came from
  (calendar label / inbox account) so work and home pressures sit side by side,
  and it picks the single most pressing *clocked* item for **one concrete first
  step**, reusing the Task Paralysis decomposition lever. (`fyi` commitments and
  already-ended events are excluded; date-only todo deadlines count as end-of-day
  so "due today" reads as *soon*, not *overdue*.) `render_panic()` emits steadying
  Markdown with an all-clear path; `summarize_panic()` adds an optional Ollama
  prose pass with heuristic fallback — the same two-layer shape as the briefing.
  Reachable four ways:
  - **On-demand:** `prefrontal panic` (`--llm` for prose) and `GET /panic`
    (structured buckets + rendered text + a one-line `headline`).
  - **Dashboard / family view:** a "😮‍💨 Panic" / "Feeling overwhelmed?" button
    opens a focused, dim-everything overlay with the first step front and center.
  - **One tap:** a "Panic" iOS Shortcut (`deploy/ios-shortcut.md`) that `GET`s
    `/panic` and reads back the `headline`.
  - **Proactive:** `POST /webhooks/panic/check` (+ `deploy/n8n/panic-check.workflow.json`)
    nudges **only when the plate tips into overwhelm** (`overwhelm_level()`:
    two-plus already late, or one late with a full plate). It edge-triggers on the
    level — like the departure signature — so a sustained pile-up nudges once, not
    every poll, with a cooldown floor. Tunable via the `panic_alert_min_pressing`
    and `panic_alert_cooldown_minutes` coaching-state keys.

  Endpoints live in `prefrontal/webhooks/routers/schedule.py`; covered by
  `tests/test_panic.py`. Delivers the get-back-on-track slice of the
  "encouragement & recovery layer" below, leaving the softer, tone-calibrated
  daily-recovery variant as the remaining piece. The three follow-ups have all
  shipped: **quiet-hours gating** ✅ — `evaluate_panic_check(quiet_hours=…)` now
  **defers** (never drops) an overwhelm edge that lands outside responsive hours,
  leaving `last_panic_level` untouched so the first poll back in responsive hours
  still fires; both `/webhooks/panic/check` and the native `coach --deliver` tick
  pass `in_quiet_hours(...)`, so an overwhelm push can no longer land at 3am.
  **First-step outcome capture** ✅ — a fired nudge logs a *pending* `panic`
  episode (`record_panic_step_sent`) that a one-tap "✓ Did it" resolves to
  success (`resolve_panic_step`) and an unanswered one is swept to a miss
  (`sweep_pending_panic_steps`), so the drift pass learns whether the surfaced
  step actually got done. **Encouragement reuse** ✅ — `overwhelm_level()` is now
  one of the triggers for the encouragement layer's `assess_day` detector
  (weighted like a missed-hard commitment) instead of a separate threshold, so a
  plate that's buried *right now* reads rough even before anything is missed.
  Covered by `tests/test_panic.py` + `tests/test_encouragement.py`.
- **"What fits right now" (widget)** ✅ — `GET /todos/now` computes the free gap
  until your next commitment (bounded by working hours + a cap) and returns the
  single best-fitting open todo — **biased toward the most-avoided** task and
  **preferring low-energy tasks later in the day** (honest prioritization, not the
  shiny thing). The Scriptable widget shows it on the home screen and all three
  Lock Screen accessories ("25m free · <todo>", or "catch up" for an avoided one).
- **Todo outcome capture** ✅ — closing a todo now logs a `task` episode (done ⇒
  `success`, drop ⇒ `miss`) via `record_todo_closed()` in `prefrontal/todos.py`,
  wired into `POST /todos/{id}/{action}` and `prefrontal todo done/drop`. This
  was the largest uninstrumented user-touch surface: the learning pass already
  saw outings, focus sessions, and mail, but a finished-or-abandoned todo — the
  moment an avoided task finally resolves — was thrown away. It feeds the `task`
  `drift` score; `actual_value` stays `None` (a todo's created→closed span is
  wall-clock, not time-on-task, so it never pollutes `time_estimation`), with the
  age kept in the episode `notes`. *(Departure outcomes are now captured too —
  see the next entry.)*
- **Automatic departure-outcome capture** ✅ — the mirror of the departure
  *reminder*: did you actually leave on time? A "leave Home" iOS geofence hits
  `POST /webhooks/departure/left`, which attributes the departure to the
  commitment you were heading to (`attribute_departure` — the soonest one whose
  leave window you're in), scores it on-time vs late against that commitment's
  computed leave-by (`classify_departure`, with a `departure_grace_minutes`
  tolerance), and logs a `departure` episode (`record_departure_outcome`). Like
  the abandoned-outing and closed-todo captures, `actual_value` is left `None` so
  it feeds the `departure` `drift` score without polluting the shared
  `time_estimation_bias` (leaving late must not *lower* the underestimate
  multiplier). Idempotent per commitment occurrence, and it silences any pending
  departure nudge for that commitment. All in `prefrontal/departure.py` +
  `routers/schedule.py`; covered by `tests/test_departure.py`. This was the last
  big uninstrumented user-touch surface flagged under "Learning & adaptation."
- **Attend-mode departures (work meetings you don't travel to)** ✅ — most work
  commitments are attended from wherever you already are (desk / WFH), so a
  travel-aware "leave now — 25 min travel" nudge for a meeting you'll take on your
  laptop was pure noise. `departure_mode()` now classifies each commitment: those
  on an attend-mode feed (`calendar_key` in `attend_calendars`, default `work`)
  skip travel logic entirely and fire a single short "starts in ~5 min — you're
  already where you need to be" reminder `work_departure_lead_minutes` before start
  (`basis="attend"`, no leave/travel copy, no heads_up→soon→go ladder). Two escape
  hatches restore the travel path for the rare in-person case: a `[commute]`/
  `[onsite]` tag in a single meeting's title/location, or flagging the whole day
  in-office via `POST /webhooks/departure/office-day` (a self-expiring toggle, tap
  it the mornings you commute). Both the check and the outcome (`/left`) surfaces
  plan the same way. In `prefrontal/departure.py` + `routers/schedule.py`; covered
  by `tests/test_departure.py`.
- **Task Paralysis module fully wired** ✅ — the last fully-stubbed module is now
  live, all interventions `active`. **auto_decompose** (opt-in, **off by default**
  via `auto_decompose_enabled`) breaks a todo you're *avoiding* into a tiny first
  step on the coaching tick (`sweep_avoided_decompositions`), not at creation, and
  only when the model judges it worth it; **tiny_first_step** reframes a stalled
  task on demand (`POST /todos/{id}/decompose`, also fed to panic and `/todos/now`); and
  **body_double_nudge** is new — `repeat_stalled_tasks()`
  (`prefrontal/modules/task_paralysis.py`) finds tasks you keep bailing on
  (≥ `body_double_min_misses` `miss` episodes on the same title, not since
  resolved) and `GET /todos/stuck` surfaces each with a tiny first step and a
  start-together suggestion; the profile section names them so the coaching prose
  stops sending reminders and offers a body-double instead. Covered by
  `tests/test_modules.py` + `tests/test_webhooks.py`.
- **Avoidance detection** ✅ — `avoided_todos()` (`prefrontal/todos.py`) scores
  open loops by how long they've been skipped (age × priority), surfacing the
  important thing you keep putting off rather than letting it sink down the list.
  Exposed at `GET /todos/avoided` and woven into the morning briefing's "you keep
  putting off" line. *(Next input for the coaching agent's `tiny_first_step`
  picker — see `docs/coaching-agent.md`.)*
- **In-progress pinning + focus-conflict alert** ✅ — a *started* todo
  (`POST /todos/{id}/start`, cleared by `/unstart`) is pinned to the top of
  `GET /todos` (`sort_todos_for_display`), so the task you're mid-flight on stays
  visible. `focus_conflict()` (`prefrontal/todos.py`) flags when everything you've
  started ranks below an important task you're avoiding-and-haven't-started —
  returned as the `focus_conflict` field on `GET /todos` and rendered as a gentle
  "worth switching?" banner on the dashboard. The honest-prioritization companion
  to avoidance detection.
- **Editable todo deadlines + per-step check-offs** ✅ — `POST /todos/{id}/deadline`
  moves or clears a deadline on an open todo, and
  `POST /todos/{id}/steps/{step_index}/done` ticks off an individual decomposition
  step (tracked in `todo_decompositions.done_steps`, index 0 = the first step) so
  visible progress keeps a decomposed task moving. Both surface in `GET /todos`
  and the dashboard.
- **Mail ingestion + triage** ✅ — `prefrontal/mail/` normalizes a batch of
  messages, triages each (Ollama with a deterministic heuristic fallback) into
  `needs_action`/`urgency`/`category`/one-line `summary`, dedupes on the
  account-scoped `message_id`, and **surfaces actionable mail as `todos`** so it
  flows into the existing open-loop machinery (fit, briefing). Per-account
  retention (`full` vs `signals`) keeps bodies local or never-stored. Surfaced
  via `prefrontal mail` (list/sync/fetch), `POST /webhooks/mail/sync`, and
  `GET /mail`. *(This is the first concrete slice of the broader Triage agent —
  see `docs/triage-agent.md` for the source-agnostic generalization.)*
- **Hyperfocus focus sessions** ✅ — `prefrontal/modules/hyperfocus.py` +
  `focus_sessions` table: a declared deep-work block with an optional plan and an
  `aligned` "is this what I meant to do?" bit. Asymmetric by design — it
  *protects* an aligned block from other nudges while healthy, and only
  interrupts to gently check alignment once it overruns or to force a break past
  the hard ceiling. Wired end-to-end via `POST /webhooks/focus/{start,check,end}`
  and `GET /focus`; all four interventions are `active`.
- **Todo decomposition (tiny first step)** ✅ — `prefrontal/todos.py` +
  `todo_decompositions` table: a todo big enough to stall on
  (≥ `decomposition_threshold`) is broken into a tiny first step
  (≤ `max_first_step_minutes`) plus collapsed remaining steps — the task
  initiation lever for the Task Paralysis module (Ollama + heuristic fallback).
- **Scriptable home-screen & Lock Screen widget** ✅ — `deploy/scriptable/` polls
  `/outings`, `/commitments`, conflicts, todos, and `/todos/now` over Tailscale
  and renders a glanceable "right now": the active outing + escalation level, next
  commitments, conflict/todo counts, and the one todo that fits your current free
  window; taps open the `/family` view. One script drives every
  family: the full Home Screen card (Small/Medium/Large) **and** the iOS 16+ Lock
  Screen accessory slots (circular / rectangular / inline, monochrome via SF
  Symbols). *(Realizes the "iOS lock-screen widget" idea from the architecture.)*
- **Commitment geocoding (places → cache → Nominatim)** ✅ —
  `prefrontal/geocode.py` resolves a commitment's free-text `location` to
  `dest_lat`/`dest_lon` so the departure reminder's travel estimate actually
  fires. Layered + local-first: a user-curated `places` alias table
  (`POST /places`, instant/offline), then a `geocode_cache` (incl. recorded
  misses), then an **opt-in** Nominatim geocoder
  (`prefrontal/integrations/nominatim.py`, gated by the `geocoding_enabled`
  state flag — off by default). Enrichment runs best-effort on calendar sync and
  manual add, with `POST /commitments/geocode` to backfill. Failures degrade to
  the `lead_minutes` fallback. A **`prefrontal place`** CLI (`add` / `list`)
  curates aliases offline from the terminal, the twin of `POST` / `GET /places`.
  *(Next: reverse-geocode the iOS location ping for nicer context; self-host
  Nominatim on the mini.)*
- **Last-known location + travel-aware departure reminders** ✅ —
  `POST /webhooks/location` stores the phone's position (one iOS "Update
  location" automation), so the coffee-shop nudge gates on location **without
  Home Assistant** (the check falls back to the stored fix). `prefrontal/
  departure.py` + `POST /webhooks/departure/check` then compute *when to leave*
  for the next commitment: a local, bias-adjusted travel estimate
  (straight-line distance × road-factor ÷ speed, no maps API) from the stored
  location to the commitment's optional `dest_lat`/`dest_lon`, escalating
  heads-up → soon → go and deduped per `(commitment, level)`. Falls back to the
  static `lead_minutes` when coordinates or a recent fix are missing. The
  rewritten `deploy/n8n/departure-reminder.workflow.json` polls it and pushes via
  Pushover. The **widget now surfaces the leave-by time** for the next
  commitment: a read-only `GET /departure/next` (the side-effect-free companion to
  `POST /webhooks/departure/check` — no dedup, no nudge, safe to poll) plans the
  soonest upcoming commitment and returns its `leave_by`, which the Scriptable
  widget shows as a "leave 4:15 PM · 12m" line under the next commitment (colored
  by departure level, gated to a *today* travel commitment so a leave-by days out
  or an attend-from-desk meeting stays quiet). Both surfaces share a new read-only
  `plan_upcoming_departures` helper. The **morning briefing now surfaces it too** —
  a "🚶 Leave by:" section listing today's remaining travel commitments with their
  leave-by (bias-adjusted travel estimate, or the static lead), planned with the
  same `plan_departure`/`departure_kwargs` the nudge uses so the digest matches
  what it's nudged for; attend-mode, zero-lead, **FYI** (someone else's event),
  and placeholder-hold items are omitted — the same real, own-commitment subset
  the nudge and cascade use via `is_attendable`, so an FYI event never gets a
  "leave by" (or a tight-stretch flag) — gated on the Time Blindness module.
  Covered by `tests/test_departure.py` + `tests/test_briefing.py`. *(Next:
  optional geocoding of free-text `location`; per-commitment travel learning.)*
- **Briefing layout + feedback loop** ✅ — the deterministic digest now leads with
  today and what to act on (schedule → leave-by → risks → opportunities) and keeps
  the gentler look-back (what slipped, focus, balance) down by the closing note,
  with consistent emoji headers and warmer phrasing. Each delivered digest ends
  with a small 👍/👎 "Did this help?" footer (signed one-tap `/nudge/act` links,
  `briefing_helped`/`briefing_not_helped`, riding a synthetic `0` target like the
  self-care checks). The running tally feeds `learned_briefing_guidance` back into
  the LLM briefing prompt — a run of 👎 tightens the voice, a run of 👍 holds its
  shape. Covered by `tests/test_briefing.py` + `tests/test_nudge_act.py`.
- **Pattern-computation pass** ✅ — `prefrontal/memory/patterns.py` derives
  `time_estimation`, `channel_response`, and `drift` patterns from `episodes`
  (confidence = `n/(n+k)`) and recomputes the `time_estimation_bias` multiplier.
  Run via `prefrontal learn` (scheduled nightly — see step 7 above). Both of the
  deferred pieces have now shipped: `context_switch` derivation (from the captured
  per-session `switch` episodes — see §5), and **finer `context_key` bucketing than
  episode type** ✅ — a new **activity** dimension (`compute_bias_by_activity`,
  `time_estimation_bias:activity:<activity>`) buckets `task` estimation pairs by the
  activity read off the episode `context` (`outing` / `focus` / `trip` / …), one
  step below `episode_type`. Every out-of-flow surface logs a `task` episode, so the
  `type:task` multiplier pooled a coffee run ("back in 15" that stretches to 45) with
  a well-estimated focus block; the activity bucket separates them. `resolve_bias`
  gained an `activity` layer (band → energy → category → **activity** → type →
  global), and the outing nudge projection (`location_anchor.evaluate` +
  `/webhooks/outing/check`) now resolves `activity="outing"` so a coffee run
  calibrates against *outing* history, falling back to the global until there's
  enough outing signal. Surfaced in the profile ("By activity: …") and
  `prefrontal learn`; auto half-life derivation and per-context half-life overrides
  cover it like the other dimensions. Covered by `tests/test_patterns.py`.
- **LLM-backed summarizer** ✅ — `summarize_profile()` feeds the structured
  profile to a local Ollama model (`prefrontal/integrations/ollama.py`) and
  returns prioritized coaching prose, falling back to the heuristic when the
  model is down. Run via `prefrontal summarize`, which now **caches** the
  narrative in the `profile_cache` table so `GET /profile` serves the prose
  without a per-request model round-trip (`?refresh=1` regenerates it,
  `?format=structured` returns the raw input; `X-Profile-*` headers report
  source/model/age/staleness). The summarizer is now one of the agents that can
  opt into the **Anthropic provider** (`ANTHROPIC_AGENTS=summarizer`) for
  higher-quality prose, with Ollama as the fallback — see below.
- **Calendar ingestion + double-booking** ✅ — `commitments` table +
  `prefrontal/commitments.py`: feed-aware calendar sync, manual add,
  `GET /commitments`, and overlap detection at `GET /commitments/conflicts`.
  Per-user **private ICS feeds** now sync natively (`prefrontal/ics.py`,
  `prefrontal calendar add-source|sync --all-users`, launchd `com.prefrontal-calendar`
  every 15 min) — the no-n8n path that *replaces* the old n8n `calendar-sync`
  workflow (deactivate it to avoid double-ingestion). The `/webhooks/calendar/sync`
  endpoint remains for batch/n8n callers.
- **Impact analysis + cascade** ✅ — `prefrontal/impact.py`: projects realistic
  free-time from the `time_estimation_bias` and flags upcoming commitments now at
  risk (`start_at − lead_minutes` vs projection). `cascade_impact()` then
  propagates the overrun *through* the chain — a late finish carries forward
  through each commitment's own length, so a meeting two hops down is flagged when
  the delay reaches it, with `delay_minutes`/`projected_start`/`caused_by` naming
  the upstream domino; the chain self-heals when a gap absorbs the slip. Surfaced
  in `/webhooks/outing/check` (an `impact` list + `hard_conflict` flag, message
  tail "This cascades: 'A' → 'B' → 'C'") and, beyond outings, at
  `GET /impact/cascade` (queryable from any free-time via `free_at`/`over_minutes`,
  else the active outing, else now). The **dashboard** renders the domino strip
  live and a "running behind" scrubber (`over_minutes`) to pre-project it, and the
  **morning briefing** runs `fragile_stretch()` — today's remaining commitments
  cascaded under the learned bias-inflated durations — to preview the tightest
  back-to-back stretch ("⏳ Tight stretch: if today runs long, A → B") before it
  slips, staying silent on a day with slack or when the bias shows no overrun.
  **Panic mode** (`GET /panic`) adds a "⚠️ Knock-on" line — the cascade seeded at
  now over upcoming commitments — so when you're already late it names the
  downstream chain that topples too, not just the first fire (shown only when two
  or more downstream commitments are at risk). The **iOS widget**
  (`deploy/scriptable/prefrontal-widget.js`) mirrors it: a "running behind" Lock
  Screen facet (`behind` param) and a home-screen line, gated the same way.
  **Travel-aware leads**: when a location is known, `GET /impact/cascade` replaces
  each leg's static `lead_minutes` with real bias-adjusted travel between commitment
  coordinates (`departure.travel_leads` → `cascade_impact(lead_override=…)`), so a
  leg you can't actually drive in the flat buffer is flagged; `travel_aware` reports
  when it applied. *(Next: thread the same override into the outing/panic surfaces.)*
- **Morning briefing** ✅ — `prefrontal/briefing.py`: a daily digest of today's
  commitments, double-bookings, what slipped this past week, and a coaching note
  (the time bias), honoring `preferred_briefing_format`. `GET /briefing` +
  `prefrontal briefing` (`--llm` for Ollama prose, heuristic fallback); delivered
  by `deploy/n8n/morning-briefing.workflow.json`.
- **Todos + time-fitting** ✅ — `todos` table + `prefrontal/scheduling.py`: open
  loops (call the dentist, plan a birthday) with estimate/priority/deadline, plus
  `free_windows()` over the schedule and `fit_todos()` that ranks what fits a gap
  (bias-adjusted). `GET/POST /todos`, `GET /todos/fit?minutes=N`,
  `prefrontal todo`/`fit`, and a "spare time" section in the morning briefing.
  Energy-aware fitting shipped in the `/todos/now` picker (above).
  **Auto-scheduling** shipped: `POST /todos/{id}/schedule` blocks time for a todo
  as a `manual` commitment — the block length is the bias-adjusted estimate (or an
  explicit `minutes`), placed at an explicit `at` or the earliest fitting free
  window today (`first_window_fitting` over `free_windows`, within waking hours),
  turning a "good for: X" suggestion into a real hold. **Multi-suggestion per
  window** shipped too: `suggest_for_windows(options_per_window=N)` returns a menu
  per gap (the reserved primary + advisory alternatives), and the briefing's spare
  section shows them ("good for: X _(or: Y, Z)_").

## ✅ Deployed and running (Module 1 live on the mini)

Prefrontal is **live on the Mac mini and in daily use** — real outings, calendar
sync, mail triage, the widget, and the nightly learn pass all run against a
multi-tenant deployment. The end-to-end Coffee Shop Nudge (outing endpoints, time
escalation, location-gating, abandoned auto-close, passive return, and the
learning + summarizer passes) is done and exercised in the wild. The original
bring-up runbook is kept below for reference / a fresh deploy (see
`docs/deployment.md`):

1. **Stand up Prefrontal** — clone, `pip install -e .`, set a strong
   `PREFRONTAL_WEBHOOK_SECRET` in `.env`, `prefrontal init-db`, load the launchd
   agent (`deploy/com.prefrontal.plist`). Confirm `GET /health`.
2. **Ollama** — `ollama pull qwen2.5:14b` (24GB mini), set `OLLAMA_MODEL`.
3. **n8n** — import `deploy/n8n/coffee-shop-nudge.workflow.json`; set the
   Prefrontal token, the Twilio Basic-Auth credential + `To`/`From`, and Pushover
   token/user.
4. **iOS Shortcuts** — build "Going out" / "I'm back" (`deploy/ios-shortcut.md`).
   *Optional but recommended:* feed `current_lat`/`current_lon` into the n8n
   `Check Outings` body (from an HA/iOS location source) to activate
   location-gating + passive return.
5. **Tailscale** — so the phone reaches the mini remotely.
6. **Dry run** — start an outing with a 1-minute window and confirm: push at
   ~30s (50%), push at ~1m (100%), Twilio call at ~90s (150%), and that
   `/return` (or coming home) logs the episode.
7. **Schedule learning** ✅ — nightly `prefrontal learn && prefrontal summarize`
   via `deploy/learn.sh` + `deploy/com.prefrontal-learn.plist`
   (launchd `StartCalendarInterval`, 03:30); see deployment §12. Load it and the
   profile recalibrates on its own.

Everything above the dry run is configuration; no further code is required for
the first test. Code follow-ups below are optional polish.

