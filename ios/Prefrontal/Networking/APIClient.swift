import Foundation

enum APIError: LocalizedError {
    case notConfigured
    case badURL
    case http(Int, String)
    case decoding(String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Set your server URL and token in Settings."
        case .badURL: return "The server URL isn't valid."
        case let .http(code, body):
            if code == 401 { return "Unauthorized — check your token." }
            return "Server error \(code). \(body)"
        case let .decoding(m): return "Couldn't read the server response. \(m)"
        case let .transport(m): return "Couldn't reach Prefrontal. \(m)"
        }
    }
}

/// Thin async JSON client. One instance per request-batch; reads a config
/// snapshot (base URL + token) captured on the main actor.
struct APIClient {
    let baseURL: URL
    let token: String

    @MainActor
    init() throws {
        let cfg = AppConfig.shared
        guard cfg.isConfigured else { throw APIError.notConfigured }
        guard let url = cfg.baseURL else { throw APIError.badURL }
        self.baseURL = url
        self.token = cfg.token
    }

    private static let decoder = JSONDecoder()

    private func request(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil) throws -> URLRequest {
        guard var comps = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false) else {
            throw APIError.badURL
        }
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        guard let url = comps.url else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(token, forHTTPHeaderField: "X-Prefrontal-Token")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        req.timeoutInterval = 20
        return req
    }

    private func send(_ req: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("No HTTP response.")
        }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw APIError.http(http.statusCode, String(body.prefix(200)))
        }
        return data
    }

    func get<T: Decodable>(_ path: String, query: [String: String] = [:], as type: T.Type) async throws -> T {
        let data = try await send(try request("GET", path, query: query))
        do { return try Self.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding("\(error)") }
    }

    @discardableResult
    func post<T: Decodable>(_ path: String, json: [String: Any] = [:], as type: T.Type) async throws -> T {
        let body = try JSONSerialization.data(withJSONObject: json)
        let data = try await send(try request("POST", path, body: body))
        do { return try Self.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding("\(error)") }
    }

    /// POST when the response body doesn't matter.
    func post(_ path: String, json: [String: Any] = [:]) async throws {
        let body = try JSONSerialization.data(withJSONObject: json)
        _ = try await send(try request("POST", path, body: body))
    }
}
