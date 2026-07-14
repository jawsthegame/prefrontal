- **iOS: fix Face ID app lock stranding after the first-launch consent prompt**
  🐛 — with App Lock on, the very first unlock attempt carries iOS's one-time
  "allow Prefrontal to use Face ID?" consent alert, whose evaluation can return
  *without* ever scanning (a system-interruption outcome, not a real cancel).
  The auto-prompt fires only on two one-shot UI edges (`RootView.onAppear`,
  `scenePhase → .active`), so nothing re-armed it: the app sat on a clean
  "Prefrontal is locked" screen — no spinner, no error — with no way forward but
  the manual button, which hit the same round-trip. (Distinct from the #617
  spinner-hang fix.) `BiometricLock.authenticate()` now self-heals: an attempt
  that ends without unlocking through no deliberate user action — the consent
  round-trip, an app-switch interruption, or a denied/locked-out biometry that
  should fall through to the passcode — triggers one automatic retry, bounded to
  once per locked session (refreshed on every re-lock) so a genuine `.userCancel`
  (the manual button stays the retry) or `.authenticationFailed` (shows the error
  instead of re-scanning in a loop) can't loop. The lock's `LocalAuthentication`
  seams are now injectable, so the state machine is covered by a hermetic
  `BiometricLockTests` XCTest suite (consent round-trip heals, cancel/failure
  don't loop, retry is bounded and per-session). Client-only (build on a Mac).
