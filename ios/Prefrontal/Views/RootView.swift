import SwiftUI
import UIKit

struct RootView: View {
    @EnvironmentObject var config: AppConfig
    @EnvironmentObject var onboarding: OnboardingModel
    @StateObject private var lock = BiometricLock.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if onboarding.active {
                OnboardingView()
            } else {
                MainTabs()
            }
        }
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
        .onAppear { scheduleUnlockPrompt() }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:     scheduleUnlockPrompt()
            case .background: lock.lock(enabled: config.appLockEnabled)
            default:          break
            }
        }
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
