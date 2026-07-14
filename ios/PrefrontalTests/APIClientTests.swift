import XCTest

@testable import Prefrontal

/// Unit tests for `APIClient` — the thin async JSON client. Uses the direct
/// `init(baseURL:token:)` seam (no App Group / Keychain) to assert request
/// construction, and a `URLProtocol` stub to exercise the response/error path
/// without a live server (#602 follow-up).
final class APIClientTests: XCTestCase {

    private func client() -> APIClient {
        APIClient(baseURL: URL(string: "https://\(StubURLProtocol.host)")!, token: "tok-123")
    }

    // MARK: request building (pure, no network)

    func testGetRequestCarriesTokenAcceptAndQuery() throws {
        let req = try client().request("GET", "todos", query: ["limit": "5"])
        XCTAssertEqual(req.httpMethod, "GET")
        XCTAssertEqual(req.url?.absoluteString, "https://h.example/todos?limit=5")
        XCTAssertEqual(req.value(forHTTPHeaderField: "X-Prefrontal-Token"), "tok-123")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Accept"), "application/json")
        // A GET carries no body and sets no Content-Type.
        XCTAssertNil(req.httpBody)
        XCTAssertNil(req.value(forHTTPHeaderField: "Content-Type"))
    }

    func testPostRequestSetsJSONBodyAndContentType() throws {
        let body = try JSONSerialization.data(
            withJSONObject: ["action": "made_it", "source": "app_intent"])
        let req = try client().request("POST", "webhooks/shortcut", body: body)
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(req.value(forHTTPHeaderField: "X-Prefrontal-Token"), "tok-123")
        XCTAssertEqual(req.httpBody, body)
    }

    func testMultiSegmentPathJoins() throws {
        let req = try client().request("POST", "webhooks/focus/start")
        XCTAssertEqual(req.url?.absoluteString, "https://h.example/webhooks/focus/start")
    }

    // MARK: response / error path (URLProtocol stub on URLSession.shared)

    override func tearDown() {
        StubURLProtocol.responder = nil
        URLProtocol.unregisterClass(StubURLProtocol.self)
        super.tearDown()
    }

    private struct Echo: Decodable, Equatable { let ok: Bool }

    func testGetDecodesA2xxJSONResponse() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { _ in (200, Data(#"{"ok":true}"#.utf8)) }
        let value = try await client().get("ping", as: Echo.self)
        XCTAssertEqual(value, Echo(ok: true))
    }

    func testNon2xxMapsToHTTPError() async {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { _ in (500, Data("boom".utf8)) }
        do {
            _ = try await client().get("ping", as: Echo.self)
            XCTFail("expected APIError.http to be thrown")
        } catch let APIError.http(code, _) {
            XCTAssertEqual(code, 500)
        } catch {
            XCTFail("expected APIError.http, got \(error)")
        }
    }
}

/// Answers every request from a canned `(statusCode, body)` so the client's
/// GET/decode/error paths run offline. Registered on `URLSession.shared` (which
/// the client uses) via `URLProtocol.registerClass`.
final class StubURLProtocol: URLProtocol {
    /// Only requests to this host are intercepted, so the stub can't accidentally
    /// swallow an unrelated request during the test run.
    static let host = "h.example"
    static var responder: ((URLRequest) -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        responder != nil && request.url?.host == host
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        guard let responder = Self.responder else {
            // Unreachable given canInit, but fail fast rather than risk a live
            // request if the interception logic ever changes.
            client?.urlProtocol(self, didFailWithError: URLError(.resourceUnavailable))
            return
        }
        let (status, body) = responder(request)
        let response = HTTPURLResponse(
            url: request.url ?? URL(string: "https://\(Self.host)")!,
            statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}
