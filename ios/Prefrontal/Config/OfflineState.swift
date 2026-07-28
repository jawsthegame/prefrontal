import Foundation
import Combine

/// Main-actor mirror of `ConnectionStore` for SwiftUI. Views observe it to show
/// the "offline / last synced" banner; it republishes whenever connectivity
/// changes (via `.prefrontalConnectionChanged`) and can be refreshed on demand
/// (e.g. on foreground, or after a Today reload).
///
/// App-target concept, but it lives here in `Config/` alongside the store it
/// mirrors; the widget compiles it harmlessly and simply never instantiates it.
@MainActor
final class OfflineState: ObservableObject {
    static let shared = OfflineState()

    /// True while reads are being served from the offline cache.
    @Published private(set) var isOffline: Bool
    /// When the last successful sync landed (drives "showing data from …").
    @Published private(set) var lastSyncedAt: Date?

    private var cancellable: AnyCancellable?

    private init() {
        isOffline = ConnectionStore.isOffline
        lastSyncedAt = ConnectionStore.lastSyncedAt
        cancellable = NotificationCenter.default
            .publisher(for: .prefrontalConnectionChanged)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.refresh() }
    }

    /// Pull the latest connectivity snapshot. Cheap and idempotent — only
    /// publishes when a value actually changed, so it's safe to call often.
    func refresh() {
        let off = ConnectionStore.isOffline
        let at = ConnectionStore.lastSyncedAt
        if off != isOffline { isOffline = off }
        if at != lastSyncedAt { lastSyncedAt = at }
    }
}
