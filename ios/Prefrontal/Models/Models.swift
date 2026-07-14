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
    let delegation: Delegation?

    enum CodingKeys: String, CodingKey {
        case id, title, notes, priority, energy, status, category, domain, avoidance, decomposition
        case delegation
        case estimateMinutes = "estimate_minutes"
        case deadline
        case startedAt = "started_at"
    }

    var isStarted: Bool { startedAt != nil }
}

/// A todo handed to an assistant: the in-app AI agent (writes a brief + drafts +
/// action items back onto the todo) or a human VA over email. Mirrors the
/// server's `get_delegation` shape and the web dashboard's delegation panel.
struct Delegation: Codable, Hashable {
    let handler: String?          // "agent" | "email"
    let destination: String?      // VA email (email handler)
    let status: String            // in_prep | prepped | forwarded | returned | failed
    let brief: String?
    let detail: String?
    let context: String?
    let drafts: [Draft]?
    let actions: [Action]?

    struct Draft: Codable, Hashable {
        let channel: String?; let to: String?; let subject: String?; let body: String?
    }
    struct Action: Codable, Hashable {
        let text: String?; let mine: Bool?
    }

    /// (label, done-ish) for the status pill.
    var label: String {
        switch status {
        case "prepped":   return "🤖 Prepped"
        case "forwarded": return "✉ Sent"
        case "in_prep":   return "… Prepping"
        case "returned":  return "↩ Returned"
        case "failed":    return "⚠ Needs a hand"
        default:          return status
        }
    }
    var isWorking: Bool { status == "in_prep" }
    var canReturn: Bool { status == "prepped" || status == "forwarded" }
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

struct Recipients: Codable { let recipients: [String] }

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

/// A curated place (from `/places`) the app can geofence. `name` is the match
/// key; a place named "home" is treated as the departure anchor.
struct Place: Codable, Identifiable, Hashable {
    let name: String
    let lat: Double
    let lon: Double
    let label: String?
    var id: String { name }
}

struct PlacesList: Codable { let places: [Place] }

struct Slots: Codable {
    let minutes: Int
    let days: Int
    let slots: [Slot]
    struct Slot: Codable, Identifiable {
        let day: String; let start: String; let end: String; let minutes: Double
        var id: String { day + start + end }
    }
}

// MARK: - Impulsivity / trip App Intents

/// Response of `POST /webhooks/impulse/capture` — an impulse parked as a todo.
struct ImpulseCaptured: Codable {
    let title: String
    let confirmation: String
    enum CodingKeys: String, CodingKey { case title, confirmation }
}

/// Response of `POST /webhooks/focus/switch` — the reflective-pause directive
/// when you feel the pull to switch off the active focus block.
struct SwitchPause: Codable {
    let intendedTask: String
    let elapsedMinutes: Double
    let pauseSeconds: Double
    let message: String
    let options: [String]
    enum CodingKeys: String, CodingKey {
        case intendedTask = "intended_task"
        case elapsedMinutes = "elapsed_minutes"
        case pauseSeconds = "pause_seconds"
        case message, options
    }
}

/// Response of `POST /webhooks/trip/retro` — the closed-out trip's read-back.
struct TripRetroResult: Codable {
    let confirmation: String
    enum CodingKeys: String, CodingKey { case confirmation }
}

// MARK: - Location settings

/// The web-configurable location tunables (`GET /schedule/location-settings`) the
/// app applies to `LocationMonitor` on refresh. Mirrors the server contract in
/// `tests/contracts/location_settings.*`; the drift guard is
/// `tests/test_contract_location_settings.py`. The master opt-in is NOT here — it
/// stays in Me ▸ Settings because only it can trigger the OS "Always" prompt.
struct LocationSettings: Codable {
    let homeRadiusM: Double
    let geofenceRadiusM: Double
    let postIntervalS: Int
    let visitsEnabled: Bool

    enum CodingKeys: String, CodingKey {
        case homeRadiusM = "home_radius_m"
        case geofenceRadiusM = "geofence_radius_m"
        case postIntervalS = "post_interval_s"
        case visitsEnabled = "visits_enabled"
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
        /// Next future local nudge time as the server's UTC "yyyy-MM-dd HH:mm:ss"
        /// text, or nil when off/done/open-ended or nothing's left today. Drives
        /// the offline local notification (#474).
        let nextDue: String?
        var id: String { key }
        enum CodingKeys: String, CodingKey {
            case key, enabled, count, target, done, satisfied, overdue
            case openEnded = "open_ended"
            case nextDue = "next_due"
        }
    }
}

// MARK: - Available hours (per-weekday availability)

/// The user's per-weekday available hours. Mirrors the server's
/// `GET/POST /schedule/available-hours` shape — see the contract fixture in
/// `tests/contracts/available_hours.*` and the drift guard in
/// `tests/test_contract_available_hours.py`. Keep `Day`'s fields in lockstep
/// with the Pydantic `DayAvailability` and the web dashboard's `settings.html`.
struct AvailableHours: Codable {
    /// Whether these are the user's explicit hours (`true`) or the inherited
    /// default waking band (`false`, until they first save).
    let configured: Bool
    /// Weekday key (`mon`…`sun`) → that day's window.
    var days: [String: Day]

    struct Day: Codable {
        var available: Bool
        var start: String   // local "HH:MM" (24-hour)
        var end: String     // local "HH:MM"; must be after `start` when available
    }

    /// Weekday keys in display order, matching the server's `WEEKDAYS`.
    static let order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    private static let labels = [
        "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
        "fri": "Fri", "sat": "Sat", "sun": "Sun",
    ]
    static func label(_ key: String) -> String { labels[key] ?? key.capitalized }

    /// `"HH:MM"` → a `Date` on today for a `.hourAndMinute` `DatePicker`.
    static func date(from hhmm: String) -> Date {
        let cal = Calendar.current
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        return cal.date(bySettingHour: parts.first ?? 9,
                        minute: parts.count > 1 ? parts[1] : 0,
                        second: 0, of: cal.startOfDay(for: Date())) ?? Date()
    }

    /// A picked `Date` → the zero-padded `"HH:MM"` the API expects.
    static func hhmm(from date: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
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

// MARK: - Mail

/// One triaged message from the mail-monitoring pipeline, a lean subset of the
/// server's `mail_messages` row (`prefrontal/memory/repos/mail.py`). Booleans
/// come back as SQLite 0/1 integers, so `unread`/`needs_action` decode as `Int?`.
struct MailMessage: Codable, Identifiable, Hashable {
    let id: Int
    let account: String?
    let senderName: String?
    let senderEmail: String?
    let subject: String?
    let receivedAt: String?
    let snippet: String?
    /// The triage classifier's one-line gist, when present.
    let summary: String?
    /// One of low / normal / high / urgent (`mail/triage.py`).
    let urgency: String?
    /// One of reply / meeting / fyi / newsletter / notification / other.
    let category: String?
    /// Free-text "who is waiting on you", or nil.
    let waitingOn: String?
    let unread: Int?
    let needsAction: Int?
    /// The open todo this message spawned, if triage created one.
    let todoId: Int?

    enum CodingKeys: String, CodingKey {
        case id, account, subject, snippet, summary, urgency, category, unread
        case senderName = "sender_name"
        case senderEmail = "sender_email"
        case receivedAt = "received_at"
        case waitingOn = "waiting_on"
        case needsAction = "needs_action"
        case todoId = "todo_id"
    }

    var isUnread: Bool { (unread ?? 0) != 0 }
    var flaggedAction: Bool { (needsAction ?? 0) != 0 }

    /// Best available display name for the sender.
    var senderDisplay: String {
        if let n = senderName, !n.isEmpty { return n }
        if let e = senderEmail, !e.isEmpty { return e }
        return "Unknown sender"
    }

    /// Prefer the triage summary; fall back to the raw snippet.
    var gist: String? {
        if let s = summary, !s.isEmpty { return s }
        if let s = snippet, !s.isEmpty { return s }
        return nil
    }
}

/// The `/mail` read-only snapshot: messages still awaiting action, plus a recent
/// feed for a dashboard glance.
struct MailInbox: Codable {
    let needsAction: [MailMessage]
    let recent: [MailMessage]

    enum CodingKeys: String, CodingKey {
        case needsAction = "needs_action"
        case recent
    }
}

// Generic ack for POSTs whose body we ignore beyond success.
struct Ack: Codable {}
