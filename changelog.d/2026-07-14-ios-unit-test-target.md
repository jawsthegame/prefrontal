- **iOS unit-test target** ✅ (closes #602) — completes the iOS verification
  story begun by the lint/CI gate. Added a `PrefrontalTests` XCTest target (wired
  in `project.yml`, with a committed `Prefrontal` scheme so it runs headless) and
  a first hermetic suite for `ConnectPayload` — the `prefrontal://connect` QR
  deep-link parser (scheme/host validation, bare-host→https normalization,
  trailing-slash trim, optional token/hints, round-trip). The `ios` CI workflow
  now runs `xcodebuild test` on a simulator (picking an available iPhone by UDID)
  alongside the existing SwiftLint + `swiftc -typecheck` gates. `APIClient`
  request-building and the offline queue are the natural next tests (they need a
  small testable init / URLProtocol stub on `APIClient`).
