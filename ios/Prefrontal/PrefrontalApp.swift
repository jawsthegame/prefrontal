import SwiftUI

@main
struct PrefrontalApp: App {
    @StateObject private var config = AppConfig.shared
    @StateObject private var onboarding = OnboardingModel.shared

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
                    }
                }
        }
    }
}
