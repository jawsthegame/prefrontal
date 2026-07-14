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
        // Re-attach the geofence delegate so a boundary-crossing relaunch (even
        // from terminated) is received; no-op unless the user opted in.
        LocationMonitor.shared.startIfEnabled()
        // Activate the Apple Watch relay (no-op without a paired watch) so the
        // watch can send requests and receive connection status.
        PhoneWatchConnectivity.shared.activate()
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
        // Non-fatal — without a device token this user just isn't an APNs
        // recipient (e.g. a free-signing dev build, which uses the server's
        // dev-only ntfy shim instead).
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

        // Server nudge buttons are signed /nudge/act HTTP(S) URLs: self-
        // authenticating, so a plain background GET performs the one-tap action.
        if let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" {
            _ = try? await URLSession.shared.data(from: url)
            return
        }

        // The only non-HTTP action we emit is the evening "⏰ Set alarm" view button
        // (shortcuts://run-shortcut?…&text=HH:MM). Restrict to that exact scheme so a
        // malformed/abused payload can't make us open an arbitrary deep link
        // (tel:, facetime:, …) — anything else is ignored.
        guard url.scheme?.lowercased() == "shortcuts" else { return }
        // Set a real system alarm natively via AlarmKit (iOS 26+), falling back to
        // opening the Set Alarm Shortcut when AlarmKit isn't available/authorized.
        if let wake = AlarmScheduler.wakeTime(from: url),
           await AlarmScheduler.scheduleWake(hour: wake.hour, minute: wake.minute) {
            return
        }
        await MainActor.run { UIApplication.shared.open(url) }
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
        // The evening morning-prep heads-up: one tap sets a real wake alarm
        // (AlarmKit on iOS 26+, else the Set Alarm Shortcut). See AppDelegate.
        "morning_prep": ["⏰ Set alarm"],
    ]

    /// Categories whose (single) action opens the app rather than running in the
    /// background. The "Set alarm" tap needs the foreground so AlarmKit can show
    /// its first-run authorization prompt (and, on older iOS, so the Shortcut can
    /// launch); every other action is a silent one-tap `/nudge/act` GET.
    static let foregroundCategories: Set<String> = ["morning_prep"]

    static var all: Set<UNNotificationCategory> {
        Set(buttons.map { category, titles in
            let options: UNNotificationActionOptions =
                foregroundCategories.contains(category) ? [.foreground] : []
            return UNNotificationCategory(
                identifier: category,
                actions: titles.map {
                    UNNotificationAction(identifier: $0, title: $0, options: options)
                },
                intentIdentifiers: [],
                options: []
            )
        })
    }
}
