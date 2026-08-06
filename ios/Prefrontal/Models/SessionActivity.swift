import Foundation
import ActivityKit

/// A Live Activity for a running **outing** or **focus** session — a glanceable
/// Lock Screen / Dynamic Island count-up / count-down (#466, M2).
/// Lives in `Models/` so it's compiled into both the app (which starts/ends the
/// activity) and the widget extension (which renders it).
///
/// The clock is *self-ticking*: the views use SwiftUI's `Text(timerInterval:)` /
/// `Text(_, style: .timer)` driven by the dates below, so it counts down/up in
/// real time with **no push updates** — the app only starts and ends it. This is
/// the whole point for M2: "this has been running 22 min" stays live on the Lock
/// Screen without opening the app, directly attacking time agnosia and the
/// time-loss that hyperfocus magnifies.
struct SessionActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// When the session started (a focus counts *up* from here).
        var startedAt: Date
        /// When an outing is due back / a focus's planned time ends. An outing
        /// counts *down* to here; a focus uses it only as the stale mark. `nil`
        /// for an open-ended session.
        var endsAt: Date?
    }

    /// "outing" or "focus" — fixed for the life of the activity. (A legacy
    /// "task" kind from an older build may still ride an in-flight activity at
    /// upgrade; the label/icon helpers below keep rendering it correctly until
    /// the next `sync` ends it.)
    var kind: String
    /// The intention (outing) or task (focus) shown as the headline.
    var title: String

    var isOuting: Bool { kind == "outing" }

    /// Only an outing counts *down* (to its back-by moment); a focus session
    /// counts *up* elapsed, even when a planned end is set.
    var countsDown: Bool { kind == "outing" }

    /// Title-case noun for the Lock Screen label (uppercased in the Dynamic
    /// Island). Unknown kinds fall back to "Focus".
    var noun: String {
        switch kind {
        case "outing": return "Out"
        case "task":   return "Task"   // legacy in-flight activity only (see `kind`)
        default:       return "Focus"
        }
    }

    /// SF Symbol representing the session kind.
    var symbolName: String {
        switch kind {
        case "outing": return "figure.walk"
        case "task":   return "checklist"   // legacy in-flight activity only (see `kind`)
        default:       return "scope"
        }
    }
}
