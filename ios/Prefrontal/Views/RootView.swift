import SwiftUI

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
        .onAppear { if config.appLockEnabled { lock.authenticate() } }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:     if config.appLockEnabled { lock.authenticate() }
            case .background: lock.lock(enabled: config.appLockEnabled)
            default:          break
            }
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
