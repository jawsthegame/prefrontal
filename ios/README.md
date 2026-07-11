# Prefrontal — iOS app

A native SwiftUI client for the Prefrontal API. It mirrors the daily-driver
surface of the web dashboard: a **Today** glance, interactive **Todos**, a
**Calendar** week view + free-slot finder, and a **Me** tab for self-care,
focus/outing controls, and settings — plus a **Panic** sheet.

It talks to the same FastAPI service as everything else, over Tailscale, using
the `X-Prefrontal-Token` header. It does **not** replace ntfy or the iOS
Shortcuts — those still handle push and one-tap logging. This app is the
foreground client. See `../ROADMAP.md` (and the GitHub project) for the native
features planned next (widgets, background refresh, native notification actions).

## Layout

```
ios/
  project.yml              # XcodeGen spec — source of truth for the Xcode project
  Prefrontal/
    PrefrontalApp.swift    # @main
    Config/                # AppConfig (base URL + token), Keychain
    Networking/            # APIClient (async URLSession) + typed Endpoints
    Models/                # Codable structs mirroring the JSON API
    Theme/                 # Brand palette + Card
    Views/                 # RootView, Today, Todos, Calendar, Me, Panic, Settings
    Assets.xcassets/       # app icon (brand mark) + accent color
```

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

## Run on your iPhone (free personal signing)

1. `cd ios && xcodegen generate && open Prefrontal.xcodeproj`
2. In Xcode: select the **Prefrontal** target → **Signing & Capabilities**.
   - Check **Automatically manage signing**.
   - **Team:** add your Apple ID (Xcode ▸ Settings ▸ Accounts ▸ +) and pick the
     personal team. Free tier is fine.
   - The bundle id is `com.morningstatic.prefrontal`; if signing complains it's
     taken, change it to something unique (e.g. add your initials).
3. Plug in your iPhone (`iphone171-1` is already on the tailnet). Trust the Mac
   when prompted. Enable **Developer Mode** on the phone if asked
   (Settings ▸ Privacy & Security ▸ Developer Mode).
4. Pick your phone as the run destination and hit **⌘R**.
5. First launch on device: Settings ▸ General ▸ VPN & Device Management → trust
   your developer profile.

> Free personal provisioning profiles expire after **7 days** — re-run from
> Xcode to refresh. A paid Apple Developer account removes that limit.

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
