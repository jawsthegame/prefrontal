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
///
/// **Self-healing prompt.** The auto-prompt is fired from a couple of one-shot
/// UI edges (`RootView.onAppear`, `scenePhase → .active`). Some evaluations end
/// *without* unlocking through no deliberate user action — most importantly the
/// **first-launch Face ID consent round-trip**, whose evaluation returns after
/// the user grants permission without ever scanning. Left alone that strands the
/// lock screen (locked + idle + no error) with no edge left to re-trigger it, so
/// `authenticate()` retries itself once per locked session on those outcomes (see
/// `canAutoRetry`).
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

    /// Runs one policy evaluation and reports `(success, LAError.Code?, message)`.
    /// The completion may arrive on any thread but is always invoked on the main
    /// actor (the production evaluator marshals there; tests call it on main), so
    /// `authenticate` bridges it with `MainActor.assumeIsolated`. Injectable so the
    /// state machine — the unlock/lock/auto-retry logic — is unit-testable without
    /// a device (`LAContext.evaluatePolicy` can't run under XCTest on a simulator).
    typealias Evaluator = (_ reason: String,
                           _ completion: @escaping (_ success: Bool,
                                                    _ code: LAError.Code?,
                                                    _ message: String?) -> Void) -> Void

    private let canAuthenticateNow: () -> Bool
    private let evaluator: Evaluator

    /// One automatic retry, refreshed on every (re-)lock. Reserved for an attempt
    /// that ended without unlocking through no deliberate user action — above all
    /// the first-launch Face ID consent round-trip, whose evaluation returns
    /// without ever scanning and would otherwise strand the lock screen. Bounding
    /// it to a single retry per locked session prevents an endless prompt loop on
    /// a persistently-failing evaluation.
    private var canAutoRetry = true

    /// Production: initial state from the App-Group flag and real biometric /
    /// passcode availability; evaluations drive `LAContext`.
    convenience init() {
        self.init(appLockEnabled: SharedStore.appLockEnabled,
                  canAuthenticate: BiometricLock.deviceCanAuthenticate,
                  evaluator: BiometricLock.evaluateWithLAContext)
    }

    /// Designated initializer. Tests inject the two `LocalAuthentication` seams
    /// (`canAuthenticate`, `evaluator`) to exercise the lock state machine
    /// hermetically.
    init(appLockEnabled: Bool,
         canAuthenticate: @escaping () -> Bool,
         evaluator: @escaping Evaluator) {
        self.canAuthenticateNow = canAuthenticate
        self.evaluator = evaluator
        // Can't reference the `canAuthenticate` computed property before `self` is
        // initialized, so use the injected closure directly.
        isUnlocked = !(appLockEnabled && canAuthenticate())
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
    var canAuthenticate: Bool { canAuthenticateNow() }

    /// The real availability check, inlined here and used both as the production
    /// `canAuthenticate` seam and by `init` for the initial lock state. `nonisolated`
    /// so it fits the plain `() -> Bool` seam (creating an `LAContext` is thread-safe).
    nonisolated static func deviceCanAuthenticate() -> Bool {
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
    /// authenticate locks (with a fresh auto-retry budget).
    func settingChanged(enabled: Bool) {
        if enabled, canAuthenticate {
            isUnlocked = false
            canAutoRetry = true
        } else {
            isUnlocked = true
            lastError = nil
        }
    }

    /// Re-lock on backgrounding, so the next foreground requires a fresh unlock.
    /// A no-op when the lock is off or the device can't authenticate. Refreshes the
    /// auto-retry budget so each locked session can self-heal once.
    func lock(enabled: Bool) {
        guard enabled, canAuthenticate else { isUnlocked = true; return }
        isUnlocked = false
        canAutoRetry = true
    }

    /// Present the biometric (with passcode fallback) prompt. Safe to call
    /// redundantly — it early-returns while already unlocked or mid-prompt.
    func authenticate(reason: String = "Unlock Prefrontal") {
        guard !isUnlocked, !authenticating else { return }
        authenticating = true
        lastError = nil
        evaluator(reason) { [weak self] success, code, message in
            guard let self else { return }
            // The production evaluator delivers on the main actor; the `Evaluator`
            // type can't *enforce* that, so stay defensive. When already on main
            // (production's hop, and every test), run inline — keeping the state
            // updates and the synchronous auto-retry on the same turn; otherwise
            // hop to the main actor rather than crash `assumeIsolated`.
            if Thread.isMainThread {
                MainActor.assumeIsolated {
                    self.finish(reason: reason, success: success, code: code, message: message)
                }
            } else {
                Task { @MainActor in
                    self.finish(reason: reason, success: success, code: code, message: message)
                }
            }
        }
    }

    /// Apply an evaluation's result: unlock on success; otherwise surface the
    /// error (unless it was a cancel) and, for non-deliberate failures, self-heal
    /// once so the lock screen doesn't strand.
    private func finish(reason: String, success: Bool, code: LAError.Code?, message: String?) {
        authenticating = false
        if success {
            isUnlocked = true
            lastError = nil
            canAutoRetry = true
            return
        }
        // Deliberate user actions (cancel, or choosing the passcode fallback) and
        // system interruptions aren't errors worth surfacing; keep real errors.
        let quiet = code == .userCancel || code == .userFallback
            || code == .appCancel || code == .systemCancel
        lastError = quiet ? nil : message
        // Self-heal once from anything that isn't a *deliberate* user action or a
        // genuine biometric mismatch: the first-launch consent round-trip, an
        // app-switch/system interruption, or a denied/locked-out biometry that
        // should fall through to the passcode. A `.userCancel` (the "Unlock" button
        // is the manual retry) or `.userFallback` (the user chose passcode) is
        // respected, and `.authenticationFailed` shows the error rather than
        // immediately re-scanning in a loop.
        let recoverable = code != .userCancel && code != .userFallback
            && code != .authenticationFailed
        if !isUnlocked, canAuthenticate, canAutoRetry, recoverable {
            canAutoRetry = false
            authenticate(reason: reason)
        }
    }

    /// Production evaluator: drives `LAContext.deviceOwnerAuthentication` and
    /// reports the result on the main actor. `nonisolated` so it fits the plain
    /// `Evaluator` seam.
    nonisolated private static func evaluateWithLAContext(reason: String,
                                                          completion: @escaping (Bool, LAError.Code?, String?) -> Void) {
        let ctx = LAContext()
        ctx.localizedFallbackTitle = "Use Passcode"
        ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, error in
            // `ctx` is captured strongly by this reply block, so the context stays
            // alive for the whole evaluation. A local-only reference would be
            // released the moment this function returns, and `LocalAuthentication`
            // cancels an in-flight evaluation when its context deallocates — the
            // prompt never appears and this block never fires (the hang fixed in
            // #617).
            withExtendedLifetime(ctx) {
                let code = (error as? LAError)?.code
                let message = error?.localizedDescription
                // The reply arrives off the main thread; hop to main, where the
                // completion (and the state it drives) must run.
                Task { @MainActor in completion(success, code, message) }
            }
        }
    }
}
