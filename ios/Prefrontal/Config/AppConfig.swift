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

    private init() {
        baseURLString = SharedStore.baseURL
        token = SharedStore.token
    }

    var isConfigured: Bool { !token.isEmpty && URL(string: baseURLString) != nil }
    var baseURL: URL? { URL(string: baseURLString) }
}
