import Foundation
import ActivityKit

/// A Live Activity for a running **outing** or **focus** session — a glanceable
/// Lock Screen / Dynamic Island countdown (#466). Lives in `Models/` so it's
/// compiled into both the app (which starts/ends the activity) and the widget
/// extension (which renders it).
///
/// The clock is *self-ticking*: the views use SwiftUI's `Text(timerInterval:)` /
/// `Text(_, style: .timer)` driven by the dates below, so it counts down/up in
/// real time with **no push updates** — the app only starts and ends it.
struct SessionActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// When the session started (focus counts *up* from here).
        var startedAt: Date
        /// When an outing is due back / a focus's planned time ends (counts
        /// *down* to here). `nil` for an open-ended focus session.
        var endsAt: Date?
    }

    /// "outing" or "focus" — fixed for the life of the activity.
    var kind: String
    /// The intention (outing) or task (focus) shown as the headline.
    var title: String

    var isOuting: Bool { kind == "outing" }
}
