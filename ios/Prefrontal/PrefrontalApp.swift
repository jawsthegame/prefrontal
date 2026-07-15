import SwiftUI
import BackgroundTasks
import WidgetKit

@main
struct PrefrontalApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var config = AppConfig.shared
    @StateObject private var onboarding = OnboardingModel.shared
    @Environment(\.scenePhase) private var scenePhase

    /// Must match `BGTaskSchedulerPermittedIdentifiers` in the app's Info.plist.
    static let refreshTaskID = "com.morningstatic.prefrontal.refresh"

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(config)
                .environmentObject(onboarding)
                .tint(Brand.accent)
                .onOpenURL { url in
                    // A `prefrontal://connect?…` link (scanned QR or tapped in a
                    // setup sheet) routes into the onboarding flow.
                    if let payload = ConnectPayload(url: url) {
                        onboarding.receive(payload)
                    } else if url.scheme == "prefrontal", url.host == "capture" {
                        // `prefrontal://capture` — pop the quick-capture sheet
                        // (RootView listens for the signal). The widget/Control
                        // Center surfaces open capture via `OpenThoughtCaptureIntent`;
                        // this deep link is the same landing for a user Shortcut or
                        // Home Screen icon.
                        SharedStore.requestCapture()
                    } else if url.host == "braindump" {
                        // `prefrontal://braindump` — open the brain-dump capture sheet.
                        CaptureRouter.shared.requestBrainDump()
                    }
                }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                Task { await Self.flushQueue() }   // reconnect → drain captures
                // Keep the watch's connected-state fresh (config may have changed
                // while backgrounded, e.g. just after onboarding).
                PhoneWatchConnectivity.shared.pushStatus()
                // An Action Button / Siri brain-dump launch left a one-shot flag in
                // the App Group (it ran in another process); present the sheet now.
                CaptureRouter.shared.consumePendingBrainDump()
            case .background:  Self.scheduleAppRefresh()
            default:           break
            }
        }
        // Opportunistic background drain + widget refresh (iOS 16+). The system
        // decides when to run it; we just reschedule the next one each time.
        .backgroundTask(.appRefresh(Self.refreshTaskID)) {
            await Self.flushQueue()
            Self.scheduleAppRefresh()
        }
    }

    private static func flushQueue() async {
        let flushed = await OfflineQueue.flush()
        if flushed > 0 {
            await MainActor.run { WidgetCenter.shared.reloadAllTimelines() }
        }
    }

    private static func scheduleAppRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}
