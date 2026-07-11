# iOS onboarding workflow

Status: **implemented** (first pass — QR/deep-link connect + notifications walkthrough)
Author: drafted with Claude, 2026-07-11

## Problem

Getting a new phone onto Prefrontal is the roughest edge of the whole product.
The native app's entire first-run experience is a single **Connect** screen
(`SettingsView(isOnboarding: true)`): two text fields where you hand-type a
tailnet HTTPS origin and paste a ~43-character opaque token. Everything else a
new user needs — install ntfy, subscribe to the right topic, allow
notifications, add the widget — lives only in a setup sheet the operator
produces out-of-band (the `onboard-user` skill). Nothing connects the two.

Hand-typing a long random token on a phone is exactly the kind of high-friction,
error-prone task this app exists to remove. A mistyped character reads back as
a generic `401 Unauthorized` with no hint which field is wrong.

## Goal

A new phone goes from "just installed" to "connected and receiving nudges" by
**scanning one QR code** off the operator's setup sheet, then being walked
through notifications — with hand-entry kept as a fallback, never the default.

## Flow

Four steps, driven by `OnboardingModel` (`active` gates the walkthrough vs. the
main tabs; kept separate from `AppConfig.isConfigured` so the flow survives past
the connect step):

1. **Welcome** — what Prefrontal is, in three lines. One button.
2. **Connect** — QR-first. "Scan QR code" opens the camera; a disclosure holds
   the manual URL + token fields for when there's no sheet to scan. Either path
   ends by validating against `GET /self-care` before advancing, so a bad
   token fails *here*, with the server's own error, not three screens later.
3. **Notifications** — the honest two-parter: (a) install **ntfy** and subscribe
   it to your topic (prefilled + copyable when the connect payload carried it),
   and (b) grant iOS notification permission (for the native alerts the ROADMAP
   adds; ntfy does the delivery today).
4. **Done** — confirmation, a nudge to add the Home Screen widget, and out.

## The connect payload

The operator→phone handoff is a `prefrontal://connect` deep link:

```
prefrontal://connect?url=<https origin>&token=<token>
  &ntfy_server=<server>&ntfy_topic=<topic>&handle=<handle>&name=<display name>
```

- Only `url` is required. `token` may be omitted (the user pastes it); the ntfy
  hints just prefill step 3.
- Rendered as a **QR** on the setup sheet, iOS Camera recognises the custom
  scheme and offers "Open in Prefrontal" directly — no in-app scan needed when
  the sheet is on another screen. As a plain link it's tappable in the sheet
  itself (handled by `.onOpenURL`, which routes to the same connect step).
- Parsed by `ConnectPayload` (iOS) and built by `build_connect_link()` (CLI).
  **These two must stay in sync** — same query keys, same semantics.

Why a custom scheme and not a Universal Link: Universal Links need a published
`apple-app-site-association` file on an HTTPS host, which a self-hosted tailnet
deployment doesn't have. A custom scheme needs nothing but the app installed.

### Producing it (operator)

```sh
prefrontal user connect-link <handle> --qr            # link + scannable QR
prefrontal user connect-link <handle> --qr --rotate   # also mint+embed a token
```

The token is shown only once at provisioning and isn't re-readable, so without
`--rotate` the link carries no token (the user pastes theirs). `--rotate` mints
a fresh one and embeds it, invalidating the old. ntfy server/topic come from the
user's resolved delivery route; the base URL defaults to `OAUTH_BASE_URL`.

QR rendering uses the optional `segno` dependency (`pip install
'prefrontal[qr]'`); without it the command still prints the plain link.

## Security notes

- The token is as sensitive as it was before — a QR is just a denser rendering
  of the same secret. It's scanned locally off a sheet, never transmitted by us.
  Treat the QR like the token: don't commit it, don't post it.
- The token still lands in App Group `UserDefaults`, not the Keychain (see the
  note in `AppConfig.swift`). Hardening to a shared Keychain access group is
  unchanged by this work and still tracked separately.
- `NSCameraUsageDescription` explains the camera is for scanning the setup-sheet
  QR. The scanner tears down its `AVCaptureSession` on dismiss.

## Files

- `ios/Prefrontal/Onboarding/ConnectPayload.swift` — parse/build the deep link.
- `ios/Prefrontal/Onboarding/OnboardingModel.swift` — flow state + notification
  permission.
- `ios/Prefrontal/Onboarding/QRScannerView.swift` — AVFoundation QR scanner.
- `ios/Prefrontal/Views/OnboardingView.swift` — the four-step UI.
- `ios/Prefrontal/Config/AppConfig.swift` — carries ntfy hints + `apply(payload)`.
- `ios/Prefrontal/PrefrontalApp.swift` / `Views/RootView.swift` — deep-link
  routing + the walkthrough-vs-tabs seam.
- `ios/project.yml` — `prefrontal` URL scheme + camera usage string.
- `prefrontal/cli.py` — `build_connect_link()` + `user connect-link`.

## Not in this pass (future)

- Native push delivery / background refresh / notification action buttons — the
  permission is requested now so these light up without a second prompt.
- Provisioning *from* the app (it still assumes an operator-provisioned user).
- Shared-Keychain token storage.
