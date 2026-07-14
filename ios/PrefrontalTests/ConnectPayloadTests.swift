import XCTest

@testable import Prefrontal

/// Unit tests for `ConnectPayload` — the `prefrontal://connect?…` deep-link the
/// onboarding QR carries. Pure value-type parsing (no App Group / Keychain /
/// network), so it's a good first hermetic seam for the iOS test target (#602).
final class ConnectPayloadTests: XCTestCase {

    func testParsesAFullConnectURL() {
        let payload = ConnectPayload(string:
            "prefrontal://connect?url=https%3A%2F%2Fagent-1.tail8b0a.ts.net"
            + "&token=abc123&ntfy_server=https%3A%2F%2Fntfy.sh"
            + "&ntfy_topic=prefrontal-sam-9f2q&handle=sam&name=Sam")

        XCTAssertNotNil(payload)
        XCTAssertEqual(payload?.baseURL, "https://agent-1.tail8b0a.ts.net")
        XCTAssertEqual(payload?.token, "abc123")
        XCTAssertEqual(payload?.ntfyServer, "https://ntfy.sh")
        XCTAssertEqual(payload?.ntfyTopic, "prefrontal-sam-9f2q")
        XCTAssertEqual(payload?.handle, "sam")
        XCTAssertEqual(payload?.displayName, "Sam")
    }

    func testBareHostGetsHTTPSAndTrailingSlashDropped() {
        // A bare host with no scheme defaults to https…
        XCTAssertEqual(
            ConnectPayload(string: "prefrontal://connect?url=agent-1.tail8b0a.ts.net")?.baseURL,
            "https://agent-1.tail8b0a.ts.net")
        // …and a trailing slash is dropped so appendingPathComponent doesn't double up.
        XCTAssertEqual(
            ConnectPayload(string: "prefrontal://connect?url=https%3A%2F%2Fhost.example%2F")?.baseURL,
            "https://host.example")
    }

    func testTokenAndHintsAreOptional() {
        let payload = ConnectPayload(string: "prefrontal://connect?url=host.example")
        XCTAssertNotNil(payload)
        XCTAssertEqual(payload?.baseURL, "https://host.example")
        XCTAssertNil(payload?.token)
        XCTAssertNil(payload?.ntfyServer)
        XCTAssertNil(payload?.ntfyTopic)
    }

    func testRejectsAnythingThatIsntAUsableConnectURL() {
        XCTAssertNil(ConnectPayload(string: "https://example.com"))          // wrong scheme
        XCTAssertNil(ConnectPayload(string: "prefrontal://connect"))          // no url param
        XCTAssertNil(ConnectPayload(string: "prefrontal://connect?url="))     // empty url param
        XCTAssertNil(ConnectPayload(string: "not a url at all"))
    }

    func testRoundTripsThroughItsCanonicalURL() {
        let original = ConnectPayload(string:
            "prefrontal://connect?url=https%3A%2F%2Fh.example&token=t&handle=sam&name=Sam")
        XCTAssertNotNil(original)
        let rebuilt = original?.url
        XCTAssertNotNil(rebuilt)
        // Rebuilt link re-parses to an equal payload (Equatable).
        XCTAssertEqual(original, rebuilt.flatMap { ConnectPayload(url: $0) })
    }
}
