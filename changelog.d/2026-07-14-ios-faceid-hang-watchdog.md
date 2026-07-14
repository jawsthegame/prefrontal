- **iOS: stop the Face ID app lock from stranding on the consent prompt** 🐛 —
  follow-up to the consent-strand fix. On some devices the first-launch flow
  ("allow Prefrontal to use Face ID?" → Allow → *no scan animation*) leaves the
  evaluation's callback never firing, so `authenticating` stuck at `true` forever:
  the "Unlock" button stayed disabled with no error, and only deleting the app
  recovered. Three changes: (1) a **wedge watchdog** — if an evaluation doesn't
  call back within a timeout, the lock resets so the screen always offers a working
  retry (no more uninstall-to-recover); (2) the prompt is now **deferred a beat
  after the scene becomes active** instead of presented during launch / a scene
  transition, which is the likely cause of the scan UI never appearing; and (3) the
  auto-retry is **scheduled off the callback** rather than re-entered synchronously
  (starting a fresh evaluation from inside the previous one's reply can wedge the
  biometric UI). Adds `[AppLock] …` console breadcrumbs so an on-device Xcode run
  shows exactly what `LocalAuthentication` returns. The `BiometricLock` seams stay
  injectable and the `BiometricLockTests` suite runs deterministically (synchronous
  retry, watchdog disabled). Client-only (build on a Mac).
