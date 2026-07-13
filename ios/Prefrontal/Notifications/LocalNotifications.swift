import Foundation
import UserNotifications

/// Offline-tolerant local notifications (#474). While the app has network it
/// schedules local `UNNotificationRequest`s for known upcoming nudges — the next
/// departure's *leave-by* time, and each self-care check's *next-due* time — so
/// the alert still fires **even if the phone has since gone off the tailnet**,
/// where server push (ntfy/APNs) can't reach it. On each refresh the pending
/// requests are replaced from current server state, so a moved/cancelled
/// departure or a satisfied check updates rather than double-fires.
///
/// Note: on the tailnet this can overlap a server nudge. That's an accepted
/// tradeoff for an off-network safety net — a duplicate is cheap; a missed
/// departure or self-care nudge isn't. It only schedules when notifications are
/// already authorized (asked during onboarding); otherwise it's a no-op.
enum LocalNotifications {
    static let departureID = "prefrontal.local.departure"
    /// Prefix for the per-check self-care local requests (`…selfcare.water`).
    static let selfCarePrefix = "prefrontal.local.selfcare."

    @discardableResult
    static func reconcileDeparture(_ departure: DepartureNext.Departure?) async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized else {
            center.removePendingNotificationRequests(withIdentifiers: [departureID])
            return false
        }

        // Replace any previously-scheduled leave-by with the current one.
        center.removePendingNotificationRequests(withIdentifiers: [departureID])

        guard let departure,
              let title = departure.title,
              let leaveBy = PFDate.parse(departure.leaveBy),
              leaveBy > Date()
        else { return false }

        let content = UNMutableNotificationContent()
        content.title = "Leave by \(PFDate.time(departure.leaveBy))"
        content.body = [title, departure.location].compactMap { $0 }
            .filter { !$0.isEmpty }.joined(separator: " · ")
        content.sound = .default

        let comps = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: leaveBy)
        let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: false)
        let request = UNNotificationRequest(identifier: departureID, content: content, trigger: trigger)
        do { try await center.add(request); return true }
        catch { return false }
    }

    /// Schedule an offline local nudge for each self-care check that has a future
    /// `next_due`, replacing any previously-scheduled self-care locals. A check
    /// that's gone off/done/open-ended/out-of-window simply isn't re-added.
    static func reconcileSelfCare(_ selfCare: SelfCare?) async {
        // A failed refresh (nil) must NOT wipe already-scheduled offline nudges —
        // off the tailnet is exactly when this fallback matters. `/self-care`
        // always returns a payload when reachable (even master-off), so nil here
        // means the fetch failed; only reconcile against real data. Real data with
        // `enabled == false` still clears below.
        guard let selfCare else { return }
        let center = UNUserNotificationCenter.current()

        // Replace prior self-care locals from current state (one id per check key),
        // so a now-quiet/off check drops out. Cleared even when unauthorized.
        let pending = await center.pendingNotificationRequests()
        let stale = pending.map(\.identifier).filter { $0.hasPrefix(selfCarePrefix) }
        if !stale.isEmpty { center.removePendingNotificationRequests(withIdentifiers: stale) }

        guard await center.notificationSettings().authorizationStatus == .authorized,
              selfCare.enabled else { return }

        for check in selfCare.checks {
            guard check.enabled, !check.openEnded, !check.done,
                  let due = check.nextDue.flatMap(PFDate.parse), due > Date()
            else { continue }
            let copy = selfCareCopy(check.key)
            let content = UNMutableNotificationContent()
            content.title = copy.title
            content.body = copy.body
            content.sound = .default
            let comps = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: false)
            let request = UNNotificationRequest(
                identifier: selfCarePrefix + check.key, content: content, trigger: trigger)
            try? await center.add(request)
        }
    }

    /// Notification copy per self-care check key (mirrors the app's SC_META).
    private static func selfCareCopy(_ key: String) -> (title: String, body: String) {
        switch key {
        case "meal": return ("🍽️ Have you eaten?", "A quick meal check.")
        case "water": return ("💧 Hydration check", "Time for some water.")
        case "meds": return ("💊 Meds check", "Time to take your meds.")
        case "winddown": return ("🌙 Wind-down", "Time to start winding down.")
        case "movement": return ("🧘 Movement", "Time to stretch or move.")
        default: return ("Self-care check", "A gentle nudge from Prefrontal.")
        }
    }
}
