import Foundation
import Combine

/// Shared storage between the app and its widget extension. Base URL + token
/// live in an App Group so the widget can authenticate too.
///
/// Note: the token is kept in the App Group `UserDefaults` (not the Keychain)
/// so the extension can read it without a shared Keychain access group — an
/// acceptable tradeoff for a self-hosted personal tool reached over Tailscale.
/// Hardening to a shared Keychain group is tracked under the onboarding issue.
enum SharedStore {
    static let appGroup = "group.com.morningstatic.prefrontal"
    static let defaults = UserDefaults(suiteName: appGroup) ?? .standard

    static let defaultBaseURL = "https://agent-1.tail8b0a.ts.net"

    static var baseURL: String { defaults.string(forKey: "baseURL") ?? defaultBaseURL }
    static var token: String { defaults.string(forKey: "token") ?? "" }

    // ntfy hints carried through onboarding so the app can walk the user
    // through subscribing their phone to the right topic. Not used by the
    // widget, but stored here so both targets read one source of truth.
    static var ntfyServer: String { defaults.string(forKey: "ntfyServer") ?? "https://ntfy.sh" }
    static var ntfyTopic: String { defaults.string(forKey: "ntfyTopic") ?? "" }
    static var displayName: String { defaults.string(forKey: "displayName") ?? "" }

    /// Opt-in geofencing flag, read from the plain defaults so the (nonisolated)
    /// `LocationMonitor` can check it without touching the `@MainActor` AppConfig.
    static var locationEnabled: Bool { defaults.bool(forKey: "locationEnabled") }
}

@MainActor
final class AppConfig: ObservableObject {
    static let shared = AppConfig()

    @Published var baseURLString: String {
        didSet { SharedStore.defaults.set(baseURLString, forKey: "baseURL") }
    }
    @Published var token: String {
        didSet { SharedStore.defaults.set(token, forKey: "token") }
    }
    @Published var ntfyServer: String {
        didSet { SharedStore.defaults.set(ntfyServer, forKey: "ntfyServer") }
    }
    @Published var ntfyTopic: String {
        didSet { SharedStore.defaults.set(ntfyTopic, forKey: "ntfyTopic") }
    }
    @Published var displayName: String {
        didSet { SharedStore.defaults.set(displayName, forKey: "displayName") }
    }
    /// Opt-in: monitor curated places (`/places`) with geofences to auto-log
    /// leaving home / arrivals. Off by default (needs Always-location + battery).
    @Published var locationEnabled: Bool {
        didSet { SharedStore.defaults.set(locationEnabled, forKey: "locationEnabled") }
    }

    private init() {
        baseURLString = SharedStore.baseURL
        token = SharedStore.token
        ntfyServer = SharedStore.ntfyServer
        ntfyTopic = SharedStore.ntfyTopic
        displayName = SharedStore.displayName
        locationEnabled = SharedStore.defaults.bool(forKey: "locationEnabled")
    }

    var isConfigured: Bool { !token.isEmpty && URL(string: baseURLString) != nil }
    var baseURL: URL? { URL(string: baseURLString) }
}
// `apply(_ payload: ConnectPayload)` lives in an app-only extension in
// Onboarding/ConnectPayload.swift — NOT here. This file (Config/) is compiled
// into the widget extension too, and `ConnectPayload` is app-target-only, so a
// reference here would fail to compile in the widget.
