import Foundation
import ActivityKit

/// Starts and ends the outing/focus Live Activities, reconciled against server
/// state on each Today refresh. Because an outing/focus can be started from many
/// places (the app, an App Intent, the widget, an ntfy tap), the robust model is
/// to **sync**: whenever we know the current active session, ensure a matching
/// activity exists and end any that no longer should. Start happens while the
/// app is foreground (a refresh), which is the reliable path for `request`.
enum LiveActivityManager {
    static let outingKind = "outing"
    static let focusKind = "focus"

    /// Reconcile running Live Activities with the current active outing/focus.
    static func sync(outing: Outings.Outing?, focus: FocusState.Session?) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            await endAll()   // user turned Live Activities off — don't leave stragglers
            return
        }

        // Outing → "back by" countdown.
        if let outing {
            let started = PFDate.parse(outing.departureAt) ?? Date()
            let ends = outing.timeWindowMinutes.map { started.addingTimeInterval($0 * 60) }
            ensure(kind: outingKind, title: outing.intention, started: started, ends: ends)
        } else {
            end(kind: outingKind)
        }

        // Focus → elapsed count-up (with a planned-end mark when set).
        if let focus {
            let started = PFDate.parse(focus.startedAt) ?? Date()
            let ends = focus.plannedMinutes.map { started.addingTimeInterval($0 * 60) }
            ensure(kind: focusKind, title: focus.intendedTask ?? "Focusing", started: started, ends: ends)
        } else {
            end(kind: focusKind)
        }
    }

    /// Start an activity of `kind` if none is running (v1 doesn't live-update an
    /// existing one — the self-ticking clock needs no updates, and the window
    /// rarely changes mid-session).
    private static func ensure(kind: String, title: String, started: Date, ends: Date?) {
        let running = Activity<SessionActivityAttributes>.activities
        guard !running.contains(where: { $0.attributes.kind == kind }) else { return }

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
