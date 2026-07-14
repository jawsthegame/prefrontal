import SwiftUI

/// Top-level watch UI: a vertically-paging `TabView` across Today, Self-care,
/// and Actions. When the phone reports it isn't connected yet, everything is
/// replaced by a single "set up on your phone" prompt (the watch can't onboard
/// on its own — the token lives on the phone).
struct WatchRootView: View {
    @EnvironmentObject private var link: WatchConnectivityClient
    @StateObject private var model = WatchModel()

    var body: some View {
        Group {
            if link.status.connected {
                TabView {
                    WatchTodayView()
                    WatchSelfCareView()
                    WatchActionsView()
                }
                .tabViewStyle(.verticalPage)
            } else {
                NotConnectedView()
            }
        }
        .environmentObject(model)
        // Refresh whenever the phone becomes connected/reachable, and on launch.
        .task(id: link.status.connected) {
            if link.status.connected { await model.refresh() }
        }
    }
}

/// Shown until the phone has been set up + has pushed a connected status.
private struct NotConnectedView: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "iphone.and.arrow.forward")
                .font(.title2)
                .foregroundStyle(WatchBrand.accent)
            Text("Open Prefrontal on your iPhone to connect.")
                .font(.footnote)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}
