import XCTest

@testable import Prefrontal

/// Unit tests for the operator admin surface — the `/admin/*` endpoint wrappers
/// (`APIClient` extension), the QR helper, and the connect-link the reveal card
/// builds. Uses the same `StubURLProtocol` seam as `APIClientTests` so the
/// request/response paths run offline (#602).
final class AdminTests: XCTestCase {

    private func client() -> APIClient {
        APIClient(baseURL: URL(string: "https://\(StubURLProtocol.host)")!, token: "op-tok")
    }

    override func setUp() {
        super.setUp()
        URLProtocol.registerClass(StubURLProtocol.self)
    }
    override func tearDown() {
        StubURLProtocol.responder = nil
        URLProtocol.unregisterClass(StubURLProtocol.self)
        super.tearDown()
    }

    // MARK: whoami

    func testWhoamiDecodesOperatorFlag() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/whoami")
            return (200, Data(#"{"handle":"sam","is_operator":true,"self_update_enabled":false}"#.utf8))
        }
        let who = try await client().adminWhoami()
        XCTAssertEqual(who.handle, "sam")
        XCTAssertTrue(who.isOperator)
        XCTAssertEqual(who.selfUpdateEnabled, false)
    }

    // MARK: create user

    func testCreateUserPostsFieldsAndDecodesToken() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/users")
            XCTAssertEqual(req.httpMethod, "POST")
            XCTAssertEqual(req.jsonBody?["handle"] as? String, "sam")
            XCTAssertEqual(req.jsonBody?["display_name"] as? String, "Sam")
            XCTAssertEqual(req.jsonBody?["email"] as? String, "sam@example.com")
            XCTAssertEqual(req.jsonBody?["is_operator"] as? Bool, true)
            return (201, Data(#"{"handle":"sam","display_name":"Sam","is_operator":true,"email":"sam@example.com","token":"secret-token-123"}"#.utf8))
        }
        let created = try await client().adminCreateUser(
            handle: "sam", displayName: "Sam", email: "sam@example.com", isOperator: true)
        XCTAssertEqual(created.handle, "sam")
        XCTAssertEqual(created.token, "secret-token-123")
        XCTAssertEqual(created.isOperator, true)
    }

    func testCreateUserOmitsBlankOptionalFields() async throws {
        StubURLProtocol.responder = { req in
            // Blank display name / email are dropped rather than sent empty.
            XCTAssertNil(req.jsonBody?["display_name"])
            XCTAssertNil(req.jsonBody?["email"])
            XCTAssertEqual(req.jsonBody?["is_operator"] as? Bool, false)
            return (201, Data(#"{"handle":"sam","display_name":null,"is_operator":false,"email":null,"token":"t"}"#.utf8))
        }
        _ = try await client().adminCreateUser(handle: "sam")
    }

    // MARK: users list

    func testUsersListDecodesSqliteOperatorInt() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/users")
            // is_operator arrives as a SQLite 0/1 int on the list route.
            return (200, Data(#"{"users":[{"id":1,"handle":"sam","display_name":"Sam","status":"active","is_operator":1,"email":null},{"id":2,"handle":"kim","display_name":null,"status":"disabled","is_operator":0,"email":"k@x.io"}]}"#.utf8))
        }
        let users = try await client().adminUsers()
        XCTAssertEqual(users.count, 2)
        XCTAssertTrue(users[0].isOperator)
        XCTAssertTrue(users[0].isActive)
        XCTAssertFalse(users[1].isOperator)
        XCTAssertFalse(users[1].isActive)
        XCTAssertEqual(users[1].email, "k@x.io")
    }

    // MARK: rotate / disable / enable / email

    func testRotatePostsToRotatePathAndReturnsToken() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/users/sam/rotate")
            XCTAssertEqual(req.httpMethod, "POST")
            return (200, Data(#"{"handle":"sam","token":"fresh-token"}"#.utf8))
        }
        let rotated = try await client().adminRotateUser("sam")
        XCTAssertEqual(rotated.handle, "sam")
        XCTAssertEqual(rotated.token, "fresh-token")
        XCTAssertNil(rotated.email)  // rotate omits the profile fields
    }

    func testDisableAndEnableHitTheirPaths() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.httpMethod, "POST")
            XCTAssertTrue(req.url?.path == "/admin/users/sam/disable")
            return (200, Data(#"{"handle":"sam","status":"disabled"}"#.utf8))
        }
        try await client().adminDisableUser("sam")

        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/users/sam/enable")
            return (200, Data(#"{"handle":"sam","status":"active"}"#.utf8))
        }
        try await client().adminEnableUser("sam")
    }

    func testSetEmailPostsEmailBody() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/users/sam/email")
            XCTAssertEqual(req.jsonBody?["email"] as? String, "new@x.io")
            return (200, Data(#"{"handle":"sam","email":"new@x.io"}"#.utf8))
        }
        try await client().adminSetUserEmail("sam", email: "new@x.io")
    }

    // MARK: households

    func testHouseholdsListDecodesMembers() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/households")
            return (200, Data(#"{"households":[{"id":3,"name":"The Kims","members":[{"handle":"sam","display_name":"Sam","status":"active"},{"handle":"kim","display_name":null,"status":"active"}]}]}"#.utf8))
        }
        let hh = try await client().adminHouseholds()
        XCTAssertEqual(hh.count, 1)
        XCTAssertEqual(hh[0].id, 3)
        XCTAssertEqual(hh[0].members.count, 2)
        // Display name, falling back to the handle when absent.
        XCTAssertEqual(hh[0].members[0].label, "Sam")
        XCTAssertEqual(hh[0].members[1].label, "kim")
    }

    func testCreateHouseholdDecodesLeanResponseWithoutMembers() async throws {
        // The create route returns {id, name} with no members key — must not fail
        // to decode (AdminHousehold defaults members to []).
        let lean = Data(#"{"id":5,"name":"The Lees"}"#.utf8)
        let decoded = try JSONDecoder().decode(AdminHousehold.self, from: lean)
        XCTAssertEqual(decoded.id, 5)
        XCTAssertTrue(decoded.members.isEmpty)
    }

    func testCreateHouseholdPostsName() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/households")
            XCTAssertEqual(req.jsonBody?["name"] as? String, "The Lees")
            return (201, Data(#"{"id":5,"name":"The Lees"}"#.utf8))
        }
        try await client().adminCreateHousehold(name: "The Lees")
    }

    func testAddHouseholdMemberPostsHandle() async throws {
        StubURLProtocol.responder = { req in
            XCTAssertEqual(req.url?.path, "/admin/households/5/members")
            XCTAssertEqual(req.jsonBody?["handle"] as? String, "sam")
            return (200, Data(#"{"household_id":5,"members":[]}"#.utf8))
        }
        try await client().adminAddHouseholdMember(householdId: 5, handle: "sam")
    }

    // MARK: connect-link + QR

    func testConnectLinkFromCreatedUserRoundTrips() throws {
        // The reveal card builds a connect link from the base URL + minted token.
        let link = ConnectPayload(baseURL: "https://agent-1.tail8b0a.ts.net",
                                  token: "secret", handle: "sam", displayName: "Sam").url
        let url = try XCTUnwrap(link)
        let parsed = try XCTUnwrap(ConnectPayload(url: url))
        XCTAssertEqual(parsed.baseURL, "https://agent-1.tail8b0a.ts.net")
        XCTAssertEqual(parsed.token, "secret")
        XCTAssertEqual(parsed.handle, "sam")
        XCTAssertEqual(parsed.displayName, "Sam")
    }

    func testQRCodeRendersLinkAndRejectsEmpty() {
        XCTAssertNotNil(QRCode.image(from: "prefrontal://connect?url=https%3A%2F%2Fh.example&token=t"))
        XCTAssertNil(QRCode.image(from: ""))
    }
}
