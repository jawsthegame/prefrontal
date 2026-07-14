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
/// `Config/Networking/Models/Theme`). Authentication uses
/// `.deviceOwnerAuthentication`, so a user who fails biometrics still has the
/// device-passcode fallback and never gets locked out.
@MainActor
final class BiometricLock: ObservableObject {
    static let shared = BiometricLock()

    /// `true` when the gate is satisfied (or not required). `RootView` reveals the
    /// app only when this is `true`. Starts locked at init if the lock is enabled
    /// and the device can evaluate biometrics, so no content renders before the
    /// first prompt.
    @Published private(set) var isUnlocked: Bool
    /// `true` while a system biometric/passcode prompt is in flight — guards
    /// against presenting two prompts at once.
    @Published private(set) var authenticating = false
    /// Last failure message, surfaced on the lock screen so a cancelled or failed
    /// attempt explains itself and offers a retry.
    @Published private(set) var lastError: String?

    private init() {
        let enabled = SharedStore.appLockEnabled
        // Can't reference `isAvailable` before `self` is initialized, so inline the
        // same capability check here.
        var err: NSError?
        let available = LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err)
        isUnlocked = !(enabled && available)
    }

    /// The device's enrolled biometry, or `.none` when biometrics are unavailable
    /// or not enrolled. A fresh `LAContext` each call reflects current enrollment.
    var biometryType: LABiometryType {
        let ctx = LAContext()
        var err: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) else { return .none }
        return ctx.biometryType
    }

    /// Whether the lock can actually run here — the toggle is only offered when so.
    var isAvailable: Bool {
        switch biometryType {
        case .faceID, .touchID, .opticID: return true
        default: return false
        }
    }

    /// Human label for the enrolled biometry, for UI copy ("Require Face ID").
    var biometryName: String {
        switch biometryType {
        case .faceID:  return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        default:       return "biometrics"
        }
    }

    /// SF Symbol matching the enrolled biometry, for the lock screen glyph.
    var symbolName: String {
        switch biometryType {
        case .touchID: return "touchid"
        case .opticID: return "opticid"
        default:       return "faceid"
        }
    }

    /// Reconcile the lock state after the toggle changes: disabling (or losing
    /// biometrics) unlocks immediately; enabling with biometrics available locks.
    func settingChanged(enabled: Bool) {
        if enabled, isAvailable {
            isUnlocked = false
        } else {
            isUnlocked = true
            lastError = nil
        }
    }

    /// Re-lock on backgrounding, so the next foreground requires a fresh unlock.
    /// A no-op when the lock is off or unavailable.
    func lock(enabled: Bool) {
        guard enabled, isAvailable else { isUnlocked = true; return }
        isUnlocked = false
    }

    /// Present the biometric (with passcode fallback) prompt. Safe to call
    /// redundantly — it early-returns while already unlocked or mid-prompt.
    func authenticate(reason: String = "Unlock Prefrontal") {
        guard !isUnlocked, !authenticating else { return }
        authenticating = true
        lastError = nil
        let ctx = LAContext()
        ctx.localizedFallbackTitle = "Use Passcode"
        ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { [weak self] success, error in
            Task { @MainActor in
                guard let self else { return }
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
