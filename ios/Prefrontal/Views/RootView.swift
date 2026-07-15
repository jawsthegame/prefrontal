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
            scheduleCapturePrompt()
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:     scheduleUnlockPrompt(); scheduleCapturePrompt()
            case .background: lock.lock(enabled: config.appLockEnabled)
            default:          break
            }
        }
    }

    /// Re-check for a pending capture request a beat after the scene settles. On a
    /// cold launch or a background→active transition the flag can be set — or
    /// `applicationState` flip to `.active` — a moment after `onAppear`/`onChange`
    /// fires, so a synchronous check would miss it. The warm paths
    /// (`.prefrontalOpenCapture`, unlock) call `tryPresentCapture()` directly.
    private func scheduleCapturePrompt() {
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            tryPresentCapture()
        }
    }

    /// Present the quick-capture sheet if a fresh request is pending.
    ///
    /// Ordering matters: while the app isn't foregrounded, or is locked, the request
    /// is *left pending* (not consumed) — presenting a sheet off `.active` trips
    /// SwiftUI transition warnings, and a locked launch has nothing visible to show
    /// over — so a later `.active` / `isUnlocked` change retries. Once we can act we
    /// consume the flag *unconditionally* (even if the sheet is already up, or we're
    /// still onboarding), so a one-shot "capture now" tap can't linger in the
    /// App-Group flag and re-pop the sheet on some unrelated trigger later.
    private func tryPresentCapture() {
        // `applicationState` is the live value; the captured `scenePhase` can be
        // stale inside these callbacks (same reason `scheduleUnlockPrompt` reads it).
        guard UIApplication.shared.applicationState == .active else { return }
        if config.appLockEnabled, lock.canAuthenticate, !lock.isUnlocked { return }
        guard SharedStore.consumeCaptureRequest() else { return }
        guard !showCapture, !onboarding.active else { return }
        showCapture = true
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
