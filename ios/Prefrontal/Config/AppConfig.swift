import Foundation
import Combine

/// Holds the two values a client needs (base URL + token) and persists them:
/// the base URL in UserDefaults, the token in the Keychain.
@MainActor
final class AppConfig: ObservableObject {
    static let shared = AppConfig()

    @Published var baseURLString: String {
        didSet { UserDefaults.standard.set(baseURLString, forKey: "baseURL") }
    }
    @Published var token: String {
        didSet { Keychain.set(token.isEmpty ? nil : token, for: "token") }
    }

    private init() {
        // Prefill with this deployment's tailnet HTTPS origin (tailscale serve).
        let stored = UserDefaults.standard.string(forKey: "baseURL")
        baseURLString = stored ?? "https://agent-1.tail8b0a.ts.net"
        token = Keychain.get("token") ?? ""
    }

    var isConfigured: Bool {
        !token.isEmpty && URL(string: baseURLString) != nil
    }

    var baseURL: URL? { URL(string: baseURLString) }
}
