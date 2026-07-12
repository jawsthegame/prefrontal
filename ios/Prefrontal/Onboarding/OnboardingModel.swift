import SwiftUI
import UIKit
import UserNotifications

/// Drives the first-run flow and owns the seam between it and the rest of the
/// app. `active` decides whether `RootView` shows the walkthrough or the main
/// tabs — deliberately *separate* from `AppConfig.isConfigured` so the flow can
/// keep running past the connect step (which flips `isConfigured` true) into
/// the notifications + done steps, and so a deep link can re-open it on an
/// already-configured phone.
@MainActor
final class OnboardingModel: ObservableObject {
    static let shared = OnboardingModel()

    enum Step: Int, CaseIterable { case welcome, connect, notifications, done }

    @Published var active: Bool
    @Published var step: Step = .welcome

    /// A `prefrontal://connect` payload that arrived via deep link, waiting for
    /// the connect step to pick it up and prefill/auto-validate.
    @Published var incoming: ConnectPayload?

    private init() {
        // Fresh install (no token yet) → walk the flow. Returning users skip it.
        active = !AppConfig.shared.isConfigured
    }

    /// Route an opened/scanned connect link. On a not-yet-configured phone this
    /// just seeds the flow; on a configured one it re-opens onboarding at the
    /// connect step so the user can confirm the switch.
    func receive(_ payload: ConnectPayload) {
        incoming = payload
        if !active {
            active = true
            step = .connect
        } else if step == .welcome {
            step = .connect
        }
    }

    func advance() {
        if let next = Step(rawValue: step.rawValue + 1) { step = next }
    }

    func finish() {
        active = false
        step = .welcome
        incoming = nil
    }

    /// Ask iOS for notification authorization. The heavy lifting today is done
    /// by ntfy, but requesting here means native alerts (background refresh,
    /// action buttons — see the ROADMAP) light up without a second prompt later.
    /// Returns whether the user granted it; a denial is not an error.
    func requestNotifications() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        let granted: Bool
        if settings.authorizationStatus == .notDetermined {
            granted = (try? await center.requestAuthorization(options: [.alert, .badge, .sound])) ?? false
        } else {
            granted = settings.authorizationStatus == .authorized
        }
        // Register for APNs so the server can deliver native push (the device
        // token flows back through AppDelegate → POST /route/apns-token).
        if granted { UIApplication.shared.registerForRemoteNotifications() }
        return granted
    }
}
