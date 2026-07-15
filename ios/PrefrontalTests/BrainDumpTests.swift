import XCTest

@testable import Prefrontal

/// Unit tests for the brain-dump client (roadmap M1). These cover the pure,
/// deterministic pieces — the `JSONValue` carrier, decoding a `POST /braindump`
/// response with heterogeneous action dicts, reconstructing the wire action to
/// echo back on Apply, and the `/braindump` round-trip over the `URLProtocol`
/// stub. The on-device parse itself (Foundation Models, iOS 26+) isn't exercised
/// here — it's unavailable in the simulator test host and gated behind
/// `#if canImport(FoundationModels)`.
final class BrainDumpTests: XCTestCase {

    private func client() -> APIClient {
        APIClient(baseURL: URL(string: "https://\(StubURLProtocol.host)")!, token: "tok-123")
    }

    override func tearDown() {
        StubURLProtocol.responder = nil
        URLProtocol.unregisterClass(StubURLProtocol.self)
        super.tearDown()
    }

    /// URLSession moves a request body into `httpBodyStream`, so read from there
    /// when `httpBody` is nil (the usual case under `URLProtocol`).
    private func bodyData(_ req: URLRequest) -> Data? {
        if let b = req.httpBody { return b }
        guard let stream = req.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let size = 4096
        let buf = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
        defer { buf.deallocate() }
        while stream.hasBytesAvailable {
            let read = stream.read(buf, maxLength: size)
            if read <= 0 { break }
            data.append(buf, count: read)
        }
        return data
    }

    // MARK: JSONValue

    func testJSONValueDecodesBoolBeforeInt() throws {
        // A `true` must decode as .bool, not .int(1) — the disambiguation matters
        // for round-tripping an action's boolean params (e.g. got:true).
        let v = try JSONDecoder().decode(JSONValue.self, from: Data("true".utf8))
        XCTAssertEqual(v, .bool(true))
        if case let .bool(b) = v { XCTAssertTrue(b) } else { XCTFail("expected .bool") }
    }

    func testJSONValueRoundTripsNestedObject() throws {
        let json = Data(#"{"op":"add_todo","priority":2,"done":false,"tags":["a","b"]}"#.utf8)
        let v = try JSONDecoder().decode(JSONValue.self, from: json)
        guard case let .object(o) = v else { return XCTFail("expected object") }
        XCTAssertEqual(o["op"], .string("add_todo"))
        XCTAssertEqual(o["priority"], .int(2))
        XCTAssertEqual(o["done"], .bool(false))
        XCTAssertEqual(o["tags"], .array([.string("a"), .string("b")]))
        // anyValue is JSONSerialization-ready for the re-POST.
        let any = v.anyValue
        XCTAssertTrue(JSONSerialization.isValidJSONObject(any))
    }

    // MARK: BrainDumpResponse decoding

    func testBrainDumpResponseDecodesActionsProposalsAndProvider() throws {
        let body = Data(#"""
        {
          "reply": "I'll add those.",
          "actions": [
            {"op": "add_todo", "summary": "Add todo: Call the dentist", "title": "Call the dentist", "priority": 2},
            {"op": "add_shopping", "summary": "Buy milk", "item": "milk"}
          ],
          "errors": ["dropped one bad item"],
          "proposals": [
            {"id": 7, "kind": "state", "summary": "set preferred_briefing_format = 'short'",
             "rationale": "keep it short", "status": "pending"}
          ],
          "provider": {"assistant": "on_device", "sensor": "on_device"}
        }
        """#.utf8)
        let r = try JSONDecoder().decode(BrainDumpResponse.self, from: body)
        XCTAssertEqual(r.reply, "I'll add those.")
        XCTAssertEqual(r.actions.count, 2)
        XCTAssertEqual(r.actions[0].op, "add_todo")
        XCTAssertEqual(r.actions[0].summary, "Add todo: Call the dentist")
        XCTAssertEqual(r.errors, ["dropped one bad item"])
        XCTAssertEqual(r.proposals.count, 1)
        XCTAssertEqual(r.proposals[0].id, 7)
        XCTAssertEqual(r.proposals[0].status, "pending")
        XCTAssertEqual(r.provider?["assistant"], "on_device")

        // The action re-serializes to the exact wire dict to echo on Apply,
        // preserving the arbitrary params (title/priority) verbatim.
        let wire = r.actions[0].wire
        XCTAssertEqual(wire["op"] as? String, "add_todo")
        XCTAssertEqual(wire["title"] as? String, "Call the dentist")
        XCTAssertEqual(wire["priority"] as? Int, 2)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(wire))
    }

    func testBrainDumpActionSummaryFallsBackToOp() throws {
        // No "summary" key → the op stands in, so the review row is never blank.
        let body = Data(#"{"reply":"","actions":[{"op":"clear_away"}],"errors":[],"proposals":[]}"#.utf8)
        let r = try JSONDecoder().decode(BrainDumpResponse.self, from: body)
        XCTAssertEqual(r.actions[0].summary, "clear_away")
        XCTAssertNil(r.provider)
    }

    // MARK: ApplyResult decoding

    func testApplyResultDecodes() throws {
        let body = Data(#"""
        {"applied": 1, "errors": [],
         "results": [{"op": "add_todo", "summary": "Add todo", "ok": true, "detail": "todo #5"}]}
        """#.utf8)
        let r = try JSONDecoder().decode(ApplyResult.self, from: body)
        XCTAssertEqual(r.applied, 1)
        XCTAssertEqual(r.results.count, 1)
        XCTAssertTrue(r.results[0].ok)
        XCTAssertEqual(r.results[0].detail, "todo #5")
    }

    // MARK: /braindump round-trip (server-parse path) over the stub

    func testBraindumpTextPostsAndDecodes() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        var seenPath: String?
        StubURLProtocol.responder = { req in
            seenPath = req.url?.path
            return (200, Data(#"{"reply":"ok","actions":[],"errors":[],"proposals":[],"provider":{"assistant":"ollama","sensor":"ollama"}}"#.utf8))
        }
        let r = try await client().braindump(text: "call the dentist")
        XCTAssertEqual(seenPath, "/braindump")
        XCTAssertEqual(r.reply, "ok")
        XCTAssertEqual(r.provider?["assistant"], "ollama")
    }

    // MARK: on-device parse body construction

    func testBraindumpParsePostsParseBody() async throws {
        URLProtocol.registerClass(StubURLProtocol.self)
        var seenPath: String?
        var seenBody: Data?
        StubURLProtocol.responder = { [self] req in
            seenPath = req.url?.path
            seenBody = bodyData(req)
            return (200, Data(#"{"reply":"got it","actions":[],"errors":[],"proposals":[],"provider":{"assistant":"on_device","sensor":"on_device"}}"#.utf8))
        }
        let parsed = ParsedBrainDump(
            reply: "got it",
            wireActions: [["op": "add_todo", "title": "Call the dentist"]])
        let r = try await client().braindump(parse: parsed)
        XCTAssertEqual(seenPath, "/braindump")
        XCTAssertEqual(r.provider?["assistant"], "on_device")

        // The request body carries a `parse` object with the reply + wire actions,
        // and NO top-level `text` — so a regression in braindump(parse:) is caught.
        let obj = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: XCTUnwrap(seenBody)) as? [String: Any])
        XCTAssertNil(obj["text"])
        let parse = try XCTUnwrap(obj["parse"] as? [String: Any])
        XCTAssertEqual(parse["reply"] as? String, "got it")
        let acts = try XCTUnwrap(parse["actions"] as? [[String: Any]])
        XCTAssertEqual(acts.count, 1)
        XCTAssertEqual(acts.first?["op"] as? String, "add_todo")
        XCTAssertEqual(acts.first?["title"] as? String, "Call the dentist")
    }
}
