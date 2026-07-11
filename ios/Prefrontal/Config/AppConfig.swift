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

    private init() {
        baseURLString = SharedStore.baseURL
        token = SharedStore.token
        ntfyServer = SharedStore.ntfyServer
        ntfyTopic = SharedStore.ntfyTopic
        displayName = SharedStore.displayName
    }

    var isConfigured: Bool { !token.isEmpty && URL(string: baseURLString) != nil }
    var baseURL: URL? { URL(string: baseURLString) }

    /// Apply a scanned/opened connect payload. Only overwrites fields the
    /// payload actually carries, so a QR that omits (say) the ntfy topic
    /// leaves an existing one intact. The token/URL are validated separately
    /// by the onboarding flow before this is trusted for real requests.
    func apply(_ payload: ConnectPayload) {
        baseURLString = payload.baseURL
        if let t = payload.token, !t.isEmpty { token = t }
        if let s = payload.ntfyServer, !s.isEmpty { ntfyServer = s }
        if let topic = payload.ntfyTopic, !topic.isEmpty { ntfyTopic = topic }
        if let name = payload.displayName, !name.isEmpty { displayName = name }
    }
}
