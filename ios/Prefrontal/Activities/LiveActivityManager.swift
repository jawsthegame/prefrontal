import Foundation
import ActivityKit

/// Starts and ends the outing/focus/task Live Activities, reconciled against
/// server state on each Today refresh. Because a session can be started from many
/// places (the app, an App Intent, the widget, an ntfy tap), the robust model is
/// to **sync**: whenever we know the current active session, ensure a matching
/// activity exists and end any that no longer should. Start happens while the
/// app is foreground (a refresh), which is the reliable path for `request`.
enum LiveActivityManager {
    static let outingKind = "outing"
    static let focusKind = "focus"

    /// Reconcile the running Live Activity with the current active session.
    ///
    /// **Exactly one** timer shows at a time, chosen by a priority ladder:
    /// **outing → focus**. This is deliberate — two competing clocks dilute the
    /// anti-time-agnosia signal, and the Dynamic Island's compact view can only
    /// render one activity anyway (with two active, iOS picks non-deterministically).
    /// The order reflects salience: an outing means you're physically out with a
    /// hard back-by deadline, and it outranks a focus block's deliberate count-up.
    /// The lower-priority kind is ended so only the winner remains.
    ///
    /// A bare started todo is deliberately *not* externalized here. With no
    /// planned end it produced an open-ended timer that never went stale and
    /// lingered in the Dynamic Island until the todo was cleared — Live Activities
    /// are reserved for the two bounded, deliberately-begun sessions (outing /
    /// focus), which is what makes their clock meaningful.
    static func sync(outing: Outings.Outing?,
                     focus: FocusState.Session?) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            await endAll()   // user turned Live Activities off — don't leave stragglers
            return
        }

        // End the lower-priority kinds *before* ensuring the winner (and await
        // it, unlike a fire-and-forget end) so there's never an instant where two
        // activities are live and iOS could pick the wrong one for the Dynamic
        // Island. `ensure` then reconciles the winner without disturbing it when
        // it's already the one on screen.
        if let outing {
            // Outing → "back by" countdown; it outranks a focus session.
            await endAll(except: outingKind)
            let started = PFDate.parse(outing.departureAt) ?? Date()
            let ends = outing.timeWindowMinutes.map { started.addingTimeInterval($0 * 60) }
            await ensure(kind: outingKind, title: outing.intention, started: started, ends: ends)
        } else if let focus {
            // Focus → elapsed count-up (with a planned-end mark when set).
            await endAll(except: focusKind)
            let started = PFDate.parse(focus.startedAt) ?? Date()
            let ends = focus.plannedMinutes.map { started.addingTimeInterval($0 * 60) }
            await ensure(kind: focusKind, title: focus.intendedTask ?? "Focusing", started: started, ends: ends)
        } else {
            await endAll()
        }
    }

    /// Ensure the running activity of `kind` matches the current session. If one
    /// with the same start + title is already showing, leave it — the clock
    /// self-ticks, so no update is needed. Otherwise (nothing running, or a
    /// *different* session is showing — e.g. you switched to a new task) end the
    /// stale one and start fresh so the glance reflects what you're on right now.
    private static func ensure(kind: String, title: String, started: Date, ends: Date?) async {
        let running = Activity<SessionActivityAttributes>.activities
            .filter { $0.attributes.kind == kind }
        // Same session already on screen? Match on start (to the second) + title.
        if running.contains(where: {
            $0.attributes.title == title
                && abs($0.content.state.startedAt.timeIntervalSince(started)) < 1
        }) { return }

        // Different (or no) session of this kind — clear any stale one first.
        for activity in running {
            await activity.end(nil, dismissalPolicy: .immediate)
        }

        let attributes = SessionActivityAttributes(kind: kind, title: title)
        let state = SessionActivityAttributes.ContentState(startedAt: started, endsAt: ends)
        do {
            _ = try Activity.request(
                attributes: attributes,
                content: .init(state: state, staleDate: ends),
                pushType: nil
            )
        } catch {
            // Not fatal — the app just goes without a Live Activity this session.
        }
    }

    /// End every running activity whose kind isn't `keep` — awaited, so callers
    /// can guarantee the losers are gone before the winner is requested. Passing
    /// `nil` (the default) ends them all.
    private static func endAll(except keep: String? = nil) async {
        for activity in Activity<SessionActivityAttributes>.activities where activity.attributes.kind != keep {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }
}
