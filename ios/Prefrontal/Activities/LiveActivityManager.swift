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
    static let taskKind = "task"

    /// Reconcile running Live Activities with the current active outing / focus /
    /// task. `task` is the current started todo (see `Todo.current(in:)`) — its
    /// elapsed timer is the general "this has been running 22 min" glance (M2),
    /// shown only when there's no formal focus session running (focus is the
    /// richer count-up of the same "I'm working" state, so it supersedes a bare
    /// started-task glance rather than stacking a second activity).
    static func sync(outing: Outings.Outing?,
                     focus: FocusState.Session?,
                     task: Todo? = nil) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            await endAll()   // user turned Live Activities off — don't leave stragglers
            return
        }

        // Outing → "back by" countdown.
        if let outing {
            let started = PFDate.parse(outing.departureAt) ?? Date()
            let ends = outing.timeWindowMinutes.map { started.addingTimeInterval($0 * 60) }
            await ensure(kind: outingKind, title: outing.intention, started: started, ends: ends)
        } else {
            end(kind: outingKind)
        }

        // Focus → elapsed count-up (with a planned-end mark when set). A running
        // focus session supersedes the plain started-task glance below.
        if let focus {
            let started = PFDate.parse(focus.startedAt) ?? Date()
            let ends = focus.plannedMinutes.map { started.addingTimeInterval($0 * 60) }
            await ensure(kind: focusKind, title: focus.intendedTask ?? "Focusing", started: started, ends: ends)
            end(kind: taskKind)
        } else {
            end(kind: focusKind)

            // Task → elapsed count-up from when it was started (estimate, when set,
            // is only the stale mark — the clock keeps counting up past it).
            if let task, task.isStarted {
                let started = PFDate.parse(task.startedAt) ?? Date()
                let ends = task.estimateMinutes.map { started.addingTimeInterval($0 * 60) }
                await ensure(kind: taskKind, title: task.title, started: started, ends: ends)
            } else {
                end(kind: taskKind)
            }
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

    private static func end(kind: String) {
        Task {
            for activity in Activity<SessionActivityAttributes>.activities where activity.attributes.kind == kind {
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    private static func endAll() async {
        for activity in Activity<SessionActivityAttributes>.activities {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }
}
