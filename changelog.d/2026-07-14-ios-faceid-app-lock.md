- **iOS: Face ID / Touch ID app lock** ✅ — an opt-in biometric gate over the
  native app. The client holds the bearer token in the Keychain and its tabs show
  personal schedule, todo, and panic data, so a borrowed unlocked phone shouldn't
  expose them. Turn it on in **Me ▸ Settings ▸ App Lock** (only offered when the
  device has enrolled biometrics); it then requires **Face ID** (or Touch ID /
  Optic ID) on launch and whenever the app returns from the background, with the
  device passcode as the built-in fallback so you can't get locked out. A new
  `Security/BiometricLock` (`LocalAuthentication`, app-target only) owns the lock
  state, `RootView` covers the whole surface with a `LockView` while locked — which
  doubles as the app-switcher privacy shield since it also shows whenever the scene
  isn't active — and `scenePhase` drives re-lock on background / re-prompt on
  foreground. Adds `NSFaceIDUsageDescription` and an `appLockEnabled` flag to the
  App-Group config. Client-only (build on a Mac).
