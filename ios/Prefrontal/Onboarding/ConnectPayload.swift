import Foundation

extension AppConfig {
    /// Apply a scanned/opened connect payload. Only overwrites fields the
    /// payload actually carries, so a QR that omits (say) the ntfy topic leaves
    /// an existing one intact. The token/URL are validated separately by the
    /// onboarding flow before this is trusted for real requests.
    ///
    /// Defined here (app target only) rather than on `AppConfig` in `Config/`,
    /// which is also compiled into the widget extension where `ConnectPayload`
    /// isn't available.
    func apply(_ payload: ConnectPayload) {
        baseURLString = payload.baseURL
        if let t = payload.token, !t.isEmpty { token = t }
        if let s = payload.ntfyServer, !s.isEmpty { ntfyServer = s }
        if let topic = payload.ntfyTopic, !topic.isEmpty { ntfyTopic = topic }
        if let name = payload.displayName, !name.isEmpty { displayName = name }
    }
}

/// The connection details handed to a new phone during onboarding: everything
/// the app needs to reach *this* deployment as *this* user, in one scannable
/// blob. The operator produces it with `prefrontal user connect-link <handle>`
/// (see the CLI / `docs/design/ios-onboarding.md`).
///
/// Wire format is a `prefrontal://connect?…` URL so iOS Camera recognises the
/// QR and offers "Open in Prefrontal" directly, and so the same string works as
/// a tappable link in a setup sheet:
///
/// ```
/// prefrontal://connect?url=https%3A%2F%2Fagent-1.tail8b0a.ts.net
///   &token=abc123&ntfy_server=https%3A%2F%2Fntfy.sh
///   &ntfy_topic=prefrontal-sam-9f2q&handle=sam&name=Sam
/// ```
///
/// `url` is the only required field; the token may be omitted (the user then
/// pastes it) and the ntfy hints are advisory. Kept deliberately tolerant —
/// a malformed extra query item is ignored rather than failing the scan.
struct ConnectPayload: Equatable {
    var baseURL: String
    var token: String?
    var ntfyServer: String?
    var ntfyTopic: String?
    var handle: String?
    var displayName: String?

    static let scheme = "prefrontal"
    static let host = "connect"

    /// Parse a scanned string or opened URL. Accepts a `prefrontal://connect?…`
    /// URL; returns `nil` for anything that isn't one or that lacks a usable
    /// server URL.
    init?(string: String) {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let comps = URLComponents(string: trimmed),
              comps.scheme?.lowercased() == Self.scheme,
              (comps.host?.lowercased() == Self.host || comps.path.contains(Self.host))
        else { return nil }

        let items = Dictionary(
            (comps.queryItems ?? []).map { ($0.name.lowercased(), $0.value ?? "") },
            uniquingKeysWith: { first, _ in first }
        )
        guard let raw = items["url"], let base = Self.normalizedBaseURL(raw) else { return nil }

        baseURL = base
        token = items["token"].flatMap { $0.isEmpty ? nil : $0 }
        ntfyServer = items["ntfy_server"].flatMap { $0.isEmpty ? nil : $0 }
        ntfyTopic = items["ntfy_topic"].flatMap { $0.isEmpty ? nil : $0 }
        handle = items["handle"].flatMap { $0.isEmpty ? nil : $0 }
        displayName = items["name"].flatMap { $0.isEmpty ? nil : $0 }
    }

    init?(url: URL) { self.init(string: url.absoluteString) }

    /// Rebuild the canonical link (used by tests and to round-trip a payload).
    var url: URL? {
        var comps = URLComponents()
        comps.scheme = Self.scheme
        comps.host = Self.host
        var q = [URLQueryItem(name: "url", value: baseURL)]
        if let token { q.append(URLQueryItem(name: "token", value: token)) }
        if let ntfyServer { q.append(URLQueryItem(name: "ntfy_server", value: ntfyServer)) }
        if let ntfyTopic { q.append(URLQueryItem(name: "ntfy_topic", value: ntfyTopic)) }
        if let handle { q.append(URLQueryItem(name: "handle", value: handle)) }
        if let displayName { q.append(URLQueryItem(name: "name", value: displayName)) }
        comps.queryItems = q
        return comps.url
    }

    /// Accept `agent-1.tail8b0a.ts.net`, `http://…`, or `https://…` and return
    /// a well-formed origin (defaulting to https), or `nil` if unusable.
    private static func normalizedBaseURL(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "https://" + s }
        guard let u = URL(string: s), u.host != nil else { return nil }
        // Drop a trailing slash so appendingPathComponent doesn't double up.
        return s.hasSuffix("/") ? String(s.dropLast()) : s
    }
}
