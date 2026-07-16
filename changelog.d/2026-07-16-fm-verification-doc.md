- **On-device brain-dump: a device-verification plan** ✅ — the Foundation Models
  parse (roadmap M1) is the one capture path CI and the simulator can't exercise
  (no system model there), so the unit tests only cover its wire/validation layer,
  never the model itself. New [`docs/foundation-models-verification.md`](docs/foundation-models-verification.md)
  is the manual pass that closes that gap: preconditions (real iOS 26 device, Apple
  Intelligence on), a code-grounded input→expected matrix (multi-item rambles,
  each if-then cue type, an invalid time-window that must drop locally, a
  behavioral episode, a settings-change the on-device pass leaves to the cloud), an
  edge/branch checklist (availability off → server fallback, guardrail refusal,
  the "server pass" escalation, the privacy claim that raw text never leaves the
  device), and how to observe each — including using the new `capture_funnel`
  stat as the end-to-end signal that the on-device path actually ran. Linked from
  the iOS README's Tests section. Docs only.
