import Foundation

// Codable models for the shared household sheet — mirrors the JSON of
// `GET /household/sheet` (prefrontal/webhooks/routers/household.py →
// prefrontal/household/build_sheet). We decode a lean subset; unknown keys are
// ignored. Booleans backed by a SQLite column (`enabled`, `got`) arrive as 0/1
// integers, so they decode as `Int?` with a computed `Bool` (mirroring
// MailMessage.unread); server-computed flags (`done_today`, `scheduled_today`)
// arrive as real JSON booleans.

// MARK: - Top-level payload

/// The `GET /household/sheet` response: the structured sheet plus the shared
/// co-parent surfaces (members, invites, weekly check-in, delta digest, and the
/// opt-in load-balance view). A caller in no household gets a 404, which the UI
/// turns into the create/join empty state rather than an error.
struct HouseholdPayload: Codable {
    let sheet: HouseholdSheet
    let markdown: String?
    let invites: [HouseholdInvite]
    /// A household of one is fully supported; the co-parent-only surfaces
    /// (check-in, digest, balance) only light up once a second parent joins.
    let shared: Bool
    let members: [HouseholdMember]
    let checkin: Checkin?
    let digest: Digest?
    let balance: BalanceInfo?
}

/// The structured sheet (`asdict(HouseholdSheet)`), assembled deterministically
/// server-side and shared by both co-parents (household-scoped, not per-user).
struct HouseholdSheet: Codable {
    let householdName: String?
    let children: [RosterMember]
    let pets: [RosterMember]
    let recentlyChanged: [HouseholdChange]
    let perChild: [FactBlock]
    let perPet: [FactBlock]
    let agreements: [Agreement]
    let upcoming: [Appointment]
    let shopping: [ShoppingItem]
    let chores: [Chore]
    let routines: [Routine]
    let counts: HouseholdCounts?

    enum CodingKeys: String, CodingKey {
        case children, pets, agreements, upcoming, shopping, chores, routines, counts
        case householdName = "household_name"
        case recentlyChanged = "recently_changed"
        case perChild = "per_child"
        case perPet = "per_pet"
    }
}

struct HouseholdCounts: Codable {
    let children: Int?
    let pets: Int?
    let facts: Int?
    let agreements: Int?
    let upcoming: Int?
    let shopping: Int?
    let chores: Int?
    let routines: Int?
}

// MARK: - Roster (kids + pets)

/// A kid or a pet — the same roster table, split into the sheet's two sections.
/// `species` is present only for pets.
struct RosterMember: Codable, Identifiable, Hashable {
    let id: Int
    let name: String
    let species: String?
    let birthday: String?
}

// MARK: - Facts (per-member reference)

/// One member's (or the household-wide) reference facts, grouped by category.
/// `childId` is 0 for the household-wide block.
struct FactBlock: Codable, Identifiable {
    let childId: Int
    let childName: String
    let categories: [FactCategory]
    var id: Int { childId }

    enum CodingKeys: String, CodingKey {
        case categories
        case childId = "child_id"
        case childName = "child_name"
    }
}

struct FactCategory: Codable, Identifiable {
    let category: String
    let label: String
    let items: [FactItem]
    var id: String { category }
}

struct FactItem: Codable, Identifiable, Hashable {
    let item: String
    let value: String?
    let updatedByName: String?
    var id: String { item }

    enum CodingKeys: String, CodingKey {
        case item, value
        case updatedByName = "updated_by_name"
    }
}

// MARK: - Agreements / star charts

/// A standing behaviour plan; a star chart when it carries a reward ladder.
/// `starTotal` is the running ledger and `nextGoal` the nearest unreached
/// reward, both precomputed server-side.
struct Agreement: Codable, Identifiable {
    let id: Int
    let title: String
    let kind: String?
    let body: String?
    let childName: String?
    let starTotal: Int?
    let nextGoal: StarGoal?
    let updatedByName: String?

    enum CodingKeys: String, CodingKey {
        case id, title, kind, body
        case childName = "child_name"
        case starTotal = "star_total"
        case nextGoal = "next_goal"
        case updatedByName = "updated_by_name"
    }

    /// A chart is anything with a running star total or a reward ladder.
    var isChart: Bool { starTotal != nil && (nextGoal != nil || (starTotal ?? 0) > 0) }
}

struct StarGoal: Codable {
    let count: Int
    let unit: String?
    let reward: String
    let remaining: Int
}

// MARK: - Appointments

/// An upcoming child appointment (a `kind='child'` commitment), soonest first.
struct Appointment: Codable, Identifiable {
    let id: Int
    let title: String
    let when: String
    let startAt: String?
    let location: String?

    enum CodingKeys: String, CodingKey {
        case id, title, when, location
        case startAt = "start_at"
    }
}

// MARK: - Shopping

/// One shared shopping-list row (still-needed first server-side). `got` is a
/// SQLite 0/1, hence `Int?`.
struct ShoppingItem: Codable, Identifiable, Hashable {
    let id: Int
    let item: String
    let spec: String?
    let whereToBuy: String?
    let got: Int?
    let childName: String?
    let addedByName: String?

    enum CodingKeys: String, CodingKey {
        case id, item, spec, got
        case whereToBuy = "where_to_buy"
        case childName = "child_name"
        case addedByName = "added_by_name"
    }

    var isGot: Bool { (got ?? 0) != 0 }
}

// MARK: - Chores + routines

/// A recurring shared chore with today's status. `enabled` is a SQLite 0/1
/// (`Int?`); `doneToday`/`scheduledToday` are server-computed booleans.
struct Chore: Codable, Identifiable {
    let id: Int
    let title: String
    let ownerName: String?
    let routineId: Int?
    let routineTitle: String?
    let impact: String?
    let enabled: Int?
    let doneToday: Bool
    let scheduledToday: Bool
    let effectiveDays: String?
    let effectiveDueTime: String?

    enum CodingKeys: String, CodingKey {
        case id, title, impact, enabled
        case ownerName = "owner_name"
        case routineId = "routine_id"
        case routineTitle = "routine_title"
        case doneToday = "done_today"
        case scheduledToday = "scheduled_today"
        case effectiveDays = "effective_days"
        case effectiveDueTime = "effective_due_time"
    }

    var isEnabled: Bool { (enabled ?? 1) != 0 }
}

/// A routine grouping chores under one accountable parent, with today's roll-up.
struct Routine: Codable, Identifiable {
    let id: Int
    let title: String
    let accountableName: String?
    let dueTime: String?
    let impact: String?
    let enabled: Int?
    let choresDone: Int?
    let choresTotal: Int?
    let doneToday: Bool?

    enum CodingKeys: String, CodingKey {
        case id, title, impact, enabled
        case accountableName = "accountable_name"
        case dueTime = "due_time"
        case choresDone = "chores_done"
        case choresTotal = "chores_total"
        case doneToday = "done_today"
    }

    var isEnabled: Bool { (enabled ?? 1) != 0 }
}

// MARK: - Recently changed (the catch-up feed)

/// One recent write to the sheet, normalized across facts, agreements, and star
/// grants. `when` is compact "how long ago" phrasing ("2h ago", "just now").
struct HouseholdChange: Codable, Identifiable {
    let what: String
    let who: String?
    let when: String
    let at: String?
    var id: String { what + (at ?? "") }
}

// MARK: - Members + invites

struct HouseholdMember: Codable, Identifiable, Hashable {
    let id: Int
    let name: String
}

/// A pending (unredeemed, unexpired) invite code for the household.
struct HouseholdInvite: Codable, Identifiable {
    let id: Int
    let code: String
    let expiresAt: String?
    let createdByName: String?

    enum CodingKeys: String, CodingKey {
        case id, code
        case expiresAt = "expires_at"
        case createdByName = "created_by_name"
    }
}

/// The light shopping-only read (`GET /household/shopping`).
struct ShoppingList: Codable { let items: [ShoppingItem] }

/// The light chore-status read (`GET /household/chores/done`): the ids done, and
/// the ids scheduled, for one local day.
struct ChoresStatus: Codable {
    let ids: [Int]
    let scheduled: [Int]

    /// Scheduled-but-not-yet-done today — the "remaining chores" count. Uses a
    /// `Set` for the done lookup so it stays O(n+m) rather than O(n*m).
    var remaining: Int {
        let done = Set(ids)
        return scheduled.filter { !done.contains($0) }.count
    }
}

/// The result of minting an invite (`POST /household/invites`).
struct InviteMinted: Codable {
    let code: String
    let joinUrl: String?

    enum CodingKeys: String, CodingKey {
        case code
        case joinUrl = "join_url"
    }
}

// MARK: - Co-parent surfaces (shared households only)

/// The opt-in weekly mental-load check-in schedule + this week's responses.
struct Checkin: Codable {
    let enabled: Bool
    let day: Int?
    let time: String?
    let week: String?
    let responses: [Response]

    struct Response: Codable, Identifiable {
        let byName: String?
        let response: String?
        var id: String { (byName ?? "") + (response ?? "") }
        enum CodingKeys: String, CodingKey {
            case response
            case byName = "by_name"
        }
    }
}

/// The opt-in daily delta digest state — how many of the other parent's changes
/// this viewer hasn't seen yet.
struct Digest: Codable {
    let enabled: Bool
    let unseen: Int
}

/// The load-balance view (opt-in, shared-only). `view` is null when off.
struct BalanceInfo: Codable {
    let enabled: Bool
    let windowDays: Int?
    let view: BalanceView?

    enum CodingKeys: String, CodingKey {
        case enabled, view
        case windowDays = "window_days"
    }
}

/// The "doing" balance (sheet edits, stars, chores done in the window), plus an
/// optional "carrying" facet (routines each parent is accountable for).
struct BalanceView: Codable {
    let total: Int
    let members: [ShareMember]
    let caption: String?
    let carrying: Carrying?

    struct Carrying: Codable {
        let total: Int
        let members: [ShareMember]
        let caption: String?
    }
}

struct ShareMember: Codable, Identifiable {
    let name: String
    let count: Int
    let share: Int
    var id: String { name }
}

// MARK: - Write results

/// Result of redeeming an invite (`POST /household/invites/redeem`).
struct RedeemResult: Codable {
    let ok: Bool
    let householdName: String?
    enum CodingKeys: String, CodingKey {
        case ok
        case householdName = "household_name"
    }
}

/// Result of the bulk "clear bought" sweep.
struct ClearResult: Codable { let cleared: Int }

/// Result of marking a chore done — echoes whether this tap finished a routine,
/// so the client can celebrate too.
struct ChoreDoneResult: Codable {
    let ok: Bool?
    let created: Bool?
    let routineCompleted: RoutineCompleted?

    enum CodingKeys: String, CodingKey {
        case ok, created
        case routineCompleted = "routine_completed"
    }

    struct RoutineCompleted: Codable {
        let routineId: Int?
        let title: String?
        enum CodingKeys: String, CodingKey {
            case title
            case routineId = "routine_id"
        }
    }
}

/// Result of awarding stars — the new total, any reward goals this grant crossed,
/// and the next goal to aim for.
struct StarAwardResult: Codable {
    let total: Int
    let delta: Int?
    let goalsReached: [StarGoal]?
    let nextGoal: StarGoal?

    enum CodingKeys: String, CodingKey {
        case total, delta
        case goalsReached = "goals_reached"
        case nextGoal = "next_goal"
    }
}

// MARK: - Schedule label helper

enum HouseholdSchedule {
    private static let weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    /// A human "Mon, Wed, Fri · 8:00 AM" style label from the sheet's CSV
    /// `effective_days` ("0,2,4") and `effective_due_time` ("HH:MM"). Empty days
    /// read as "daily"; a blank time is an untimed checklist chore.
    static func label(days: String?, dueTime: String?) -> String {
        var parts: [String] = []
        let csv = (days ?? "").trimmingCharacters(in: .whitespaces)
        if csv.isEmpty {
            parts.append("daily")
        } else {
            let names = csv.split(separator: ",")
                .compactMap { Int($0) }
                .filter { (0..<7).contains($0) }
                .map { weekdays[$0] }
            if !names.isEmpty { parts.append(names.joined(separator: ", ")) }
        }
        if let t = dueTime, !t.isEmpty, let pretty = time12(t) { parts.append(pretty) }
        return parts.joined(separator: " · ")
    }

    /// "08:00" → "8:00 AM" in the phone's locale; nil if unparseable.
    private static func time12(_ hhmm: String) -> String? {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        guard let h = parts.first else { return nil }
        let m = parts.count > 1 ? parts[1] : 0
        var c = DateComponents(); c.hour = h; c.minute = m
        guard let date = Calendar.current.date(from: c) else { return nil }
        let f = DateFormatter(); f.timeStyle = .short; f.dateStyle = .none
        return f.string(from: date)
    }
}
