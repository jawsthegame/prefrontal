import Foundation

// Codable models for the self-care checks — mirrors `GET /self-care` and
// `GET /self-care/review` (prefrontal/modules/self_care.py). Split out of
// Models.swift, which sits at the 1200-line lint ceiling; the watch target
// deliberately compiles only Models.swift and uses its own `WatchSelfCare`
// (Watch/WatchProtocol.swift) rather than these API shapes.
//
// Both payloads carry `module_off`: the Self-Care module is off deployment-wide
// (`PREFRONTAL_MODULES`) or via this user's Settings ▸ Features toggle. `enabled`
// is also false in that case, so a client written before the flag existed still
// renders "checks are off" correctly.

struct SelfCare: Codable {
    enum CodingKeys: String, CodingKey {
        case enabled, checks
        case moduleOff = "module_off"
    }

    let enabled: Bool
    let checks: [Check]
    /// True when the module that owns this surface is off — deployment-wide
    /// (`PREFRONTAL_MODULES`) or via this user's Settings ▸ Features toggle. The
    /// payload is empty in that case, so a view hides itself rather than showing
    /// controls whose writes would be refused. Absent on an older server → nil.
    let moduleOff: Bool?
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
