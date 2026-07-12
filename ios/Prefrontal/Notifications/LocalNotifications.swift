import Foundation
import UserNotifications

/// Offline-tolerant local notifications (#474). While the app has network it
/// schedules a local `UNNotificationRequest` for the next departure's *leave-by*
/// time; the alert then fires at that time **even if the phone has since gone
/// off the tailnet**, where server push (ntfy/APNs) can't reach it. On each
/// refresh the pending request is replaced from current server state, so a
/// moved/cancelled departure updates rather than double-fires.
///
/// Scoped to departures for now — the one nudge with a concrete fire time in the
/// API (`/departure/next` → `leave_by`). Self-care checks have no explicit
/// next-due timestamp to schedule against yet; that's a follow-up.
///
/// Note: on the tailnet this can overlap a server departure nudge. That's an
/// accepted tradeoff for an off-network safety net — a duplicate "leave by" is
/// cheap; a missed departure isn't. It only schedules when notifications are
/// already authorized (asked during onboarding); otherwise it's a no-op.
enum LocalNotifications {
    static let departureID = "prefrontal.local.departure"

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
}
