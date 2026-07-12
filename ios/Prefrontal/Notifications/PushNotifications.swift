import UIKit
import UserNotifications

/// Native APNs push (#467, client side). The `AppDelegate` registers the device
/// with APNs, hands the token to the server (`POST /route/apns-token`), and
/// handles taps on the notification's action buttons by firing the signed
/// `/nudge/act` URL the server put in the payload — so a one-tap "I'm back" /
/// "Ate" works straight from the notification, the native equivalent of ntfy's
/// inline buttons. Registered as the app's delegate via `UIApplicationDelegateAdaptor`.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(PushCategories.all)
        // If the user already granted notifications on a past launch, refresh the
        // APNs token now (tokens can rotate); the didRegister callback re-posts it.
        Task {
            if await center.notificationSettings().authorizationStatus == .authorized {
                await MainActor.run { application.registerForRemoteNotifications() }
            }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { try? await withAPI { try await $0.registerApnsToken(hex) } }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // Non-fatal — delivery falls back to ntfy on the server side.
    }

    // Show the alert even while the app is foreground.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    // A tapped action button → fire the matching signed URL from the payload.
    // Action identifiers are the button labels (see `PushCategories`), and the
    // payload's `actions` carry `{label, url}`, so we match by label. A plain
    // body tap (`defaultActionIdentifier`) just opens the app.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let tapped = response.actionIdentifier
        guard tapped != UNNotificationDefaultActionIdentifier,
              tapped != UNNotificationDismissActionIdentifier else { return }
        let userInfo = response.notification.request.content.userInfo
        guard let actions = userInfo["actions"] as? [[String: Any]],
              let match = actions.first(where: { ($0["label"] as? String) == tapped }),
              let urlString = match["url"] as? String,
              let url = URL(string: urlString)
        else { return }
        // The URL is self-authenticating (a signed /nudge/act token), so a plain
        // GET does the one-tap action — no header needed.
        _ = try? await URLSession.shared.data(from: url)
    }
}

/// The notification categories the app registers, mirroring the server's nudge
/// buttons (`prefrontal/webhooks/notify.py:_NUDGE_BUTTONS`). Keyed by the APNs
/// `category` the server sets — the cue's `context_key`. A category the server
/// doesn't send simply never appears; a notification whose category we don't
/// know degrades to a plain banner (still delivered, just no buttons).
enum PushCategories {
    /// category id → button titles, in tap order. Titles double as the action
    /// identifiers, matched against the payload's action labels on tap.
    ///
    /// Keys are the server's **`context_key`** (what `DeliveryClient` sets as the
    /// APNs `category`), NOT the nudge *kind*. They usually coincide, but the
    /// weekly check-in's context_key is `"checkin"` even though its buttons come
    /// from the `"load"` kind (`_CONTEXT_KIND["checkin"] == "load"`). Cues whose
    /// context_key has no push buttons server-side (e.g. `away_proposal`, or the
    /// per-user dynamic `trip` domains) aren't here and show as a plain banner.
    static let buttons: [String: [String]] = [
        "focus": ["Wrap up"],
        "outing": ["I'm back", "Abandon"],
        "departure": ["Made it", "Missed it"],
        "pause": ["Stay on task", "Park it", "Switch anyway"],
        "panic": ["✓ Did it"],
        "meal": ["✓ Ate", "Snooze"],
        "water": ["✓ Drank", "Snooze"],
        "meds": ["✓ Took", "Snooze"],
        "biobreak": ["✓ Went", "Snooze"],
        "winddown": ["🌙 Winding down", "Snooze"],
        "movement": ["🧘 Stretched", "Snooze"],
        "star": ["⭐ Yes", "Not today"],
        "checkin": ["Felt light 🙂", "Balanced ⚖️", "Carried a lot 🫠"],
        "digest": ["Caught up 👍"],
        "chore": ["✓ Done"],
    ]

    static var all: Set<UNNotificationCategory> {
        Set(buttons.map { category, titles in
            UNNotificationCategory(
                identifier: category,
                actions: titles.map {
                    UNNotificationAction(identifier: $0, title: $0, options: [])
                },
                intentIdentifiers: [],
                options: []
            )
        })
    }
}
