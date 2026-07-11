# Prefrontal — iOS app

A native SwiftUI client for the Prefrontal API. It mirrors the daily-driver
surface of the web dashboard: a **Today** glance, interactive **Todos**, a
**Calendar** week view + free-slot finder, and a **Me** tab for self-care,
focus/outing controls, and settings — plus a **Panic** sheet.

It talks to the same FastAPI service as everything else, over Tailscale, using
the `X-Prefrontal-Token` header. It does **not** replace ntfy or the iOS
Shortcuts — those still handle push and one-tap logging. This app is the
foreground client. See `../ROADMAP.md` (and the GitHub project) for the native
features planned next (background refresh, native notification actions).

It ships a **WidgetKit extension** (`PrefrontalWidgets`) — Home Screen
(small/medium) and Lock Screen (rectangular/circular/inline) glances showing
your next departure, what-fits-now, and self-care progress. The app and widget
share the base URL + token via the **App Group** `group.com.morningstatic.prefrontal`.

## Layout

```
ios/
  project.yml              # XcodeGen spec — source of truth for the Xcode project
  Prefrontal/
    PrefrontalApp.swift    # @main
    Prefrontal.entitlements# App Group (shared with the widget)
    Config/                # AppConfig + SharedStore (App Group base URL + token)
    Networking/            # APIClient (async URLSession) + typed Endpoints
    Models/                # Codable structs mirroring the JSON API
    Theme/                 # Brand palette + Card
    Views/                 # RootView, Today, Todos, Calendar, Me, Panic, Settings
    Assets.xcassets/       # app icon (brand mark) + accent color
  PrefrontalWidgets/       # WidgetKit extension (Home + Lock Screen glances)
    PrefrontalWidgets.swift
    PrefrontalWidgets.entitlements  # same App Group
```

The widget target re-uses the app's `Config/`, `Networking/`, `Models/`,
`Theme/` sources (compiled into the extension too) and reads config from the
shared App Group, so it authenticates without you entering the token twice.

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

## Run on your iPhone

The widget uses an **App Group**, which requires a **paid Apple Developer
account** (App Groups aren't available to free "Personal Team" signing). The
app alone runs under free signing, but the full app + widget needs the paid tier.

1. **Set your team once, locally** (survives `xcodegen generate`, never committed):
   ```sh
   git update-index --skip-worktree ios/Signing.xcconfig   # ignore local edits
   # then edit ios/Signing.xcconfig → DEVELOPMENT_TEAM = <YOUR_TEAM_ID>
   ```
   Find the Team ID in Xcode ▸ Settings ▸ Accounts ▸ (your team), or at
   developer.apple.com ▸ Membership.
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

On first launch the app shows a **Connect** screen. It prefills the tailnet
HTTPS origin this deployment already serves:

- **Server URL:** `https://agent-1.tail8b0a.ts.net` (from `tailscale serve`;
  valid TLS, so no plaintext-HTTP exception is needed). The plain
  `http://<mac>:8000` tailnet URL also works but would need an ATS exception.
- **Token:** your personal `X-Prefrontal-Token` (from your setup sheet, or
  `prefrontal user token <handle>`).

The token is stored in the **Keychain**; the URL in UserDefaults. Tap
**Connect** to verify against `/self-care` before saving. Change either later in
**Me ▸ Settings** (gear icon).

## What maps to what

| Screen | Endpoints |
|---|---|
| Today | `/todos/now`, `/departure/next`, `/outings`, `/focus`, `/nudges`; Add → `POST /todos`; Panic |
| Todos | `/todos`; `POST /todos`, `/todos/{id}/start` · `/unstart` · `/done` · `/drop` · `/decompose` · `/steps/{i}/done` |
| Calendar | `/commitments`, `/calendar/slots` |
| Me | `/self-care` + `/self-care/mark`; `/webhooks/focus/start` · `/end`; `/webhooks/outing/start` · `/return` |
| Panic | `/panic` |
