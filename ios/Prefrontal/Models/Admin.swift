import Foundation

// Codable models for the operator admin surface — mirrors the JSON of the
// `/admin/*` routes (prefrontal/webhooks/routers/admin.py). Only an operator's
// token resolves these (a non-operator gets a 403); the app gates the Admin
// screen on `AdminWhoami.isOperator` so the surface never shows for a regular
// user. Booleans backed by a SQLite column (`is_operator` in the users list)
// arrive as 0/1 ints, so they decode as `Int?` with a computed `Bool`
// (mirroring the household models); the create/rotate responses hand back a real
// JSON bool, decoded directly.

/// The signed-in user's admin capabilities (`GET /admin/whoami`). Any
/// authenticated user may call it — a non-operator just sees `isOperator == false`
/// — so the client can decide whether to show the operator-only Admin surface.
struct AdminWhoami: Codable {
    let handle: String
    let isOperator: Bool
    let selfUpdateEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case handle
        case isOperator = "is_operator"
        case selfUpdateEnabled = "self_update_enabled"
    }
}

/// One user in the operator's roster (`GET /admin/users`) — never their token.
/// `isOperatorFlag` is a SQLite 0/1, hence `Int?` with a computed `isOperator`.
struct AdminUser: Codable, Identifiable {
    let handle: String
    let displayName: String?
    let status: String
    let isOperatorFlag: Int?
    let email: String?

    var id: String { handle }
    var isOperator: Bool { (isOperatorFlag ?? 0) != 0 }
    var isActive: Bool { status == "active" }

    enum CodingKeys: String, CodingKey {
        case handle, status, email
        case displayName = "display_name"
        case isOperatorFlag = "is_operator"
    }
}

struct AdminUsersList: Codable { let users: [AdminUser] }

/// The one-time provisioning result (`POST /admin/users`) — carries the raw
/// token, shown **once**. Also the shape returned by `POST /admin/users/{h}/rotate`
/// (which omits the profile fields, so those are optional here).
struct AdminUserCreated: Codable {
    let handle: String
    let displayName: String?
    let isOperator: Bool?
    let email: String?
    let token: String

    enum CodingKeys: String, CodingKey {
        case handle, email, token
        case displayName = "display_name"
        case isOperator = "is_operator"
    }
}

/// A household in the operator view (`GET /admin/households`), with its members.
/// `members` defaults to empty so the leaner `POST /admin/households` create
/// response (`{id, name}`, no members) still decodes.
struct AdminHousehold: Codable, Identifiable {
    let id: Int
    let name: String
    let members: [AdminHouseholdMember]

    enum CodingKeys: String, CodingKey { case id, name, members }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        members = try c.decodeIfPresent([AdminHouseholdMember].self, forKey: .members) ?? []
    }
}

struct AdminHouseholdsList: Codable { let households: [AdminHousehold] }

/// A member row inside an admin household (handle + display name + status).
struct AdminHouseholdMember: Codable, Identifiable {
    let handle: String
    let displayName: String?
    let status: String?
    var id: String { handle }

    /// What to show for the member — their display name, falling back to the handle.
    var label: String { (displayName?.isEmpty == false ? displayName : nil) ?? handle }

    enum CodingKeys: String, CodingKey {
        case handle, status
        case displayName = "display_name"
    }
}
