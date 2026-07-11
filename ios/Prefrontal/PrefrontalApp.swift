import SwiftUI

@main
struct PrefrontalApp: App {
    @StateObject private var config = AppConfig.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(config)
                .tint(Brand.accent)
        }
    }
}
