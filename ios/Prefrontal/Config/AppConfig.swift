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

    /// One-shot hand-off flag: the brain-dump App Intent (a separate process) sets
    /// it; the app drains it on the next `.active` to present the capture sheet
    /// (see `CaptureRouter`). Lives here in Config so the intent — compiled into
    /// the widget target too — can set it without the app-only `CaptureRouter`.
    static let pendingBrainDumpKey = "pendingBrainDump"

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

    /// Opt-in biometric app lock (Face ID / Touch ID). App-only — the widget never
    /// reads it — but stored in the App Group defaults like the other flags. Read
    /// standalone here so `BiometricLock.init` can decide the initial lock state
    /// without touching the `@MainActor` AppConfig.
    static var appLockEnabled: Bool { defaults.bool(forKey: "appLockEnabled") }

    // Web-configured location tunables (#565): cached in the App Group by
    // `LocationMonitor.syncLocationSettings()` from `/schedule/location-settings`
    // so the nonisolated monitor reads them synchronously, with a sensible default
    // until the first sync. These are local App-Group cache keys, not the API
    // field names — `post_interval_s`/`visits_enabled` on the wire map to the
    // `location_`-prefixed keys here (the prefix namespaces them in shared defaults).
    static let geofenceRadiusKey = "geofence_radius_m"
    static let locationPostIntervalKey = "location_post_interval_s"
    static let visitsEnabledKey = "location_visits_enabled"

    /// Curated-place geofence radius (m); default 120 until first synced.
    static var geofenceRadiusM: Double {
        let v = defaults.double(forKey: geofenceRadiusKey)
        return v > 0 ? v : 120
    }
    /// Significant-change post floor (s); default 300 until first synced.
    static var locationPostIntervalS: Double {
        let v = defaults.double(forKey: locationPostIntervalKey)
        return v > 0 ? v : 300
    }
    /// Whether `CLVisit` monitoring runs; default on (absent key → true).
    static var visitsEnabled: Bool {
        defaults.object(forKey: visitsEnabledKey) == nil ? true : defaults.bool(forKey: visitsEnabledKey)
    }

    // MARK: Quick-capture hand-off
    //
    // The interactive-widget button and Control Center control can't collect free
    // text inline, so they open the app straight to the quick-capture sheet. They
    // signal that intent here — a timestamped App-Group flag the app consumes on
    // its next foreground, plus a notification for the already-running case — since
    // the widget/Control Center intent code is compiled into the extension and
    // can't touch app-only view state directly.

    private static let captureRequestKey = "captureRequestedAt"

    /// Ask the app to open the quick-capture sheet at its next opportunity. Sets a
    /// timestamped flag (survives a cold launch) and posts a notification (instant
    /// when the app is already foregrounded).
    static func requestCapture() {
        defaults.set(Date().timeIntervalSince1970, forKey: captureRequestKey)
        NotificationCenter.default.post(name: .prefrontalOpenCapture, object: nil)
    }

    /// Consume a *fresh* capture request (set within `window` seconds), clearing it
    /// so the sheet pops exactly once. A stale flag — left by a crash before the app
    /// could present — is discarded rather than popping the sheet minutes later.
    static func consumeCaptureRequest(window: TimeInterval = 30) -> Bool {
        let ts = defaults.double(forKey: captureRequestKey)
        guard ts > 0 else { return false }
        defaults.removeObject(forKey: captureRequestKey)
        return Date().timeIntervalSince1970 - ts <= window
    }
}

extension Notification.Name {
    /// Posted by `SharedStore.requestCapture()` (the widget/Control Center capture
    /// intents, and the `prefrontal://capture` deep link) so a foregrounded app
    /// pops the quick-capture sheet immediately.
    static let prefrontalOpenCapture = Notification.Name("PrefrontalOpenCapture")
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
    /// Opt-in: gate the app behind Face ID / Touch ID on launch and on return from
    /// the background (`BiometricLock`). Off by default.
    @Published var appLockEnabled: Bool {
        didSet { SharedStore.defaults.set(appLockEnabled, forKey: "appLockEnabled") }
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
        appLockEnabled = SharedStore.appLockEnabled
    }

    var isConfigured: Bool { !token.isEmpty && URL(string: baseURLString) != nil }
    var baseURL: URL? { URL(string: baseURLString) }
}
// `apply(_ payload: ConnectPayload)` lives in an app-only extension in
// Onboarding/ConnectPayload.swift — NOT here. This file (Config/) is compiled
// into the widget extension too, and `ConnectPayload` is app-target-only, so a
// reference here would fail to compile in the widget.
