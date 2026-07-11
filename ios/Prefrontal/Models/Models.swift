import Foundation

// The API emits snake_case and naive datetime strings ("2026-07-10 17:28:05").
// We decode a lean subset of each payload; unknown keys are ignored by Codable.

// MARK: - Datetime helpers

enum PFDate {
    /// Parse the server's "yyyy-MM-dd HH:mm:ss" (UTC, naive) strings.
    static let parser: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f
    }()

    static func parse(_ s: String?) -> Date? {
        guard let s else { return nil }
        if let d = parser.date(from: s) { return d }
        // deadline is sometimes date-only midnight; the format above still works.
        return nil
    }

    /// "3:40 PM" / "Tue 3:40 PM" style, in the phone's local zone.
    static func time(_ s: String?) -> String {
        guard let d = parse(s) else { return "" }
        let f = DateFormatter(); f.timeStyle = .short; f.dateStyle = .none
        return f.string(from: d)
    }

    static func dayTime(_ s: String?) -> String {
        guard let d = parse(s) else { return "" }
        let f = DateFormatter(); f.setLocalizedDateFormatFromTemplate("EEE h:mm a")
        return f.string(from: d)
    }
}

// MARK: - Todos

struct Todo: Codable, Identifiable, Hashable {
    let id: Int
    let title: String
    let notes: String?
    let estimateMinutes: Double?
    let priority: Int?
    let deadline: String?
    let energy: String?
    let status: String
    let category: String?
    let domain: String?
    let startedAt: String?
    let avoidance: Avoidance?
    let decomposition: Decomposition?

    enum CodingKeys: String, CodingKey {
        case id, title, notes, priority, energy, status, category, domain, avoidance, decomposition
        case estimateMinutes = "estimate_minutes"
        case deadline
        case startedAt = "started_at"
    }

    var isStarted: Bool { startedAt != nil }
}

struct Avoidance: Codable, Hashable {
    let count: Int?
}

struct Decomposition: Codable, Hashable {
    let firstStep: String?
    let firstStepMinutes: Double?
    let steps: [String]
    let doneSteps: [Int]

    enum CodingKeys: String, CodingKey {
        case firstStep = "first_step"
        case firstStepMinutes = "first_step_minutes"
        case steps
        case doneSteps = "done_steps"
    }

    /// All steps as (index, text, done) with index 0 = firstStep.
    var allSteps: [(index: Int, text: String, done: Bool)] {
        var out: [(Int, String, Bool)] = []
        if let firstStep { out.append((0, firstStep, doneSteps.contains(0))) }
        for (i, s) in steps.enumerated() {
            let idx = i + 1
            out.append((idx, s, doneSteps.contains(idx)))
        }
        return out.map { ($0.0, $0.1, $0.2) }
    }
}

struct TodoList: Codable { let todos: [Todo] }

struct TodosNow: Codable {
    let freeMinutes: Double?
    let withinHours: Bool?
    let nextCommitment: NextCommitment?
    let suggestion: Suggestion?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case freeMinutes = "free_minutes"
        case withinHours = "within_hours"
        case nextCommitment = "next_commitment"
        case suggestion, reason
    }

    struct NextCommitment: Codable { let title: String?; let startAt: String?
        enum CodingKeys: String, CodingKey { case title; case startAt = "start_at" } }
    struct Suggestion: Codable, Identifiable {
        let todoId: Int?; let title: String?; let estimateMinutes: Double?
        var id: Int { todoId ?? title.hashValue }
        enum CodingKeys: String, CodingKey {
            case todoId = "todo_id"; case title; case estimateMinutes = "estimate_minutes" }
    }
}

struct TodosFit: Codable {
    let availableMinutes: Double?
    let fits: [Fit]
    enum CodingKeys: String, CodingKey { case availableMinutes = "available_minutes"; case fits }
    struct Fit: Codable, Identifiable {
        let todoId: Int; let title: String; let estimateMinutes: Double?; let effectiveMinutes: Double?; let priority: Int?
        var id: Int { todoId }
        enum CodingKeys: String, CodingKey {
            case todoId = "todo_id"; case title
            case estimateMinutes = "estimate_minutes"; case effectiveMinutes = "effective_minutes"; case priority }
    }
}

// MARK: - Commitments / calendar

struct Commitment: Codable, Identifiable, Hashable {
    let id: Int
    let title: String
    let startAt: String?
    let endAt: String?
    let location: String?
    let hardness: String?
    let kind: String?
    let domain: String?
    let calendar: String?
    let outcome: String?

    enum CodingKeys: String, CodingKey {
        case id, title, location, hardness, kind, domain, calendar, outcome
        case startAt = "start_at"; case endAt = "end_at"
    }
}

struct CommitmentList: Codable {
    let commitments: [Commitment]
    /// Recently-elapsed commitments still awaiting a made/missed answer (the
    /// server surfaces these for about a day). Drives the outcome affordance.
    let previous: [Commitment]?
}

struct Slots: Codable {
    let minutes: Int
    let days: Int
    let slots: [Slot]
    struct Slot: Codable, Identifiable {
        let day: String; let start: String; let end: String; let minutes: Double
        var id: String { day + start + end }
    }
}

// MARK: - Departure

struct DepartureNext: Codable {
    let departure: Departure?
    let locationKnown: Bool?
    enum CodingKeys: String, CodingKey { case departure; case locationKnown = "location_known" }

    struct Departure: Codable {
        let title: String?
        let location: String?
        let startAt: String?
        let leaveBy: String?
        let minutesUntilLeave: Double?
        let level: String?
        enum CodingKeys: String, CodingKey {
            case title, location, level
            case startAt = "start_at"; case leaveBy = "leave_by"; case minutesUntilLeave = "minutes_until_leave"
        }
    }
}

// MARK: - Outings

struct Outings: Codable {
    let active: [Outing]
    let recent: [Outing]
    struct Outing: Codable, Identifiable {
        let outingId: Int
        let intention: String
        let status: String
        let timeWindowMinutes: Double?
        let departureAt: String?
        var id: Int { outingId }
        enum CodingKeys: String, CodingKey {
            case outingId = "outing_id"; case intention; case status
            case timeWindowMinutes = "time_window_minutes"; case departureAt = "departure_at"
        }
    }
}

// MARK: - Focus

struct FocusState: Codable {
    let active: [Session]
    let recent: [Session]
    struct Session: Codable, Identifiable {
        let sessionId: Int
        let intendedTask: String?
        let status: String
        let plannedMinutes: Double?
        let startedAt: String?
        var id: Int { sessionId }
        enum CodingKeys: String, CodingKey {
            case sessionId = "session_id"; case intendedTask = "intended_task"; case status
            case plannedMinutes = "planned_minutes"; case startedAt = "started_at"
        }
    }
}

// MARK: - Nudges

struct Nudges: Codable {
    let nudges: [Nudge]
    struct Nudge: Codable, Identifiable {
        let id: Int
        let kind: String
        let level: String?
        let message: String
        let createdAt: String?
        enum CodingKeys: String, CodingKey { case id, kind, level, message; case createdAt = "created_at" }
    }
}

// MARK: - Self-care

struct SelfCare: Codable {
    let enabled: Bool
    let checks: [Check]
    struct Check: Codable, Identifiable {
        let key: String
        let enabled: Bool
        let count: Int
        let target: Int
        let done: Bool
        let openEnded: Bool
        let satisfied: Bool
        let overdue: Bool
        var id: String { key }
        enum CodingKeys: String, CodingKey {
            case key, enabled, count, target, done, satisfied, overdue
            case openEnded = "open_ended"
        }
    }
}

// MARK: - Briefing

struct Briefing: Codable {
    let date: String?
    let format: String?
    let today: [Commitment]?
    /// Clean, ready-to-show prose the server renders (`render_briefing`); the
    /// same text plain-text channels receive, with no footer baked in.
    let text: String?
    /// Optional closing encouragement line, when the recovery layer is on.
    let encouragement: String?
    /// On a wide-open day, the recorded "take it easy" / "get things done" pick.
    let openDayChoice: String?

    enum CodingKeys: String, CodingKey {
        case date, format, today, text, encouragement
        case openDayChoice = "open_day_choice"
    }
}

// MARK: - Panic

struct Panic: Codable {
    let headline: String
    let firstStep: String?
    let firstStepFor: String?
    let counts: Counts?
    let late: [Item]
    let soon: [Item]
    let pilingUp: [Item]

    enum CodingKeys: String, CodingKey {
        case headline, counts, late, soon
        case firstStep = "first_step"; case firstStepFor = "first_step_for"
        case pilingUp = "piling_up"
    }

    struct Counts: Codable { let late: Int?; let soon: Int?; let pilingUp: Int?; let pressing: Int?
        enum CodingKeys: String, CodingKey { case late, soon, pressing; case pilingUp = "piling_up" } }

    struct Item: Codable, Identifiable, Hashable {
        let bucket: String?
        let kind: String?
        let title: String
        let when: String?
        let todoId: Int?
        let commitmentId: Int?
        var id: String { (kind ?? "") + title + (when ?? "") }
        enum CodingKeys: String, CodingKey {
            case bucket, kind, title, when
            case todoId = "todo_id"; case commitmentId = "commitment_id"
        }
    }
}

// Generic ack for POSTs whose body we ignore beyond success.
struct Ack: Codable {}
