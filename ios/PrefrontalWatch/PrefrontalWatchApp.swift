import SwiftUI

/// The Prefrontal Apple Watch companion. A thin, glanceable client that relays
/// every request through the paired iPhone (see `WatchProtocol.swift`) — so it
/// holds no token and makes no network calls of its own.
@main
struct PrefrontalWatchApp: App {
    @StateObject private var link = WatchConnectivityClient.shared

    init() {
        WatchConnectivityClient.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            WatchRootView()
                .environmentObject(link)
                .tint(WatchBrand.accent)
        }
    }
}
