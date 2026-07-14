import Foundation
import Combine
import LocalAuthentication

/// Opt-in biometric app lock. When enabled (Me ▸ Settings ▸ App Lock), the app
/// gates its whole surface behind **Face ID / Touch ID** on launch and again
/// whenever it returns from the background — the token lives in the Keychain and
/// the tabs show personal schedule/todo/panic data, so a borrowed unlocked phone
/// shouldn't expose them.
///
/// App-target only: it imports `LocalAuthentication` and lives under `Security/`,
/// which the widget extension doesn't compile (the widget pulls only
/// `Config/Networking/Models/Theme`).
///
/// **Two policies, on purpose.** `isAvailable` (biometrics enrolled) gates only
/// whether the *Settings toggle* is offered — you set up "Require Face ID" when
/// Face ID works. Every *runtime* decision (initial lock, re-lock, overlay,
/// prompt) is gated on `canAuthenticate`, which uses `.deviceOwnerAuthentication`
/// (biometrics **or** device passcode). That matters during **biometry lockout**
/// (too many failed scans): biometrics become unevaluatable, but the passcode
/// fallback can still unlock — so the gate must stay engaged, not disappear.
@MainActor
final class BiometricLock: ObservableObject {
    static let shared = BiometricLock()

    /// `true` when the gate is satisfied (or not required). `RootView` reveals the
    /// app only when this is `true`. Starts locked at init if the lock is enabled
    /// and the device can authenticate, so no content renders before the first
    /// prompt.
    @Published private(set) var isUnlocked: Bool
    /// `true` while a system biometric/passcode prompt is in flight — guards
    /// against presenting two prompts at once.
    @Published private(set) var authenticating = false
    /// Last failure message, surfaced on the lock screen so a cancelled or failed
    /// attempt explains itself and offers a retry.
    @Published private(set) var lastError: String?

    /// Holds the `LAContext` for the lifetime of an in-flight `evaluatePolicy`
    /// call. A local context is deallocated as soon as `authenticate()` returns,
    /// and `LocalAuthentication` invalidates the evaluation when its context dies
    /// — the system prompt never appears and the reply block never fires, so
    /// `authenticating` sticks at `true` (the lock screen spinner spins forever
    /// and the button stays disabled). Kept alive here until the callback returns.
    private var authContext: LAContext?

    private init() {
        let enabled = SharedStore.appLockEnabled
        // Can't reference instance members before `self` is initialized, so inline
        // the same `.deviceOwnerAuthentication` check `canAuthenticate` uses.
        var err: NSError?
        let canAuth = LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: &err)
        isUnlocked = !(enabled && canAuth)
    }

    /// The device's enrolled biometry, or `.none` when biometrics can't currently
    /// be evaluated (unenrolled, or locked out). A fresh `LAContext` each call
    /// reflects current state.
    var biometryType: LABiometryType {
        let ctx = LAContext()
        var err: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) else { return .none }
        return ctx.biometryType
    }

    /// Whether biometrics are enrolled and usable — gates *offering* the toggle.
    var isAvailable: Bool {
        switch biometryType {
        case .faceID, .touchID, .opticID: return true
        default: return false
        }
    }

    /// Whether the device can authenticate at all — biometrics **or** passcode.
    /// This, not `isAvailable`, gates every runtime lock decision so a biometry
    /// lockout falls through to the passcode prompt instead of disengaging the
    /// lock. False only when no passcode is set (then nothing can gate the app).
    var canAuthenticate: Bool {
        var err: NSError?
        return LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: &err)
    }

    /// Label for what a tap will actually use: the enrolled biometry when usable,
    /// else "Passcode" — because during lockout `.deviceOwnerAuthentication` prompts
    /// for the passcode, so "Unlock with Face ID" would be a lie.
    var biometryName: String {
        switch biometryType {
        case .faceID:  return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        default:       return "Passcode"
        }
    }

    /// SF Symbol matching what a tap will use: the biometry glyph when usable, else
    /// a generic lock (the prompt will be the passcode, not a biometric scan).
    var symbolName: String {
        switch biometryType {
        case .faceID:  return "faceid"
        case .touchID: return "touchid"
        case .opticID: return "opticid"
        default:       return "lock.fill"
        }
    }

    /// Reconcile the lock state after the toggle changes: disabling (or a device
    /// that can't authenticate at all) unlocks; enabling while the device can
    /// authenticate locks.
    func settingChanged(enabled: Bool) {
        if enabled, canAuthenticate {
            isUnlocked = false
        } else {
            isUnlocked = true
            lastError = nil
        }
    }

    /// Re-lock on backgrounding, so the next foreground requires a fresh unlock.
    /// A no-op when the lock is off or the device can't authenticate.
    func lock(enabled: Bool) {
        guard enabled, canAuthenticate else { isUnlocked = true; return }
        isUnlocked = false
    }

    /// Present the biometric (with passcode fallback) prompt. Safe to call
    /// redundantly — it early-returns while already unlocked or mid-prompt.
    func authenticate(reason: String = "Unlock Prefrontal") {
        guard !isUnlocked, !authenticating else { return }
        authenticating = true
        lastError = nil
        let ctx = LAContext()
        // Retain the context until the reply block runs; a local-only reference
        // would be released the moment this method returns, cancelling the
        // evaluation before the prompt ever appears.
        authContext = ctx
        ctx.localizedFallbackTitle = "Use Passcode"
        ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { [weak self] success, error in
            Task { @MainActor in
                guard let self else { return }
                self.authContext = nil
                self.authenticating = false
                if success {
                    self.isUnlocked = true
                    self.lastError = nil
                } else {
                    // Nil out the user-cancelled case's noisy message; keep real errors.
                    let code = (error as? LAError)?.code
                    self.lastError = (code == .userCancel || code == .appCancel || code == .systemCancel)
                        ? nil
                        : error?.localizedDescription
                }
            }
        }
    }
}
