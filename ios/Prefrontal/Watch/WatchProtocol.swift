import Foundation

/// The wire contract between the iPhone app and its Apple Watch companion.
///
/// The watch never talks to the server directly — the Prefrontal API is
/// Tailscale-only, and a standalone Watch usually isn't on the tailnet. Instead
/// the watch **relays** every request over `WCSession` to the paired iPhone,
/// which holds the token, is on the tailnet, runs the matching `APIClient`
/// call, and relays the JSON back. So this file carries no networking — just the
/// small `Codable` shapes both sides agree on.
///
/// Compiled into **all three** targets (iOS app, watch app, watch widget
/// extension), so keep it dependency-free (Foundation only — no UIKit/SwiftUI).

// MARK: - Request kinds

/// The action a watch message asks the phone to perform. The raw value is the
/// `"kind"` key in the `WCSession` message dictionary.
enum WatchRequestKind: String, Codable {
    // Reads
    case todayGlance          // assembled snapshot for the Today surface
    case selfCare             // the full self-care checks list
    case panic                // panic headline + first step
    // Capture writes (queueable — replayed via transferUserInfo when offline)
    case markSelfCare         // params: key, undo, reset
    case addTodo              // params: title
    // Lifecycle writes (require reachability — never queued)
    case imBack               // close the active outing
    case wrapUpFocus          // end the active focus session
}

/// Keys used inside a `WCSession` message payload. The message is
/// `[messageKey: kind.rawValue] + params`.
enum WatchMessageKey {
    static let kind = "kind"
    static let key = "key"        // self-care check key
    static let undo = "undo"
    static let reset = "reset"
    static let title = "title"    // new todo title
    /// The reply carries the response JSON under this key (a UTF-8 `Data` blob),
    /// or an error string under `error`.
    static let payload = "payload"
    static let error = "error"
}

// MARK: - Glance snapshot

/// The Today surface's data, assembled by the phone from the same endpoints the
/// Home Screen widget's `Glance` uses (`departure/next`, `todos/now`,
/// `self-care`, `outings`, `focus`). Codable so it crosses `WCSession` and can be
/// cached to the watch App Group for the complication.
///
/// Times are carried as the server's raw "yyyy-MM-dd HH:mm:ss" UTC strings and
/// parsed on the watch with the shared `PFDate`, so this stays `Date`-free and
/// trivially Codable.
struct WatchGlance: Codable, Equatable {
    var connected: Bool = true
    // Next departure
    var departureTitle: String?
    var departureLeaveBy: String?   // UTC "yyyy-MM-dd HH:mm:ss"
    var departureLevel: String?
    // The one thing to do now
    var suggestionTitle: String?
    var suggestionMinutes: Int?
    var freeMinutes: Int = 0
    // Next commitment
    var nextTitle: String?
    var nextAt: String?             // UTC "yyyy-MM-dd HH:mm:ss"
    // Active lifecycle
    var outingIntention: String?
    var focusTask: String?
    // Enabled self-care checks, in server order: (key, count, target).
    var selfCare: [WatchCheck] = []

    struct WatchCheck: Codable, Equatable, Identifiable {
        let key: String
        let count: Int
        let target: Int
        var id: String { key }
        var done: Bool { count >= target }
    }

    /// Whether an outing or focus session is running (the most actionable state).
    var hasActive: Bool { outingIntention != nil || focusTask != nil }

    /// A not-yet-connected placeholder (phone hasn't been set up, or the watch
    /// hasn't heard from it yet).
    static let disconnected = WatchGlance(connected: false)

    /// Sample data for previews/placeholders.
    static let sample = WatchGlance(
        departureTitle: "Dentist",
        departureLeaveBy: nil,
        departureLevel: "soon",
        suggestionTitle: "Reply to landlord",
        suggestionMinutes: 15,
        freeMinutes: 45,
        selfCare: [.init(key: "water", count: 3, target: 6),
                   .init(key: "meal", count: 2, target: 3)]
    )
}

// MARK: - Self-care display metadata

/// Label + SF Symbol for a self-care check key, kept in lockstep with the phone
/// (`selfCareLabel` in Views/Shared.swift and the widget's `SelfCareCheck.symbol`)
/// so the watch reads the same. Foundation-only (just strings), so it compiles
/// into every target.
enum WatchSelfCare {
    static func label(_ key: String) -> String {
        ["meal": "Meals", "water": "Water", "meds": "Meds", "biobreak": "Breaks",
         "winddown": "Wind-down", "movement": "Movement"][key] ?? key.capitalized
    }

    static func symbol(_ key: String) -> String {
        ["meal": "fork.knife", "water": "drop.fill", "meds": "pills.fill",
         "biobreak": "figure.walk", "winddown": "moon.fill", "movement": "figure.run"][key]
            ?? "checkmark.circle"
    }
}

// MARK: - Glance cache (watch app ⇄ complication)

/// The latest `WatchGlance`, persisted by the watch app to an App Group its
/// complication extension shares, so the complication renders without its own
/// networking. A watch-local group, distinct from the phone's App Group. Pure
/// Foundation, so it compiles into every target (unused on the phone).
enum WatchGlanceCache {
    static let appGroup = "group.com.morningstatic.prefrontal.watch"
    private static let key = "watchGlance"

    static func write(_ glance: WatchGlance) {
        guard let defaults = UserDefaults(suiteName: appGroup),
              let data = try? JSONEncoder().encode(glance) else { return }
        defaults.set(data, forKey: key)
    }

    static func read() -> WatchGlance? {
        guard let defaults = UserDefaults(suiteName: appGroup),
              let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(WatchGlance.self, from: data)
    }
}

// MARK: - Connection status (application context)

/// The lightweight status the phone pushes to the watch via
/// `updateApplicationContext` so the watch can show a "connect on your phone"
/// state without holding the token. The token deliberately never leaves the
/// phone — the relay makes it unnecessary on the watch.
struct WatchStatus: Codable, Equatable {
    var connected: Bool
    var displayName: String

    /// Encoded into the `[String: Any]` application-context dictionary.
    var context: [String: Any] {
        ["connected": connected, "displayName": displayName]
    }

    init(connected: Bool, displayName: String) {
        self.connected = connected
        self.displayName = displayName
    }

    /// Decode from a received application-context dictionary.
    init?(context: [String: Any]) {
        guard let connected = context["connected"] as? Bool else { return nil }
        self.connected = connected
        self.displayName = context["displayName"] as? String ?? ""
    }
}
