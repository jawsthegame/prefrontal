import SwiftUI
import UIKit

struct RootView: View {
    @EnvironmentObject var config: AppConfig
    @EnvironmentObject var onboarding: OnboardingModel
    @StateObject private var lock = BiometricLock.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var showCapture = false

    var body: some View {
        Group {
            if onboarding.active {
                OnboardingView()
            } else {
                MainTabs()
            }
        }
        // Quick-capture, opened from the interactive-widget button, the Control
        // Center control, or a `prefrontal://capture` deep link. All funnel through
        // `SharedStore.requestCapture()`, which posts `.prefrontalOpenCapture` (warm
        // app) and sets a flag `tryPresentCapture()` consumes (cold launch).
        .sheet(isPresented: $showCapture) { CaptureThoughtView() }
        .onReceive(NotificationCenter.default.publisher(for: .prefrontalOpenCapture)) { _ in
            tryPresentCapture()
        }
        // Re-check once biometrics clear — a request that arrived while locked is
        // held (not consumed) until the content is actually visible.
        .onChange(of: lock.isUnlocked) { _, _ in tryPresentCapture() }
        // The lock cover shows whenever the gate isn't satisfied, and also while
        // the scene isn't active — so backgrounding hides content in the app
        // switcher snapshot without triggering a re-auth (only `.active` prompts).
        // Gated on `canAuthenticate` (biometrics OR passcode), so a biometry
        // lockout still shows the cover and prompts for passcode rather than
        // silently exposing content.
        .overlay {
            if config.appLockEnabled, lock.canAuthenticate, (!lock.isUnlocked || scenePhase != .active) {
                LockView().transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: lock.isUnlocked)
        // Cold-launch auto-prompt: `onChange` doesn't fire for the initial phase,
        // so kick the first unlock here (idempotent — `authenticate()` guards).
        .onAppear {
            scheduleUnlockPrompt()
            tryPresentCapture()
            // Cold launch from the capture control: `perform()` may set the flag a
            // beat after `onAppear`, so re-check once the launch settles.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 500_000_000)
                tryPresentCapture()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:     scheduleUnlockPrompt(); tryPresentCapture()
            case .background: lock.lock(enabled: config.appLockEnabled)
            default:          break
            }
        }
    }

    /// Present the quick-capture sheet if a fresh request is pending — but not over
    /// the lock screen, and not before the app is connected. Consuming the flag is
    /// deferred (left pending) while locked, so `onChange(lock.isUnlocked)` picks it
    /// up once the content is visible.
    private func tryPresentCapture() {
        guard !showCapture, !onboarding.active else { return }
        if config.appLockEnabled, lock.canAuthenticate, !lock.isUnlocked { return }
        if SharedStore.consumeCaptureRequest() { showCapture = true }
    }

    /// Present the biometric prompt a beat after the scene settles rather than
    /// synchronously during launch / a scene transition. Presenting the system
    /// Face ID UI mid-transition can leave the scan UI unpresented (consent shows,
    /// then no scan animation) with the evaluation wedged — deferring lets the
    /// window become key first. Idempotent: `authenticate()` guards on state.
    private func scheduleUnlockPrompt() {
        guard config.appLockEnabled else { return }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            // Re-check after the delay: only prompt while the app is actually in the
            // foreground. If it backgrounded during the wait, presenting the system
            // biometric UI now would just bounce back `.appCancel` / `.systemCancel`;
            // the next `.active` transition re-arms this. (`applicationState` is the
            // live value — the captured `scenePhase` would be stale here.)
            guard config.appLockEnabled,
                  UIApplication.shared.applicationState == .active else { return }
            lock.authenticate()
        }
    }
}

struct MainTabs: View {
    @State private var showPanic = false

    var body: some View {
        TabView {
            NavigationStack { TodayView(showPanic: $showPanic) }
                .tabItem { Label("Today", systemImage: "sun.max") }
            NavigationStack { TodosView() }
                .tabItem { Label("Todos", systemImage: "checklist") }
            NavigationStack { MailView() }
                .tabItem { Label("Mail", systemImage: "envelope") }
            NavigationStack { CalendarView() }
                .tabItem { Label("Calendar", systemImage: "calendar") }
            NavigationStack { MeView() }
                .tabItem { Label("Me", systemImage: "person.crop.circle") }
        }
        .sheet(isPresented: $showPanic) { PanicView() }
    }
}
