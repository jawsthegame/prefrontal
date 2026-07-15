- **iOS: on-device brain-dump capture (Apple Foundation Models)** ✅ — the native
  client half of roadmap M1's "capture at the speed of thought". A new **Brain-dump**
  App Intent (Action Button / Siri) opens a capture sheet where the user types — or
  taps the keyboard mic to dictate — one rambling stream of thoughts. When the device
  supports it (iOS 26+, model available), the ramble is parsed **on-device** via the
  Foundation Models framework into structured capture actions (todos, commitments,
  shopping, blockers) — private and cheap, no cloud model sees the raw thought — and
  posted to the same `POST /braindump` endpoint as a pre-parsed `parse`, which the
  server re-validates and returns as a **preview**. On older devices (or when the
  on-device model is unavailable), it falls back to sending the raw text for the
  server to parse (the opt-in cloud agent for the hard reasoning); a "server pass"
  button escalates a dump on demand, which also surfaces the behavioral asides the
  on-device pass leaves alone. Nothing is written on capture: actions apply via
  `POST /assistant/apply` and behavioral proposals via `POST /proposals/{id}/accept`,
  each after the user confirms — an on-device parse is untrusted input that can't
  write, so the human-in-the-loop safety model is unchanged. Foundation Models is
  gated exactly like AlarmKit (`#if canImport(FoundationModels)` + `@available(iOS
  26.0, *)`) so the iOS-17 deployment target still compiles and runs. New
  `Capture/BrainDumpParser.swift` (+ `CaptureRouter`), `Views/BrainDumpView.swift`,
  `braindump`/`applyAssistantActions`/`acceptProposal` endpoints, and `JSONValue` /
  brain-dump response models; `PrefrontalTests/BrainDumpTests.swift` covers the
  decode/round-trip. The `Brain-dump` Siri phrase takes the App Shortcuts provider's
  last free slot; **Log Trip** is demoted to a non-provider intent (still available in
  the Shortcuts app / Action Button), like Missed It. Client-only (build on a Mac).
