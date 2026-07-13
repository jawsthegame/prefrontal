import Foundation
import Combine

/// Shared storage between the app and its widget extension.
///
/// Non-secret config (base URL, ntfy hints, display name) lives in an App Group
/// `UserDefaults` both targets read. The **token** lives in a shared Keychain
/// access group instead (`KeychainStore`, #496) — a bearer credential doesn't
/// belong in `UserDefaults`, which is unencrypted in the container and in
/// backups. Both the app and the widget carry the `keychain-access-groups`
/// entitlement, so both read the same item.
enum SharedStore {
    static let appGroup = "group.com.morningstatic.prefrontal"
    static let defaults = UserDefaults(suiteName: appGroup) ?? .standard

    static let defaultBaseURL = "https://agent-1.tail8b0a.ts.net"

    static var baseURL: String { defaults.string(forKey: "baseURL") ?? defaultBaseURL }

    /// The token, from the shared Keychain. Falls back to the legacy App Group
    /// `UserDefaults` copy until `migrateTokenIfNeeded()` runs (so an install that
    /// hasn't relaunched the app since the update — e.g. a widget refresh — still
    /// authenticates). Empty string means "not connected".
    static var token: String {
        if let t = KeychainStore.token(), !t.isEmpty { return t }
        return defaults.string(forKey: "token") ?? ""
    }

    /// One-time move of a pre-#496 token from App Group `UserDefaults` into the
    /// Keychain, then wipe the plaintext copy. Idempotent — safe to call on every
    /// launch. Runs from `AppConfig.init` (the app owns the migration; the widget
    /// only reads, and the `token` getter's fallback covers it until then).
    static func migrateTokenIfNeeded() {
        let legacy = defaults.string(forKey: "token") ?? ""
        if (KeychainStore.token() ?? "").isEmpty, !legacy.isEmpty {
            KeychainStore.setToken(legacy)
        }
        // Once the token is safely in the Keychain, remove the plaintext copy.
        if !(KeychainStore.token() ?? "").isEmpty {
            defaults.removeObject(forKey: "token")
        }
    }

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
        // Persist to the shared Keychain (empty clears it). Also drop any stale
        // plaintext copy so a legacy value can't shadow a change.
        didSet {
            KeychainStore.setToken(token)
            SharedStore.defaults.removeObject(forKey: "token")
        }
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
        // Move a pre-#496 plaintext token into the Keychain before the first read.
        SharedStore.migrateTokenIfNeeded()
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
