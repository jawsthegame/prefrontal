import XCTest
import LocalAuthentication

@testable import Prefrontal

/// Unit tests for `BiometricLock`'s state machine — initial lock state, the
/// unlock/lock transitions, and (the reason this file exists) the auto-retry that
/// recovers an evaluation which ended without unlocking through no deliberate user
/// action. The real `LocalAuthentication` seams (`canAuthenticate`, `evaluator`)
/// are injected via the designated initializer so none of this touches a device.
@MainActor
final class BiometricLockTests: XCTestCase {

    /// Scriptable stand-in for `LAContext.evaluatePolicy`. Queued results fire
    /// synchronously (on the main actor, as production guarantees); with none
    /// queued the completion is parked for `fire(...)` so a mid-prompt state can
    /// be inspected. Not `@MainActor` so its method type matches the plain
    /// `Evaluator` typealias; the test drives it on the main actor only.
    final class FakeEvaluator {
        private(set) var calls = 0
        var scripted: [(Bool, LAError.Code?, String?)] = []
        private var pending: ((Bool, LAError.Code?, String?) -> Void)?

        func evaluate(reason: String,
                      completion: @escaping (Bool, LAError.Code?, String?) -> Void) {
            calls += 1
            if scripted.isEmpty {
                pending = completion
            } else {
                let r = scripted.removeFirst()
                completion(r.0, r.1, r.2)
            }
        }

        func fire(_ success: Bool, _ code: LAError.Code? = nil, _ message: String? = nil) {
            let c = pending
            pending = nil
            c?(success, code, message)
        }
    }

    private func makeLock(enabled: Bool = true,
                          canAuthenticate: Bool = true,
                          fake: FakeEvaluator) -> BiometricLock {
        BiometricLock(appLockEnabled: enabled,
                      canAuthenticate: { canAuthenticate },
                      evaluator: fake.evaluate)
    }

    // MARK: initial state

    func testStartsLockedWhenEnabledAndCanAuthenticate() {
        let lock = makeLock(enabled: true, canAuthenticate: true, fake: FakeEvaluator())
        XCTAssertFalse(lock.isUnlocked)
    }

    func testStartsUnlockedWhenDisabled() {
        let lock = makeLock(enabled: false, canAuthenticate: true, fake: FakeEvaluator())
        XCTAssertTrue(lock.isUnlocked)
    }

    func testStartsUnlockedWhenDeviceCannotAuthenticate() {
        // No passcode set → nothing can gate the app, so it must not lock a user out.
        let lock = makeLock(enabled: true, canAuthenticate: false, fake: FakeEvaluator())
        XCTAssertTrue(lock.isUnlocked)
    }

    // MARK: happy path

    func testUnlocksOnSuccess() {
        let fake = FakeEvaluator()
        fake.scripted = [(true, nil, nil)]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertTrue(lock.isUnlocked)
        XCTAssertFalse(lock.authenticating)
        XCTAssertNil(lock.lastError)
        XCTAssertEqual(fake.calls, 1)
    }

    // MARK: the regression — first-launch consent round-trip

    /// The bug from the screenshots: the initial evaluation carries the OS Face ID
    /// consent alert and returns *without* scanning (a system-cancel here), which
    /// used to strand the lock screen. It must now self-heal: retry once, scan,
    /// unlock.
    func testConsentRoundTripSelfHealsAndUnlocks() {
        let fake = FakeEvaluator()
        fake.scripted = [(false, .systemCancel, nil), (true, nil, nil)]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 2, "should retry the interrupted consent evaluation")
        XCTAssertTrue(lock.isUnlocked)
        XCTAssertFalse(lock.authenticating)
        XCTAssertNil(lock.lastError)
    }

    /// Denying Face ID consent surfaces as `biometryNotAvailable`; the retry falls
    /// through to the passcode (also modelled here as an eventual success).
    func testConsentDenialFallsThroughToPasscode() {
        let fake = FakeEvaluator()
        fake.scripted = [(false, .biometryNotAvailable, "Biometry is not available."),
                         (true, nil, nil)]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 2)
        XCTAssertTrue(lock.isUnlocked)
        XCTAssertNil(lock.lastError)
    }

    // MARK: deliberate cancel / hard failure must NOT loop

    func testUserCancelDoesNotRetryAndClearsError() {
        let fake = FakeEvaluator()
        fake.scripted = [(false, .userCancel, "Canceled by user.")]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 1, "a deliberate cancel leaves the manual button, no auto-retry")
        XCTAssertFalse(lock.isUnlocked)
        XCTAssertFalse(lock.authenticating)
        XCTAssertNil(lock.lastError, "a user cancel isn't surfaced as an error")
    }

    func testUserFallbackDoesNotRetry() {
        // Choosing "Use Passcode" is a deliberate action, not an interruption.
        let fake = FakeEvaluator()
        fake.scripted = [(false, .userFallback, "Fallback selected.")]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 1, "an explicit fallback choice shouldn't re-prompt")
        XCTAssertFalse(lock.isUnlocked)
        XCTAssertNil(lock.lastError, "fallback isn't surfaced as an error")
    }

    func testAuthenticationFailureSurfacesErrorWithoutRetry() {
        let fake = FakeEvaluator()
        fake.scripted = [(false, .authenticationFailed, "Face not recognized.")]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 1, "a real mismatch shouldn't immediately re-scan in a loop")
        XCTAssertFalse(lock.isUnlocked)
        XCTAssertEqual(lock.lastError, "Face not recognized.")
    }

    // MARK: retry is bounded, and refreshed per locked session

    func testAutoRetryIsBoundedToOnePerSession() {
        let fake = FakeEvaluator()
        // Three interruptions in a row: exactly one retry should follow the first.
        fake.scripted = [(false, .systemCancel, nil),
                         (false, .systemCancel, nil),
                         (false, .systemCancel, nil)]
        let lock = makeLock(fake: fake)

        lock.authenticate()

        XCTAssertEqual(fake.calls, 2, "at most one automatic retry per locked session")
        XCTAssertFalse(lock.isUnlocked)
    }

    func testReLockRefreshesTheRetryBudget() {
        let fake = FakeEvaluator()
        fake.scripted = [(false, .systemCancel, nil), (false, .systemCancel, nil)]
        let lock = makeLock(fake: fake)

        lock.authenticate()
        XCTAssertEqual(fake.calls, 2)   // budget spent
        XCTAssertFalse(lock.isUnlocked)

        // A background re-lock refreshes the budget so the next session heals again.
        lock.lock(enabled: true)
        fake.scripted = [(false, .systemCancel, nil), (true, nil, nil)]
        lock.authenticate()

        XCTAssertEqual(fake.calls, 4)
        XCTAssertTrue(lock.isUnlocked)
    }

    // MARK: concurrency guard

    func testGuardPreventsOverlappingPrompts() {
        let fake = FakeEvaluator()   // nothing scripted → the first prompt parks
        let lock = makeLock(fake: fake)

        lock.authenticate()
        XCTAssertTrue(lock.authenticating)
        XCTAssertEqual(fake.calls, 1)

        lock.authenticate()   // redundant call while mid-prompt
        XCTAssertEqual(fake.calls, 1, "a second prompt must not stack on the first")

        fake.fire(true)
        XCTAssertTrue(lock.isUnlocked)
        XCTAssertFalse(lock.authenticating)
    }

    // MARK: toggle + background transitions

    func testSettingChangedLocksAndUnlocks() {
        let lock = makeLock(enabled: false, canAuthenticate: true, fake: FakeEvaluator())
        XCTAssertTrue(lock.isUnlocked)

        lock.settingChanged(enabled: true)
        XCTAssertFalse(lock.isUnlocked)

        lock.settingChanged(enabled: false)
        XCTAssertTrue(lock.isUnlocked)
    }

    func testLockOnBackgroundEngagesTheGate() {
        let fake = FakeEvaluator()
        fake.scripted = [(true, nil, nil)]
        let lock = makeLock(fake: fake)
        lock.authenticate()
        XCTAssertTrue(lock.isUnlocked)

        lock.lock(enabled: true)
        XCTAssertFalse(lock.isUnlocked)
    }

    func testLockIsNoOpWhenDeviceCannotAuthenticate() {
        let lock = makeLock(enabled: true, canAuthenticate: false, fake: FakeEvaluator())
        lock.lock(enabled: true)
        XCTAssertTrue(lock.isUnlocked, "can't gate a device with no passcode/biometrics")
    }
}
