- **iOS: require iOS 26, make on-device brain-dump parsing a hard dependency** —
  the app's deployment target moves from iOS 17 to **iOS 26**, so the brain-dump
  capture parses on-device via Apple Foundation Models **unconditionally** rather
  than behind availability gates. `BrainDumpParser` drops the
  `#if canImport(FoundationModels)` / `@available(iOS 26.0, *)` gymnastics and
  imports the framework directly; it still checks `SystemLanguageModel` availability
  at runtime (a device can be on iOS 26 with Apple Intelligence off, unsupported,
  or the model still downloading) and returns `nil` in that case, so the server
  text-parse fallback and the graceful "no on-device model" path are unchanged.
  Bumps the `project.yml` iOS deployment target and the CI `swiftc -typecheck`
  targets (app + widget) to `arm64-apple-ios26.0`, and the `ios/README.md`
  sanity-check command to match. The watchOS companion (watchOS 10) is unaffected.
  Trade-off: drops support for iOS 17–25 across the whole app. (AlarmKit's existing
  iOS-26 gating is now redundant-but-harmless — left for a separate tidy-up.)
  Client-only (build on a Mac).
