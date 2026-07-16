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

    func testBlockersDecodeAndWaitingDays() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { _ in
            (200, Data(#"{"blockers":[{"id":1,"person":"Sam","what":"the numbers","priority":3,"blocking_since":"2020-01-01 00:00:00","status":"open"}]}"#.utf8))
        }
        let list = try await client().blockers()
        XCTAssertEqual(list.count, 1)
        XCTAssertEqual(list[0].person, "Sam")
        XCTAssertEqual(list[0].what, "the numbers")
        XCTAssertEqual(list[0].priority, 3)
        // 2020 → now is well over 100 days; the floor-of-elapsed/86400 math matches
        // the server's waiting_days helper.
        XCTAssertGreaterThan(list[0].waitingDays, 100)
    }

    func testObserveReturnsProposalCount() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            // The sensor path is POST /observe; the reply carries the pending count.
            XCTAssertEqual(req.url?.path, "/observe")
            XCTAssertEqual(req.httpMethod, "POST")
            return (201, Data(#"{"count":2,"proposals":[]}"#.utf8))
        }
        let count = try await client().observe(text: "I keep blowing off admin on Mondays")
        XCTAssertEqual(count, 2)
    }

    func testEmotionSupportDecodesSkill() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/emotion/support")
            XCTAssertEqual(req.httpMethod, "POST")
            // The typed feeling is forwarded under the `text` key the server reads.
            XCTAssertEqual(req.jsonBody?["text"] as? String, "everything at once")
            return (200, Data(#"{"kind":"skill","state":"overwhelm","skill":"paced_breathing","family":"dbt","text":"Slow the exhale: **in 4, out 6**."}"#.utf8))
        }
        let s = try await client().emotionSupport(text: "everything at once")
        XCTAssertFalse(s.isCrisis)
        XCTAssertEqual(s.family, "dbt")
        XCTAssertEqual(s.state, "overwhelm")
        XCTAssertEqual(s.text, "Slow the exhale: **in 4, out 6**.")
    }

    func testEmotionSupportOneTapSendsEmptyBody() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            // A wordless one-tap request carries no `text` key (server treats an
            // empty body as a generic-skill ask, not a crisis).
            XCTAssertNil(req.jsonBody?["text"])
            return (200, Data(#"{"kind":"skill","state":"generic","skill":"name_and_allow","family":"act","text":"Name it plainly."}"#.utf8))
        }
        let s = try await client().emotionSupport()
        XCTAssertFalse(s.isCrisis)
        XCTAssertEqual(s.skill, "name_and_allow")
    }

    func testEmotionSupportDecodesCrisisResponse() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        // The crisis screen trips server-side and returns resources (empty
        // state/skill/family), never a coping skill — `isCrisis` must reflect that
        // so the view renders resources, not a "try another" skill card.
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.jsonBody?["text"] as? String, "i can't do this anymore")
            return (200, Data(#"{"kind":"crisis","state":"","skill":"","family":"","text":"Please reach out now: call or text 988."}"#.utf8))
        }
        let s = try await client().emotionSupport(text: "i can't do this anymore")
        XCTAssertTrue(s.isCrisis)
        XCTAssertTrue(s.text.contains("988"))
    }

    func testPeopleQueueDecodesMentions() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/people/queue")
            return (200, Data(#"{"mentions":[{"id":7,"name":"Sarah","source":"mail","context":"lunch with Sarah","status":"pending"}]}"#.utf8))
        }
        let queue = try await client().peopleQueue()
        XCTAssertEqual(queue.count, 1)
        XCTAssertEqual(queue[0].id, 7)
        XCTAssertEqual(queue[0].name, "Sarah")
        XCTAssertEqual(queue[0].source, "mail")
    }

    func testIdentifyMentionPostsCategoryBody() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/people/mentions/7/identify")
            XCTAssertEqual(req.httpMethod, "POST")
            // Creates a new person (no person_id) with the chosen category fields.
            XCTAssertEqual(req.jsonBody?["relationship"] as? String, "coworker")
            XCTAssertEqual(req.jsonBody?["importance"] as? Int, 2)
            XCTAssertNil(req.jsonBody?["person_id"])
            return (200, Data(#"{"mention_id":7,"status":"identified"}"#.utf8))
        }
        try await client().identifyMention(7, relationship: "coworker", importance: 2)
    }

    func testDismissMentionPosts() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/people/mentions/7/dismiss")
            XCTAssertEqual(req.httpMethod, "POST")
            return (200, Data(#"{"mention_id":7,"status":"dismissed"}"#.utf8))
        }
        try await client().dismissMention(7)
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

extension URLRequest {
    /// The POST body decoded as a JSON object. A `URLProtocol` sees the body on
    /// `httpBodyStream` (URLSession moves it off `httpBody` before dispatch), so
    /// read the stream when the direct property is nil.
    var jsonBody: [String: Any]? {
        let data: Data?
        if let httpBody {
            data = httpBody
        } else if let stream = httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = Data()
            let size = 4096
            let chunk = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
            defer { chunk.deallocate() }
            while stream.hasBytesAvailable {
                let read = stream.read(chunk, maxLength: size)
                if read <= 0 { break }
                buffer.append(chunk, count: read)
            }
            data = buffer
        } else {
            data = nil
        }
        guard let data, !data.isEmpty else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
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
