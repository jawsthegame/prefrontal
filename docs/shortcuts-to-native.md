# Migrating from iOS Shortcuts to native iOS services

*Status: design sketch. Nothing here is committed to a release; it's a map of
where the native app already replaces Shortcuts, what's left, and the handful of
cases that can't cleanly go native.*

## TL;DR

Prefrontal started as a **Shortcuts-first** client: every one-tap action was a
hand-built "Get Contents of URL" shortcut POSTing to the FastAPI server over
Tailscale (`deploy/ios-shortcut.md`). The SwiftUI app (`ios/`) has since
re-implemented **most** of those touchpoints as native services — App Intents,
APNs, geofencing, Live Activities, interactive widgets, Control Center controls,
and a durable offline queue. So this is less a from-scratch migration than
**finishing one that's ~80% done at the mechanism level** and then retiring
Shortcuts as the *documented / onboarding* path.

What remains is three buckets:

1. **Gaps** — touchpoints with no native equivalent yet (trip-retro intent,
   self-care local notifications, location cadence).
2. **Hard cases** — things iOS historically wouldn't let a third-party app do
   natively (system alarms). *(Now largely closed: **AlarmKit** (iOS 26) schedules
   a real alarm natively; the Shortcut remains the pre-iOS-26 fallback — see the
   [Set Alarm](#the-hard-case-set-alarm) section.)*
3. **The paper cut** — docs, the `onboard-user` skill, and server self-
   description still tell users to build Shortcuts. That's the last mile. *(Now
   closed: the `onboard-user` skill and the user-facing docs — README, deployment,
   guide, the iOS README — are repointed at the native app with Shortcuts as the
   documented fallback, the server self-description + `source` provenance enum have
   shipped, and the iOS App Intents now send `source: "app_intent"` so the
   provenance is actually populated. This bucket is done.)*

The end state is **not** "delete Shortcuts." It's "native is the default path;
Shortcuts remain a documented fallback for free-signing installs" (see
[Constraints](#constraints-that-shape-the-plan)).

---

## Where we are today

The app runs two delivery/ingestion realities side by side:

| Layer | Native (paid-tier app) | Legacy (Shortcuts + n8n) |
|---|---|---|
| **Auth** | `X-Prefrontal-Token` via `APIClient` off the App Group (`ios/Prefrontal/Networking/APIClient.swift`) | same token, pasted into each shortcut |
| **Config** | `prefrontal://connect?url=…&token=…` QR/deep-link → `AppConfig` (`ConnectPayload.swift`) | manual URL + token per shortcut |
| **Push** | native APNs → `POST /route/apns-token` (the product transport; `PushNotifications.swift`, `delivery.py`) | ntfy DEV-ONLY shim (`PREFRONTAL_NTFY_DEV=1`) |
| **Push actions** | `UNNotificationCategory`/`UNNotificationAction` mirroring server buttons, fire signed `/nudge/act` (`PushNotifications.swift`) | ntfy inline buttons |
| **Leave-home** | `CLCircularRegion` geofence → `/webhooks/departure/left` + `/webhooks/location` (`Location/LocationMonitor.swift`) | "Leaving Home" automation |
| **Sessions** | Live Activities for outing/focus/task (`Activities/LiveActivityManager.swift`) | — |
| **One-tap actions** | App Intents (Siri / Action Button / Spotlight) + widgets + Controls (`Intents/PrefrontalIntents.swift`, `PrefrontalControls.swift`) | the shortcut catalog |
| **Offline safety** | App-Group offline queue drained by `BGTaskScheduler` (`Config/OfflineQueue.swift`) | lost if off-tailnet |

The single most important already-shipped piece is **App Intents**
(`ios/Prefrontal/Intents/PrefrontalIntents.swift`): eight `openAppWhenRun == false`
intents that authenticate via the App Group and drive the exact endpoints the
shortcuts hit — `AddTodo`, `Panic`, `GoingOut`, `ImBack`, `StartFocus`,
`EndFocus`, `MadeIt`, `MissedIt`. `AppShortcuts.swift` registers Siri phrases for
all eight, and `WidgetActionIntents.swift` adds `MarkSelfCare`. **This is the
native replacement for a hand-built shortcut** — same trigger surfaces (Siri,
Action Button, Home/Lock-screen widget, Control Center), no URL to paste.

---

## Touchpoint-by-touchpoint migration map

Each row is a shortcut from `deploy/ios-shortcut.md` and its native destination.
"Done" = a native surface already drives the same endpoint.

| Shortcut (trigger) | Endpoint | Native replacement | Status |
|---|---|---|---|
| Made it / Missed it | `POST /webhooks/shortcut` | `MadeItIntent` / `MissedItIntent` + notification action buttons | ✅ Done |
| Add Todo | `POST /todos` | `AddTodoIntent` (+ Siri param, widget quick-add) | ✅ Done |
| Capture (impulse) | `POST /webhooks/impulse/capture` | *(no intent yet)* — add `CaptureImpulseIntent` | ⚠️ Gap |
| Going out | `POST /webhooks/outing/start` | `GoingOutIntent` (intention as a parameter) | ✅ Done |
| I'm back | `POST /webhooks/outing/return` | `ImBackIntent` + `ImBackControl` | ✅ Done |
| Start / End focus | `/webhooks/focus/{start,end}` | `StartFocusIntent` / `EndFocusIntent` + `WrapUpFocusControl` | ✅ Done |
| Switching? (reflective pause) | `/webhooks/focus/{switch,resolve}` | *(no intent yet)* — needs a 2-step confirm intent | ⚠️ Gap |
| Panic | `GET /panic` (speak) | `PanicIntent` (`ProvidesDialog` → Siri speaks) + `PanicControl` | ✅ Done |
| Trip retro | `POST /webhooks/trip/retro` | *(no intent yet)* — label/domain buttons exist on the push | ⚠️ Gap |
| Update location (periodic) | `POST /webhooks/location` | geofence enter/exit only; no periodic native cadence | ⚠️ Partial |
| Leaving Home (departure) | `POST /webhooks/departure/left` | `LocationMonitor` `didExitRegion` for "home" | ✅ Done |
| Arrive Home (close outing) | `POST /webhooks/outing/return` | `LocationMonitor` `didEnterRegion` posts location; auto-close is server-side | ✅ Done |
| Departure reminder (leave-by) | n8n polls `/webhooks/departure/check` | `LocalNotifications.reconcileDeparture` schedules the next leave-by locally | ✅ Done (next-departure only) |
| Set Alarm (evening prep) | client-side `shortcuts://` deep-link | **AlarmKit** (`AlarmScheduler.swift`) on iOS 26+; Shortcut deep-link below that | ✅ Done (26+) / ⚠️ fallback |

---

## Remaining gaps (tractable)

These are straightforward additions in the existing `Intents/` +
`Notifications/` patterns.

### 1. Missing App Intents
Add three intents alongside the eight in `PrefrontalIntents.swift`, and register
Siri phrases in `AppShortcuts.swift`:

- **`CaptureImpulseIntent`** — a single-parameter intent (`impulse_text`) →
  `POST /webhooks/impulse/capture`. Mirrors `AddTodoIntent` exactly.
- **`TripRetroIntent`** — parameters for label + a `@Parameter` enum of
  life-domains (server vocab `shop/work/home/kids/personal`) + an optional
  reflection string → `POST /webhooks/trip/retro`. Today the ntfy/APNs push
  already carries one-tap domain buttons, so the intent is the *deliberate*
  (Siri/Action-Button) twin, not the only path.
- **`SwitchPauseIntent`** — the one genuinely stateful case: it must `POST
  /webhooks/focus/switch` (409 if no block), surface the returned
  `message`/`pause_seconds`, then `POST /webhooks/focus/resolve` with
  `return`/`defer`/`switch`. App Intents support this via a follow-up
  `requestConfirmation` / `IntentDialog` + a choice parameter, but it's the most
  involved of the three. Alternative: model it as a push with three action
  buttons (reflective-pause-as-notification) rather than an intent.

### 2. Self-care local notifications
`LocalNotifications.swift` currently schedules **only** the next departure's
leave-by. The self-care checks (meal/water/meds/…) still depend entirely on
server-driven APNs/ntfy, so they go silent off-tailnet. Extend
`reconcile*` to also lay down `UNCalendarNotificationTrigger`s for each enabled
self-care check's fire-times (read from `GET /self-care`), reconciled on refresh
— the same "off-tailnet safety net" rationale that already justifies the
departure one. Keep the server as the source of truth; the local copy is a
mirror that the next successful sync corrects.

### 3. Location cadence
Geofencing (`LocationMonitor`) covers **enter/exit of known places** — which is
what closed-loop trips and leave-home need. But the old "Update location"
shortcut also fed a *periodic* fix that powers coffee-shop-nudge gating and
departure travel-time estimates between geofences. Native options, cheapest
first:

- **`CLLocationManager.startMonitoringSignificantLocationChanges()`** — ~500m/
  cell-tower granularity, near-zero battery, wakes a terminated app. Good enough
  for "roughly where am I" gating.
- **`CLVisit` monitoring** (`startMonitoringVisits`) — arrivals/departures at
  places the OS infers, complements our explicit geofences.
- Post whichever into the existing `Endpoints.postLocation`. Continuous precise
  tracking stays out of scope (that's the Home Assistant tier).

---

## The hard case: "Set Alarm"

The evening `morning_prep` nudge (Time Blindness) carries a **⏰ Set alarm**
button that opens `shortcuts://run-shortcut?name=Set%20Alarm&input=text&text=<HH:MM>`
(`prefrontal/webhooks/notify.py` `alarm_actions()`; `DEFAULT_ALARM_SHORTCUT`).
Pre-iOS-26, **iOS exposed no public API to create a system Clock alarm** — the
Shortcuts "Create Alarm" action was the only sanctioned path — so this was the
one touchpoint that couldn't go native. The options weighed at the time:

1. **Keep the Shortcut** for the alarm specifically. Lowest effort, and the
   `alarm_shortcut_name` config already supports it.
2. **Surrogate wake via a critical/time-sensitive local notification** at the
   wake time (`UNTimeIntervalNotificationTrigger`, interruption level
   `.timeSensitive` or `.critical` with entitlement). It *alerts* but doesn't
   *ring like an alarm* through silent mode without the critical entitlement —
   weaker than a real alarm.
3. **`AlarmKit`** (introduced iOS 26) — the first native alarm-scheduling
   framework, ringing through silent mode / Focus like the system alarm.

**✅ Shipped (option 3 + option 1 as the fallback).** `AlarmScheduler.swift`
schedules the alarm natively via `AlarmManager` on iOS 26+, prompting once for
AlarmKit authorization (`NSAlarmKitUsageDescription`). The "Set alarm" tap is
handled in `AppDelegate` (`PushNotifications.swift`): it reads the suggested
`HH:MM` from the action's `shortcuts://…&text=` URL and, on iOS 26+, sets a
one-off system alarm; on older iOS — or if AlarmKit is unavailable/denied — it
falls back to **opening that same Shortcut deep link** (option 1). All AlarmKit
code sits behind `#if canImport(AlarmKit)` + `@available(iOS 26.0, *)`, so the
iOS 17 deployment target is unchanged. **The server side didn't move**: the nudge
still ships the same `shortcuts://` view action; the client decides native-vs-
Shortcut. The only thing left here is retiring the Shortcut fallback entirely once
the minimum target reaches iOS 26.

---

## Constraints that shape the plan

- **Paid Apple Developer account required** for APNs, App Groups, and Live
  Activities. `aps-environment` is intentionally omitted from the committed
  `Prefrontal.entitlements` (`ios/README.md`), so a **free-signing** build can't
  receive native APNs push. On such a build, push falls back to the **ntfy
  DEV-ONLY shim** (`PREFRONTAL_NTFY_DEV=1`, off by default) and, for capture, to
  Shortcuts — while **native APNs push is the product path**. This is the core
  reason the end state keeps Shortcuts (and the ntfy shim) as a documented
  free-signing fallback rather than deleting them.
- **App Group is the auth substrate** for intents/widgets (they can't reach the
  main-actor `AppConfig`). Any new intent must build its `APIClient` via
  `APIClient(shared:)`, like the existing ones.
- **Stateful lifecycle writes** (focus/outing start/return) are deliberately
  *not* offline-queued (`OfflineQueue.swift`) — a stale replay would corrupt
  session state. A `SwitchPauseIntent` must respect this (no queue on the
  switch/resolve pair).
- **Tailscale reachability** — native push (APNs) and local notifications work
  off-tailnet; every *write* still needs the server reachable, hence the offline
  queue for the capture-style actions and the local-notification mirror for
  reminders.

---

## Suggested sequencing

Small, independently-shippable steps; each leaves the app working:

1. **Close the intent gaps** — `CaptureImpulseIntent`, `TripRetroIntent`
   (parameters mirror the existing intents; ~a day each).
2. **Self-care local notifications** — extend `LocalNotifications` to mirror
   `GET /self-care` fire-times. Removes the biggest off-tailnet silence.
3. **`SwitchPauseIntent` or push-based reflective pause** — pick one; the push
   route is simpler and reuses `_NUDGE_BUTTONS`.
4. **Significant-location-change / `CLVisit`** — retire the periodic "Update
   location" shortcut.
5. **Alarm decision** — ✅ done: AlarmKit (iOS 26+) sets a native alarm, with the
   Shortcut deep link kept as the pre-iOS-26 fallback (`AlarmScheduler.swift`).
6. **Retire Shortcuts from onboarding** (below) — the actual "migration" from the
   user's point of view. ✅ Done: the `onboard-user` skill, the user-facing docs,
   the server self-description / `source` provenance enum, and the iOS App Intents
   sending `source: "app_intent"` have all shipped.

Steps 1–4 are additive and low-risk. Step 6 is the one users notice.

## Retiring Shortcuts from onboarding & docs

The last mile is documentation, not code:

- ✅ **`onboard-user` skill** (`.claude/skills/onboard-user/SKILL.md`) — Step 5's
  handoff now leads with the native app: connect via the `prefrontal://connect`
  QR, one-tap logging through App Intents (Siri / Action Button / widgets), with a
  "no paid dev account? use these Shortcuts" fallback appendix.
- ✅ **`README.md`, `docs/deployment.md`, `docs/guide.md`, `ios/README.md`** —
  iOS Shortcuts reframed as the free-signing fallback, native app as primary; the
  data-flow diagrams now read `App Intent / geofence → POST …` with the Shortcut as
  the fallback lane.
- ✅ **Server self-description + client provenance** — `prefrontal/__init__.py`
  and the FastAPI `summary` now name the native app alongside Shortcuts, and
  `POST /webhooks/shortcut` takes a `source` provenance enum
  (`app_intent`/`geofence`/`shortcut`, default `shortcut`) threaded into the
  `engaged` feature-usage event, so /stats can distinguish native taps from
  fallback-Shortcut ones. The endpoints are unchanged (the app hits the same ones).
  The iOS App Intents (`MadeItIntent`/`MissedItIntent` via `logShortcut`) now send
  `source: "app_intent"`, so the provenance is actually populated — a hand-built
  Shortcut omits it and the server defaults to `shortcut`.

Nothing in `deploy/ios-shortcut.md` needs deleting — it becomes the fallback
reference, not the primary setup.

---

## What explicitly does *not* migrate

- **n8n Twilio voice escalation** — server-side, already native to the delivery
  layer (`delivery.py`); unrelated to Shortcuts.
- **Home Assistant continuous location (Tier 2)** — an intentionally separate,
  higher-fidelity source; out of scope.
- **`deploy/ios-shortcut.md`** — retained as the free-signing fallback catalog.
