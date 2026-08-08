- **iOS: fix occasionally landing on Setup while logged in** ✅ — `AppConfig.token`
  and `OnboardingModel.active` were each read/derived exactly once at process
  launch and never reconciled. If that first Keychain read raced a not-yet-readable
  item — the app spun up in the background while the device was still locked (a
  location event or background push before the first unlock after a reboot; the
  token is stored `AfterFirstUnlock`) — the token latched empty, so `isConfigured`
  read false and the walkthrough showed even for a logged-in user. On foreground
  the app now re-reads the token (`AppConfig.refreshFromStore`) and dismisses a
  walkthrough it is only showing because that first read came back empty
  (`OnboardingModel.reconcileConfigured`) — a genuine new user (no token) and a
  deep-link re-onboard (past the welcome step, payload pending) are left untouched.
