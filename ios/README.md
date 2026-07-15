# Prefrontal — iOS app

A native SwiftUI client for the Prefrontal API. It mirrors the daily-driver
surface of the web dashboard: a **Today** glance, interactive **Todos**, a
**Mail** triage inbox, a **Calendar** week view + free-slot finder, and a **Me**
tab for self-care, focus/outing controls, settings, and a behavioral **Insights**
screen — plus a **Panic** sheet.

It talks to the same FastAPI service as everything else, over Tailscale, using
the `X-Prefrontal-Token` header. Its **App Intents** (Siri / Action Button /
widgets) give one-tap logging with nothing to paste, and it receives push via
**APNs** — so on a paid-tier install it is the **primary** client, superseding the
iOS Shortcuts and ntfy paths. Those remain the **free-signing fallback** (no paid
Apple Developer account → no App Intents / widget / APNs). See
`../docs/shortcuts-to-native.md` for the full migration map and `../ROADMAP.md`
for what's next.

It ships a **WidgetKit extension** (`PrefrontalWidgets`) — Home Screen
(small/medium) and Lock Screen (rectangular/circular/inline) glances showing
your next departure, the one todo to start right now (with the next commitment
on its own line), and self-care progress. The Home Screen
widget is **interactive** (iOS 17): tapping the self-care chips logs a meal /
glass of water in place (`MarkSelfCareIntent`), and when an outing or focus
session is active it shows a one-tap **I'm back** / **Wrap up** button
(`ImBackIntent` / `EndFocusIntent`) — all without launching the app. A separate
**configurable** Lock Screen circular ring (`SelfCareCircleWidget.swift`) lets you
long-press → **Edit** → pick which self-care check it tracks (Water, Meals, …),
rather than being fixed to water. A separate **"one next thing"** widget
(`OneNextThingWidget.swift`, all five families) shows the *single* honest next
action and nothing else — the mid-flight task you're in, a commitment to leave
for, the worst clock-bound fire, or the avoided-but-important todo you keep
skipping — withholding everything else behind a "+N more can wait" line. It reads
`GET /next`, so the phone never re-ranks; the mid-flight cases carry the same
one-tap **Wrap up** / **I'm back** buttons. When an outing or focus session is running it
also shows a **Live Activity** on the Lock Screen and in the Dynamic Island — a
self-ticking "back by" countdown (outing) or elapsed timer (focus), started and
ended by `Activities/LiveActivityManager` and rendered by
`PrefrontalWidgets/SessionLiveActivity.swift`. The app and widget share the base
base URL via the **App Group** `group.com.morningstatic.prefrontal`, and the
token via a shared **Keychain** access group (`…prefrontal.shared`).

## Layout

```
ios/
  project.yml              # XcodeGen spec — source of truth for the Xcode project
  Prefrontal/
    PrefrontalApp.swift    # @main; routes prefrontal:// connect deep links
    Prefrontal.entitlements# App Group + shared Keychain group (both shared with the widget)
    Config/                # AppConfig + SharedStore (App Group base URL); KeychainStore (token)
    Networking/            # APIClient (async URLSession) + typed Endpoints
    Models/                # Codable structs mirroring the JSON API
    Theme/                 # Brand palette + Card
    Onboarding/            # ConnectPayload, OnboardingModel, QRScannerView
    Intents/               # App Intents (Siri/Shortcuts/Spotlight/Action Button)
    Activities/            # LiveActivityManager (starts/ends the Live Activity)
    Notifications/         # LocalNotifications + AppDelegate (APNs, categories)
    Location/              # LocationMonitor (opt-in geofences over /places)
    Views/                 # RootView, Onboarding, Today, Todos, Calendar, Me, Panic, Settings
    Watch/                 # WatchProtocol (shared wire types) + PhoneWatchConnectivity (relay)
    Assets.xcassets/       # app icon (brand mark) + accent color
  PrefrontalWidgets/       # WidgetKit extension (Home + Lock Screen glances)
    PrefrontalWidgets.swift
    PrefrontalWidgets.entitlements  # same App Group + shared Keychain group
  PrefrontalWatch/         # watchOS companion app (relays through the phone)
    PrefrontalWatchApp.swift, WatchConnectivityClient, WatchModel, Watch*View
  PrefrontalWatchWidgets/  # watch-face complications (WidgetKit accessory families)
```

The widget target re-uses the app's `Config/`, `Networking/`, `Models/`,
`Theme/` sources (compiled into the extension too) and reads config from the
shared App Group (base URL) and shared Keychain group (token), so it
authenticates without you entering the token twice.

## Apple Watch companion

`PrefrontalWatch/` is a native watchOS app — a thin, glanceable client for the
wrist. Because the API is **Tailscale-only** and a standalone Apple Watch usually
isn't on the tailnet, the watch **relays every request through the paired
iPhone** over **WatchConnectivity** (`WCSession`) rather than talking to the
server itself:

```
Watch  ──sendMessage(kind, params)──▶  iPhone (PhoneWatchConnectivity)
  │  decode with shared Models              │  APIClient(shared:) → Endpoints
  └──────────  JSON reply  ◀────────────────┘
```

So the watch **holds no token and makes no network calls** — the phone (which
already has the token in its Keychain and is on the tailnet) runs the matching
`APIClient` call and relays the JSON back. The watch reuses only the phone's
`Models/Models.swift` (pure Foundation) plus the shared wire types in
`Prefrontal/Watch/WatchProtocol.swift`. The phone pushes a lightweight
**connection status** (`updateApplicationContext`) so the watch can show
"open Prefrontal on your iPhone" until it's set up — the token deliberately never
leaves the phone.

Surfaces (a vertical-paging `TabView`):

- **Today** — the next departure's leave-by (colored by escalation level), the one
  thing to do now, or free time; an active outing/focus shows its one-tap end.
- **Self-care** — a row per enabled check with `count/target`; tap to log, tap at
  the target to wrap back to zero (the tap-at-max cycle, like the phone/widget).
- **Actions** — **I'm back** / **Wrap up focus** (only while active), **Panic**
  (shows the headline + first step), and a dictated **Add todo**.

**Complications** (`PrefrontalWatchWidgets/`, WidgetKit accessory families —
inline / circular / rectangular) render from the last glance the watch app cached
to a watch-local App Group; the app reloads them on each refresh.

Offline behavior mirrors the phone: **capture writes** (self-care, add todo) that
can't reach the phone fall back to `transferUserInfo` (queued, at-least-once);
**lifecycle writes** (I'm back / wrap up) require reachability and are never
queued (a stale replay would be wrong). The phone re-runs capture writes through
the same `queueable` endpoints, so they land in its own `OfflineQueue` if it too
is off-tailnet — durable end to end.

> **Paid tier for the complication.** The complication's App Group
> (`group.com.morningstatic.prefrontal.watch`) needs a paid team, like the phone
> widget's. The watch app itself relays through the phone and needs no capability,
> so it runs under free signing.

The watch app is embedded in the phone app (XcodeGen wires the Embed Watch Content
phase), so installing Prefrontal on the iPhone installs the watch app too. Build
it standalone with `xcodebuild -scheme PrefrontalWatch` (a committed scheme).

The `.xcodeproj` is **generated** and git-ignored. Regenerate any time with:

```sh
brew install xcodegen        # once
cd ios && xcodegen generate
```

## First-time setup on this Mac

The Simulator/device build components need a one-time install (this is why a
plain `xcodebuild` currently fails to load `CoreSimulator`). Run once:

```sh
sudo xcodebuild -runFirstLaunch     # installs CoreSimulator + friends
```

(Or just open `Xcode.app` once and let it install components when prompted.)

To sanity-check the code compiles without a full build (no components needed):

```sh
cd ios/Prefrontal
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
find . -name '*.swift' -print0 | xargs -0 xcrun swiftc -sdk "$SDK" -target arm64-apple-ios17.0 -typecheck
```

## Linting

[SwiftLint](https://github.com/realm/SwiftLint) is configured in
[`.swiftlint.yml`](.swiftlint.yml). Run it from `ios/`:

```sh
brew install swiftlint   # once
cd ios && swiftlint
```

CI runs the same lint plus the compile check above on every change under `ios/`
(`.github/workflows/ios.yml`, macOS runner). It's non-strict for now — only
error-severity rules fail the build — so a first pass can land green; tighten to
`swiftlint --strict` once the baseline is clean.

## Tests

Unit tests live in `PrefrontalTests/` and run on a simulator:

```sh
cd ios && xcodegen generate
# Pick any installed iPhone simulator by UDID (robust to device-name churn) —
# the same selection CI uses.
UDID=$(xcrun simctl list devices available | grep -m1 'iPhone' | grep -oiE '[0-9a-f-]{36}' | head -1)
xcodebuild test -project Prefrontal.xcodeproj -scheme Prefrontal -destination "id=$UDID"
```

CI runs the same via `xcodebuild test` (it picks an available simulator by UDID).
The first cut covers the pure/logic seams that need no device or live server —
`ConnectPayload` deep-link parsing to start; `APIClient` request-building and the
offline queue are the natural next additions (they need a small testable init /
URLProtocol stub on `APIClient`).

## Run on your iPhone

The widget uses an **App Group** and a **shared Keychain access group** (for the
token), both of which require a **paid Apple Developer account** (neither is
available to free "Personal Team" signing). The app alone runs under free
signing, but the full app + widget needs the paid tier.

1. **Set your team once, locally** — create a git-ignored override; it survives
   `xcodegen generate` **and** fresh clones, so you never re-enter the team:
   ```sh
   cp ios/Signing.local.xcconfig.example ios/Signing.local.xcconfig
   # then edit ios/Signing.local.xcconfig → DEVELOPMENT_TEAM = <YOUR_TEAM_ID>
   ```
   `Signing.xcconfig` (tracked) optionally `#include?`s that local file, so the
   team applies to both targets without being committed — no
   `git update-index --skip-worktree` needed. Find the Team ID in Xcode ▸
   Settings ▸ Accounts ▸ (your team), or at developer.apple.com ▸ Membership.
2. `cd ios && xcodegen generate && open Prefrontal.xcodeproj`
3. Both targets use **Automatically manage signing**; with the team set, Xcode
   registers the App Group (`group.com.morningstatic.prefrontal`) on the portal
   automatically. Bundle ids: app `com.morningstatic.prefrontal`, widget
   `…prefrontal.widgets`.
4. Plug in your iPhone (`iphone171-1` is on the tailnet). Enable **Developer
   Mode** if asked (Settings ▸ Privacy & Security ▸ Developer Mode → on → reboot).
5. Pick your phone as the run destination and hit **⌘R**. Then add the widget
   from the Home/Lock Screen gallery.

## Connecting the app to your server

On first launch the app runs a short **onboarding flow** — welcome → connect →
notifications → done (design: `../docs/design/ios-onboarding.md`). Connect is
**QR-first**:

- **Scan to connect (recommended):** the operator hands you a QR code (see
  below). Point the camera at it in the connect step — or, if the sheet's on
  another screen, scan it with the iOS **Camera** app and tap "Open in
  Prefrontal". Either way the app fills in the server URL + token for you.
- **By hand (fallback):** expand *Enter details manually* and type:
  - **Server URL:** `https://agent-1.tail8b0a.ts.net` (from `tailscale serve`;
    valid TLS, so no plaintext-HTTP exception is needed). The plain
    `http://<mac>:8000` tailnet URL also works but would need an ATS exception.
  - **Token:** your personal `X-Prefrontal-Token` (from your setup sheet, or
    `prefrontal user connect-link <handle> --rotate`).

Tap **Connect** to verify against `/self-care` before advancing. The base URL
lives in the App Group and the token in a shared Keychain group, so the widget
authenticates too; change either later in
**Me ▸ Settings** (gear icon). That screen also has a **Features** section — a
per-user toggle for each enabled module **and Context pack** (`/settings/features`),
so you can turn off support behaviors you don't want without changing the
deployment default (turning a pack off hides its situation tools + care surface
for you) —
an **Available hours** section — a per-weekday toggle + start/end time pickers
that write `/schedule/available-hours`, so the slot-finder and todo suggestions
stay inside the hours you're free (an off day is skipped entirely) — and a
read-only **Diagnostics** section showing the App Group sharing state (use it if a widget
shows "Tap to connect" while the app works: that means the App Group capability
isn't provisioned into the widget target; enable it on both targets, same Team,
and reinstall).

### Producing the QR (operator)

```sh
prefrontal user connect-link <handle> --qr            # prints link + scannable QR
prefrontal user connect-link <handle> --qr --rotate   # also mint & embed a token
pip install 'prefrontal[qr]'                          # once, to render the QR
```

The QR encodes a `prefrontal://connect?url=…&token=…&ntfy_topic=…` deep link.
Treat it like the token itself — it *is* the token, just denser. The base URL
defaults to `OAUTH_BASE_URL`; ntfy hints come from the user's delivery route.

## Offline capture queue + background refresh

Off the tailnet, a write would otherwise just fail and the capture would be
lost. Capture writes — **Capture a Thought**, **Add Todo**, **self-care** marks,
**Made it / Missed it** — are marked `queueable`, so on a transport failure `APIClient` persists
them to an App-Group-backed `OfflineQueue` (shared by the app, widget, and
intents) instead of erroring. They replay oldest-first when the app next comes
to the foreground, and opportunistically via a **Background App Refresh** task
(`com.morningstatic.prefrontal.refresh`), which also reloads the widget. Today
shows a small "N changes waiting to sync" banner while the queue is non-empty.

Stateful lifecycle writes (focus/outing start/return) are deliberately **not**
queued — replaying a "start focus" long after the fact would log a bogus
session. Delivery is at-least-once (a replay can double-apply if the original
actually landed), which is acceptable for todos/self-care.

The reverse case — nudges that can't reach you off-tailnet — is covered by
**local notifications** (`Notifications/LocalNotifications.swift`): each Today
refresh schedules a local "leave by" alert for the next departure **and** a
next-due alert for each self-care check (from `/self-care`'s `next_due`), which
fire at those times even after the phone leaves the tailnet (where ntfy/APNs
can't). Both reconcile each refresh (a moved departure or a satisfied check
replaces/drops the pending one) and no-op unless notifications were authorized
during onboarding.

## What maps to what

| Screen | Endpoints |
|---|---|
| Today | `/todos/now`, `/departure/next`, `/outings`, `/focus`, `/nudges`, `/briefing`; Add → `POST /todos`; briefing 👍/👎 → `POST /briefing/feedback`; Panic; **Situations** → `/packs/situations`, run → `POST /packs/situations/{tool}` (shown only when a Context Pack contributes tools) |
| Todos | `/todos`; `POST /todos`, `/todos/{id}/start` · `/unstart` · `/done` · `/drop` · `/decompose` · `/steps/{i}/done`; Edit → `/todos/{id}/deadline` · `/notes`; Delegate → `/todos/delegate-recipients`, `/todos/{id}/delegate` · `/delegate/return`; Stuck & avoided → `/todos/stuck`, `/todos/avoided` |
| Clarify | `/clarifications`; resolve → `POST /clarifications/{id}/resolve`, dismiss → `POST /clarifications/{id}/dismiss`, sweep → `POST /clarifications/check`, guide → `/clarifications/playbooks/{task_type}` — reached from **Todos** |
| Mail | `/mail` (read-only: `needs_action` + `recent`) |
| Calendar | `/commitments` (+ its `previous` list), `/calendar/slots`; Made it/Missed it → `POST /commitments/{id}/outcome`; conflicts → `/commitments/conflicts`, reschedule → `.../conflicts/reschedule`, dismiss → `.../conflicts/dismiss` |
| Me | `/self-care` + `/self-care/mark`, `/self-care/review` (end-of-day gap recap); `/webhooks/focus/start` · `/end`; `/webhooks/outing/start` · `/return` |
| Insights | `/stats/data` (estimate bias, follow-through, channels, self-care, feature usage), `/balance` (focus balance) — reached from **Me** |
| Panic | `/panic` |

## Native push (APNs)

When the server is APNs-configured (`APNS_*` env + the `prefrontal[apns]` extra),
the app opts this device into **native Apple Push** instead of ntfy: on
notification authorization it registers for remote notifications, and
`AppDelegate` posts the device token to `POST /route/apns-token`. Nudges then
arrive as native notifications whose **action buttons** (registered as
`UNNotificationCategory`s mirroring the server's nudge buttons — *I'm back*,
*Ate*, *Wrap up*, …) fire the signed `/nudge/act` URL from the payload on tap —
the native equivalent of ntfy's inline buttons. The server falls back to ntfy for
any device that hasn't registered. See the server side in
`prefrontal/integrations/delivery.py` + `docs/multi-tenant.md`.

> **Paid tier only.** Native push needs the **Push Notifications** capability
> (the `aps-environment` entitlement), which — like App Groups — requires a
> **paid Apple Developer account**. A free "Personal Team" can't mint a profile
> that includes it, and Xcode will refuse to sign the app if it's declared. So
> `aps-environment` is **not** in the committed `Prefrontal.entitlements`; the
> app builds and signs on free signing and just uses ntfy.
>
> To turn on native push, don't add the capability in Xcode's UI — that writes it
> into the **git-ignored, regenerated** `.xcodeproj`, so `xcodegen generate` wipes
> it. Instead flip one line in your git-ignored `Signing.local.xcconfig` (same
> place as your Team ID), pointing at the committed push-enabled entitlements file:
>
>     PREFRONTAL_ENTITLEMENTS = Prefrontal/Prefrontal.push.entitlements
>
> then `cd ios && xcodegen generate`. Because the build only *references* that
> file (never regenerates it), the capability is permanent — regeneration-proof.
> The `aps-environment` value tracks the build config via `APS_ENVIRONMENT`
> (`development` for Debug/run-to-device → **sandbox** APNs, `production` for
> Release/TestFlight); the server's `APNS_USE_SANDBOX` must match. The APNs client
> code ships either way and is a no-op without the entitlement
> (`didFailToRegisterForRemoteNotifications` is handled).

## Location automations (opt-in geofencing)

Off by default. Enabled in **Me ▸ Settings ▸ Location automations**, it prompts
for **Always** location and monitors your curated places (`GET /places`) with
`CLCircularRegion` geofences (`Location/LocationMonitor.swift`). Leaving the
place named **home** posts `/webhooks/departure/left` — the native replacement
for the "when I leave Home" Shortcut automation — and any enter/exit posts the
current position to `/webhooks/location`, feeding departure-timing and outing
distance without a tap. Arriving **home** with an outing active posts
`/webhooks/outing/return` to close it tap-free (#563), the native replacement for
the "I'm back" arrival automation. Region monitoring is battery-cheap (the OS
wakes the app only on a crossing, even from terminated; `AppDelegate` re-attaches
the delegate on launch). Add places with `prefrontal place add <name> <lat> <lon>`.

Alongside the geofences it also runs a **significant-location-change** feed
(#562) — coarse (~500 m / cell-tower) position updates that keep
`/webhooks/location` fresh *between* curated places (what departure travel-time
and trip stop-detection need), replacing the Shortcuts "Update location"
automation. Same Always-location opt-in; also battery-cheap and terminated-state
capable. Posts are throttled (default 5 min floor; web-configurable under #565).

A **`CLVisit`** feed (#564) covers arrivals/departures at *arbitrary* venues
beyond the ≤18 curated places, posting their coordinate to `/webhooks/location`
too. All three feeds share a de-dupe guard so a `CLVisit` coinciding with a
curated-place geofence crossing doesn't double-post. Permission handling is
hardened (#566): the Settings section primes before the one-shot prompt, shows
the live authorization state, offers an "upgrade to Always" path from
While-Using, and points to iOS Settings when denied.

Together these **fully replace the location Shortcut automations** — a native-app
user needs none of them. `deploy/ios-shortcut.md` keeps the Shortcuts stack
documented for **web / ntfy-only** users (no app), with the native equivalents
mapped at the top.

## App lock (Face ID / Touch ID)

Off by default. Enabled in **Me ▸ Settings ▸ App Lock** (the toggle only appears
when the device has enrolled biometrics), it gates the whole app behind **Face ID**
— or Touch ID / Optic ID — on launch and whenever the app returns from the
background. The token lives in the Keychain and the tabs show personal schedule,
todo, and panic data, so this keeps a borrowed unlocked phone from exposing them.

`Security/BiometricLock.swift` (app-target only — it imports `LocalAuthentication`,
which the widget doesn't need) owns the lock state; `RootView` covers the app with
`Views/LockView.swift` while locked. The cover also shows whenever the scene isn't
active, so it doubles as the app-switcher privacy shield. Authentication uses
`LAPolicy.deviceOwnerAuthentication`, so the **device passcode** is the built-in
fallback and a failed biometric scan can't lock you out. Needs
`NSFaceIDUsageDescription` (set via `INFOPLIST_KEY_NSFaceIDUsageDescription` in
`project.yml`). No paid-tier capability required — this one works under free
signing.

The auto-prompt is fired from one-shot UI edges (`RootView.onAppear`,
`scenePhase → .active`), so `authenticate()` **self-heals once per locked
session**: an attempt that ends without unlocking through no deliberate user
action — most notably the first-launch Face ID *consent* round-trip, whose
evaluation returns without ever scanning — retries automatically rather than
stranding the lock screen. A real `.userCancel` (the manual button is the retry)
or `.authenticationFailed` (shows the error) doesn't retry, so it can't loop. The
`LocalAuthentication` seams are injectable for the hermetic `BiometricLockTests`.

## Siri / Shortcuts / Action Button (App Intents)

`Intents/PrefrontalIntents.swift` exposes the core one-tap actions as **App
Intents**, so they work from Siri, the Shortcuts app, Spotlight, and the Action
Button without hand-building "Get Contents of URL" shortcuts or pasting tokens:

- **Capture a Thought** (takes free text) → `POST /observe` (the sensor path)
- **Add Todo** (takes a title) → `POST /todos`
- **Panic** → `GET /panic` (speaks the headline + first step)
- **Going Out** (intention + optional window) → `POST /webhooks/outing/start`
- **I'm Back** → `POST /webhooks/outing/return`
- **Start Focus** (task + optional length) / **Wrap Up Focus** → `/webhooks/focus/start` · `/end`
- **Made It / Missed It** → `POST /webhooks/shortcut`

Each intent authenticates like the widget does — `APIClient(shared:)` reads the
base URL + token from the App Group — so it runs in the background
(`openAppWhenRun == false`) even when the app isn't open, and reloads the widget
timeline after a state change. Siri phrases are registered in
`PrefrontalShortcuts` (`AppShortcutsProvider`, `Intents/AppShortcuts.swift`);
assign any of them to the Action Button in **Settings ▸ Action Button ▸ Shortcut**.

### Capture this thought (zero-friction capture → the sensor path)

**Capture a Thought** is the headline capture surface: a passing thought is fed to
the LLM-as-sensor (`POST /observe`), which only *proposes* pending candidate
updates for review — it never writes an authoritative fact on capture — so the
tap is safe from anywhere. It's reachable three ways, all landing in the same
sensor path:

- **Action Button / Siri** — assign **Capture a Thought** to the Action Button;
  a press prompts for the thought via dictation and sends it in the background,
  no app launch (`CaptureThoughtIntent`).
- **Interactive widget** — the ✎ button in the Home Screen widget header opens the
  app's pre-focused capture field in one tap (`OpenThoughtCaptureIntent`).
- **Control Center / Lock Screen** — the **Capture a Thought** control does the
  same (iOS 18, `CaptureThoughtControl`).

The widget/Control Center surfaces can't collect free text inline, so they open
the app's quick-capture sheet (`Views/CaptureThoughtView.swift`) via a shared
App-Group hand-off (`SharedStore.requestCapture()` → `RootView`); the sheet — and
a `prefrontal://capture` deep link — both feed `APIClient.observe`. Captures are
`queueable`, so a thought jotted off the tailnet replays on reconnect rather than
being lost.

### Control Center controls (iOS 18)

The widget extension also ships **Control Center controls**
(`PrefrontalWidgets/PrefrontalControls.swift`) for the no-input actions —
**Panic**, **I'm Back**, **Wrap Up Focus** — each firing the matching App Intent
without opening the app, plus **Capture a Thought**, which opens the quick-capture
sheet (the one control that needs the app, since a thought is free text). Add them
in **Settings ▸ Control Center** (or assign one to the Action Button under
**Controls**). Other input actions (Add Todo, Going Out, Start Focus) stay in
Siri/Shortcuts, which can prompt for their values. The
action intents live in `Intents/PrefrontalIntents.swift`, compiled into both the
app (for Siri) and the widget extension (for the controls).

## System alarms (AlarmKit, iOS 26)

The evening **morning-prep** nudge (Time Blindness) suggests a wake time and
carries a **⏰ Set alarm** button. Tapping it sets a real, silent-mode-piercing
system alarm natively via **AlarmKit** (`Notifications/AlarmScheduler.swift`) —
the one touchpoint iOS historically gave no public API for, so it was the last
thing still delegated to a hand-built "Set Alarm" Shortcut (`deploy/ios-shortcut.md`;
see the "hard case" in `docs/shortcuts-to-native.md`).

- **iOS 26+**: the tap schedules a one-off alarm at the next occurrence of the
  suggested `HH:MM` via `AlarmManager`, prompting once for AlarmKit authorization
  (the `NSAlarmKitUsageDescription` string in `project.yml`). The action opens the
  app (it's registered `.foreground`, so the first-run permission prompt has
  somewhere to appear), but no Shortcut is involved and no wake time is typed by
  hand — the alarm is set for you.
- **Older iOS / AlarmKit unavailable or denied**: it falls back to opening the
  `shortcuts://run-shortcut?name=Set%20Alarm&text=<HH:MM>` deep link the server
  already sends — the pre-AlarmKit path — so nothing regresses.

The server side is unchanged: the nudge still ships the same `shortcuts://` view
action (`notify.alarm_actions`); the client reads the wake time out of that URL's
`text` and decides native-vs-Shortcut. All AlarmKit code sits behind
`#if canImport(AlarmKit)` + `@available(iOS 26.0, *)`, so the app still builds and
runs on the iOS 17 deployment target (the button just opens the Shortcut there).
