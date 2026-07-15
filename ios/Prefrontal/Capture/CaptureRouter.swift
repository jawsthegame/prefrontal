import Foundation
import Combine

/// Routes a "start a brain-dump" request from outside the UI (the Action Button /
/// Siri App Intent, or a `prefrontal://braindump` deep link) into presenting the
/// capture sheet.
///
/// An App Intent runs in a *separate process* from the app, so it can't set an
/// in-memory flag the app would see. Instead the intent (which launches the app,
/// `openAppWhenRun == true`) leaves a one-shot flag in the shared App Group
/// `UserDefaults`; the app drains it when it next becomes active (see
/// `PrefrontalApp`), flipping `showBrainDump` so `RootView` presents the sheet.
/// The deep-link path sets `showBrainDump` directly, in-process.
@MainActor
final class CaptureRouter: ObservableObject {
    static let shared = CaptureRouter()

    /// Bound to a `.sheet(isPresented:)` in `RootView`.
    @Published var showBrainDump = false

    private init() {}

    /// Request the brain-dump sheet directly (in-process — the deep-link path).
    func requestBrainDump() { showBrainDump = true }

    /// Consume a pending request left by an App Intent — call when the app becomes
    /// active. Presents the sheet exactly once, clearing the flag so it doesn't
    /// re-fire on the next foreground. The intent sets the flag via
    /// `SharedStore.pendingBrainDumpKey` (it can't reach this app-only type).
    func consumePendingBrainDump() {
        guard SharedStore.defaults.bool(forKey: SharedStore.pendingBrainDumpKey) else { return }
        SharedStore.defaults.removeObject(forKey: SharedStore.pendingBrainDumpKey)
        showBrainDump = true
    }
}
