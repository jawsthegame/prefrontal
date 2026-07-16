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

    /// The single "current task" to externalize as a running Live Activity timer:
    /// the most-recently-started **open** todo (M2). Nil when nothing's in
    /// progress. Matches the server's "working on" definition — `started_at` set,
    /// status still `open` (`prefrontal/todos.py`). When several are started, the
    /// latest start wins: that's the one you're actually on right now.
    static func current(in todos: [Todo]) -> Todo? {
        todos
            .filter { $0.isStarted && $0.status == "open" }
            .max { (PFDate.parse($0.startedAt) ?? .distantPast)
                 < (PFDate.parse($1.startedAt) ?? .distantPast) }
    }
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

/// The single honest next action from `GET /next` — the "one next thing" glance.
/// Mirrors `prefrontal.next_thing.NextThing`: one thing, an honest `reason`, and a
/// single `alsoCount` standing in for everything deliberately withheld.
struct NextThing: Codable {
    let kind: String            // focus|outing|departure|todo|blocker|mail|clear
    let reason: String          // mid-flight|leave-now|overdue|avoided|fits|clear|…
    let title: String
    let detail: String
    let action: String?         // wrap_up|im_back|leave|start|open|none
    let source: String?
    let alsoCount: Int?
    let estimateMinutes: Int?
    let freeMinutes: Int?
    let headline: String?
    let commitmentId: Int?
    let todoId: Int?
    let blockerId: Int?
    let sessionId: Int?
    let outingId: Int?

    enum CodingKeys: String, CodingKey {
        case kind, reason, title, detail, action, source, headline
        case alsoCount = "also_count"
        case estimateMinutes = "estimate_minutes"
        case freeMinutes = "free_minutes"
        case commitmentId = "commitment_id"
        case todoId = "todo_id"
        case blockerId = "blocker_id"
        case sessionId = "session_id"
        case outingId = "outing_id"
    }

    static let sample = NextThing(
        kind: "todo", reason: "avoided", title: "Sort the garage",
        detail: "open 6d, kept skipping", action: "start", source: "home",
        alsoCount: 3, estimateMinutes: nil, freeMinutes: nil,
        headline: "Next: Sort the garage — the thing you keep skipping.",
        commitmentId: nil, todoId: 1, blockerId: nil, sessionId: nil, outingId: nil
    )
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

/// Response of `POST /observe` — the LLM-as-sensor read a captured thought and
/// filed `count` *pending* candidate updates for review. Nothing authoritative
/// is written on capture (see `prefrontal/webhooks/routers/sensor.py`); the
/// `proposals` payload is ignored here — the app only needs the count for the
/// capture confirmation.
struct ObserveResult: Codable {
    let count: Int
    enum CodingKeys: String, CodingKey { case count }
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

/// Today's end-of-day self-care **gap** analysis — the read twin of the opt-in
/// evening recap (`GET /self-care/review`, `prefrontal/self_care_review.py`). It
/// reads the day's confirms back as a timeline and names the gaps a raw tally
/// hides (a late first glass, a long stretch between breaks, a quota finished
/// short), plus what went well. A pure read — safe to poll any time.
struct SelfCareReview: Codable {
    let date: String?
    /// The self-care master switch; when off, there's nothing to show.
    let enabled: Bool
    /// Flattened "<Title> — <finding>" gap lines, ready to render.
    let gaps: [String]
    /// Short "what went well" tokens (e.g. "water 6/6", "meds").
    let wins: [String]
    /// True when anything at all was logged or is due today — the visibility gate
    /// (an enabled-but-idle day stays quiet).
    let hasContent: Bool

    enum CodingKeys: String, CodingKey {
        case date, enabled, gaps, wins
        case hasContent = "has_content"
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

// MARK: - Emotion regulation

/// In-the-moment emotion-regulation support (`POST /emotion/support`). The server
/// (`prefrontal/emotion_regulation.py`) screens for crisis language **first**: if
/// it trips, `kind == "crisis"` and `text` is resources + an urge to reach a
/// person — never a coping skill. Otherwise `kind == "skill"` and `text` is one
/// micro-skill fitted to the inferred `state`, from a `family` (`act` / `dbt` /
/// `self_compassion`). All keys are flat single words, so no `CodingKeys` mapping.
/// `text` is delivered verbatim — the client renders it, never paraphrases it.
struct EmotionSupport: Codable {
    let kind: String
    let state: String?
    let skill: String?
    let family: String?
    let text: String

    var isCrisis: Bool { kind == "crisis" }
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

// MARK: - Insights (behavioral stats + focus balance)

/// Aggregated behavioral insights (`GET /stats/data`, `prefrontal/stats.py`) —
/// the "it gets better the longer you use it" story rolled up from the learning
/// loop's episodes. A lean subset of the full payload (the estimate scatter
/// `points` and per-feature rows are dropped; the glanceable summaries stay).
/// Pure read. All fields are safe/zeroed on an empty history.
struct Stats: Codable {
    let timeEstimation: TimeEstimation
    let followThrough: FollowThrough
    let channels: [Channel]
    let selfCare: [SelfCareStat]
    let featureUsage: FeatureUsage
    let counts: EpisodeCounts

    enum CodingKeys: String, CodingKey {
        case channels, counts
        case timeEstimation = "time_estimation"
        case followThrough = "follow_through"
        case selfCare = "self_care"
        case featureUsage = "feature_usage"
    }

    struct EpisodeCounts: Codable { let episodes: Int }

    /// Estimate-vs-actual bias: `ratio` is the median actual/predicted multiplier
    /// ("~1.4× over"); `direction` is over / under / on; `n` the sample size.
    struct TimeEstimation: Codable {
        let n: Int
        let ratio: Double?
        let direction: String?
        let contexts: [Context]

        struct Context: Codable, Identifiable {
            let context: String
            let n: Int
            let ratio: Double?
            let direction: String?
            var id: String { context }
        }
    }

    /// Outcomes over time, framed *forgivingly* — no consecutive-success streak
    /// (a broken streak is the classic ADHD abandonment trigger). `rate` is the
    /// overall completion rate; `recentRate` is "lately"; `trend` is up/steady/down
    /// momentum; `returned` flags a recent return-after-lapse (the comeback the app
    /// celebrates); `bestRate` is a personal-best stretch that can't be lost.
    struct FollowThrough: Codable {
        let n: Int
        let counts: Split
        let rate: Double?
        let recentRate: Double?
        let trend: String?
        let returned: Bool
        let bestRate: Double?
        let series: [String]

        struct Split: Codable { let success: Int; let partial: Int; let miss: Int }

        enum CodingKeys: String, CodingKey {
            case n, counts, rate, trend, returned, series
            case recentRate = "recent_rate"
            case bestRate = "best_rate"
        }
    }

    /// Acknowledgement rate for one delivery channel ("which channel you answer").
    struct Channel: Codable, Identifiable {
        let channel: String
        let n: Int
        let acked: Int
        let rate: Double?
        var id: String { channel }
    }

    /// Per basic-needs check: typical-day adherence, response rate, and nudge→tap latency.
    struct SelfCareStat: Codable, Identifiable {
        let key: String
        let enabled: Bool
        let n: Int
        let confirmed: Int
        let target: Int
        let avgPerDay: Double
        let responseRate: Double?
        let avgLatencySeconds: Double?
        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, enabled, n, confirmed, target
            case avgPerDay = "avg_per_day"
            case responseRate = "response_rate"
            case avgLatencySeconds = "avg_latency_seconds"
        }
    }

    /// Which features you lean on vs. ignore vs. never touch — summary counts only.
    struct FeatureUsage: Codable {
        let summary: Summary
        struct Summary: Codable { let using: Int; let ignored: Int; let dormant: Int; let muted: Int }
    }
}

/// Focus-balance rollup (`GET /balance`) — out-of-home time per life-domain over
/// a window, with each sphere's weekly target and an underserved flag. `hint`
/// explains a lopsided/empty view; `summary` is the one-line digest.
struct FocusBalance: Codable {
    let days: Int
    /// Minutes are floats server-side (rounded to 1dp), and `target_minutes` is
    /// null for an untargeted domain — hence `Double` / `Double?`.
    let totalMinutes: Double
    let summary: String?
    let hint: String?
    let domains: [Domain]

    enum CodingKeys: String, CodingKey {
        case days, summary, hint, domains
        case totalMinutes = "total_minutes"
    }

    struct Domain: Codable, Identifiable {
        let domain: String
        let minutes: Double
        let count: Int
        let targetMinutes: Double?
        let underserved: Bool
        var id: String { domain }

        enum CodingKeys: String, CodingKey {
            case domain, minutes, count, underserved
            case targetMinutes = "target_minutes"
        }
    }
}

// MARK: - Clarifications (ambiguity → honed, startable items)

/// One pending clarifying question about a vague todo/commitment
/// (`GET /clarifications`, `prefrontal/clarify.py`). Answering it hones the item
/// into something startable — the task-initiation lever of the task-paralysis
/// module. `options` are the candidate readings, in the order the server offers
/// them (the index is what `resolve` takes as `option_index`).
struct Clarification: Codable, Identifiable {
    let id: Int
    let targetType: String?
    let targetId: Int?
    let title: String
    let question: String
    let options: [Option]

    enum CodingKeys: String, CodingKey {
        case id, title, question, options
        case targetType = "target_type"
        case targetId = "target_id"
    }

    struct Option: Codable {
        let label: String?
        let taskType: String?
        /// Whether choosing this reading unlocks a built-in guided playbook.
        let hasPlaybook: Bool

        enum CodingKeys: String, CodingKey {
            case label
            case taskType = "task_type"
            case hasPlaybook = "has_playbook"
        }
    }
}

/// A recently-resolved clarification whose chosen reading maps to a playbook, so
/// the walkthrough can be re-opened.
struct GuidedClarification: Codable, Identifiable {
    let id: Int
    let title: String
    let answer: String?
    let taskType: String?
    let playbookTitle: String?

    enum CodingKeys: String, CodingKey {
        case id, title, answer
        case taskType = "task_type"
        case playbookTitle = "playbook_title"
    }
}

/// The `/clarifications` payload: the pending review queue plus resolved items
/// that unlocked a guide.
struct ClarificationList: Codable {
    let clarifications: [Clarification]
    let guided: [GuidedClarification]
}

/// An ordered guided walkthrough for a recognized task type (the payload of
/// `GET /clarifications/playbooks/{task_type}`, and the `playbook` on a resolve).
struct Playbook: Codable, Identifiable {
    let taskType: String
    let title: String
    let intro: String
    let steps: [Step]
    var id: String { taskType }

    enum CodingKeys: String, CodingKey {
        case title, intro, steps
        case taskType = "task_type"
    }

    struct Step: Codable {
        let title: String
        let detail: String
    }
}

/// The result of resolving a clarification — the honed reading, and a `playbook`
/// when the chosen task type has a built-in guide.
struct ClarificationResolveResult: Codable {
    let id: Int
    let status: String
    let answer: String?
    let taskType: String?
    let playbook: Playbook?

    enum CodingKeys: String, CodingKey {
        case id, status, answer, playbook
        case taskType = "task_type"
    }
}

/// The `POST /clarifications/check` sweep result.
struct SweepResult: Codable { let created: Int }

// MARK: - Stuck / avoided todos (honest prioritization)

/// A task you keep bailing on (`GET /todos/stuck`) — repeated misses on the same
/// title. Carries a tiny first step and a body-double ("start together")
/// suggestion, the Task-Paralysis intervention a plain reminder won't fix.
struct StuckTodo: Codable {
    let title: String
    let misses: Int
    let attempts: Int
    let firstStep: String?
    let suggestion: String?

    enum CodingKeys: String, CodingKey {
        case title, misses, attempts, suggestion
        case firstStep = "first_step"
    }
}

struct StuckList: Codable { let stuck: [StuckTodo] }

/// An important todo that's been sitting open, worst-avoided first
/// (`GET /todos/avoided`). `daysOpen`/`score` are floats server-side.
struct AvoidedTodo: Codable, Identifiable {
    let todoId: Int
    let title: String
    let daysOpen: Double
    let score: Double?
    let priority: Int?
    let estimateMinutes: Double?
    let deadline: String?
    var id: Int { todoId }

    enum CodingKeys: String, CodingKey {
        case title, score, priority, deadline
        case todoId = "todo_id"
        case daysOpen = "days_open"
        case estimateMinutes = "estimate_minutes"
    }
}

struct AvoidedList: Codable { let avoided: [AvoidedTodo] }

// MARK: - Blockers (who's waiting on you)

/// Someone *else* is blocked on you — the mirror of a todo (`GET /blockers`).
/// `person` is who's waiting, `what` is what they need from you; `blockingSince`
/// drives the "waiting N days" aging that feeds prioritization.
struct Blocker: Codable, Identifiable, Hashable {
    let id: Int
    let person: String
    let what: String
    let priority: Int?
    let deadline: String?
    let blockingSince: String?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id, person, what, priority, deadline, status
        case blockingSince = "blocking_since"
    }

    /// Whole days since they started waiting, floored at 0 — mirrors the server's
    /// `prefrontal.blockers.waiting_days` (floor of elapsed seconds / 86400), so
    /// the phone's "waiting Nd" matches the briefing and web dashboard.
    var waitingDays: Int {
        guard let since = PFDate.parse(blockingSince) else { return 0 }
        return max(0, Int(Date().timeIntervalSince(since) / 86_400))
    }
}

struct BlockerList: Codable { let blockers: [Blocker] }

// MARK: - Schedule conflicts (double-bookings) + reschedule

/// One side of an overlapping pair (`/commitments/conflicts`).
struct ConflictSide: Codable {
    let id: Int
    let title: String
    let startAt: String?
    let calendar: String?

    enum CodingKeys: String, CodingKey {
        case id, title, calendar
        case startAt = "start_at"
    }
}

/// An overlap between two commitments — a firm double-booking or a soft possible.
/// `key` identifies the pair for dismiss/reschedule.
struct Conflict: Codable, Identifiable {
    let a: ConflictSide
    let b: ConflictSide
    let overlapMinutes: Double?
    let key: String
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case a, b, key
        case overlapMinutes = "overlap_minutes"
    }
}

/// The `/commitments/conflicts` payload: firm double-bookings and soft possibles.
struct ConflictList: Codable {
    let conflicts: [Conflict]
    let possibleConflicts: [Conflict]

    enum CodingKeys: String, CodingKey {
        case conflicts
        case possibleConflicts = "possible_conflicts"
    }
}

/// Result of `POST /commitments/conflicts/reschedule` — the drafted (or sent)
/// polite "please move one" note. `status` is drafted / forwarded / failed.
struct RescheduleResult: Codable {
    let moved: ConflictSide
    let kept: ConflictSide
    let status: String
    let subject: String?
    let body: String?
    let recipient: String?
    let detail: String?
    let offline: Bool?
    let slots: [String]
    let dismissed: String?
}

// MARK: - Context Pack situation tools

/// One on-demand situation tool an enabled Context Pack contributes
/// (`GET /packs/situations`) — e.g. the Parent pack's School run. Empty list
/// when no pack that has tools is enabled.
struct SituationTool: Codable, Identifiable {
    let tool: String
    let title: String
    let description: String?
    var id: String { tool }
}

struct SituationList: Codable { let situations: [SituationTool] }

/// The result of running a situation tool (`POST /packs/situations/{tool}`).
/// Payloads are tool-specific; this decodes the common `headline`/`first_step`
/// plus the known collections each built-in tool returns (school-run
/// `departures`, pack-the-bag `checklists`, sick-day `pressing`). Unknown keys
/// are ignored, so a new tool degrades to its headline.
struct SituationResult: Codable {
    let tool: String?
    let title: String?
    let headline: String?
    let firstStep: String?
    let firstStepFor: String?
    let departures: [Item]?
    let pressing: [Item]?
    let checklists: [Checklist]?

    enum CodingKeys: String, CodingKey {
        case tool, title, headline, departures, pressing, checklists
        case firstStep = "first_step"
        case firstStepFor = "first_step_for"
    }

    /// A school-run departure or a sick-day pressing item (fields optional per tool).
    struct Item: Codable {
        let title: String?
        let message: String?
        let location: String?
        let startAt: String?
        let leaveBy: String?

        enum CodingKeys: String, CodingKey {
            case title, message, location
            case startAt = "start_at"
            case leaveBy = "leave_by"
        }
    }

    /// A pack-the-bag get-ready checklist for one upcoming kid event.
    struct Checklist: Codable {
        let title: String?
        let firstStep: String?
        let steps: [String]?
        let startAt: String?

        enum CodingKeys: String, CodingKey {
            case title, steps
            case firstStep = "first_step"
            case startAt = "start_at"
        }
    }
}

// MARK: - Per-user feature (module) toggles

/// One deployment-enabled module in the per-user Features control
/// (`/settings/features`). `enabled` is whether *this user* has it on; turning it
/// off is a per-user overlay that never changes the deployment default.
struct FeatureModule: Codable, Identifiable {
    let key: String
    let title: String
    /// Modules carry `challenge` (what they address); packs carry `detail`
    /// (the JSON `description`). `blurb` is whichever is present.
    let challenge: String?
    let detail: String?
    let enabled: Bool
    var id: String { key }

    var blurb: String? {
        if let c = challenge, !c.isEmpty { return c }
        if let d = detail, !d.isEmpty { return d }
        return nil
    }

    enum CodingKeys: String, CodingKey {
        case key, title, challenge, enabled
        case detail = "description"
    }

    /// A copy with `enabled` flipped — for an optimistic toggle before the write lands.
    func setting(enabled: Bool) -> FeatureModule {
        FeatureModule(key: key, title: title, challenge: challenge, detail: detail, enabled: enabled)
    }
}

/// The `/settings/features` payload: the deployment-enabled modules and Context
/// packs the user can toggle for themselves.
struct FeatureList: Codable {
    let modules: [FeatureModule]
    let packs: [FeatureModule]
}

// Generic ack for POSTs whose body we ignore beyond success.
struct Ack: Codable {}

// Brain-dump client models (JSONValue, ParsedBrainDump, BrainDumpResponse,
// BrainDumpAction, BrainDumpProposal, ApplyResult) live in Models/BrainDump.swift
// to keep this file under the file-length ceiling.
