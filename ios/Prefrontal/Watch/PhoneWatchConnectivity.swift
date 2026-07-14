import Foundation
import WatchConnectivity
import WidgetKit

/// The iPhone half of the watch relay (see `WatchProtocol.swift`). Owns the
/// `WCSession`, answers the watch's request messages by running the matching
/// `APIClient`/`Endpoints` call, and pushes connection status to the watch.
///
/// Reachability model, mirroring the app's own offline policy:
/// - **Reads** and **lifecycle writes** arrive as `didReceiveMessage` with a
///   reply handler (the watch only sends these when the phone is reachable).
/// - **Capture writes** the watch couldn't deliver live arrive as
///   `didReceiveUserInfo` (its `transferUserInfo` fallback). We run them through
///   the same `queueable` Endpoints methods, so if *this* phone is also off the
///   tailnet they land in the app's `OfflineQueue` and replay later — the write
///   is durable end to end.
final class PhoneWatchConnectivity: NSObject, WCSessionDelegate {
    static let shared = PhoneWatchConnectivity()

    private var session: WCSession? {
        WCSession.isSupported() ? .default : nil
    }

    /// Activate the session (idempotent). Called from `AppDelegate`.
    func activate() {
        guard let session else { return }
        session.delegate = self
        session.activate()
    }

    // MARK: - Status push

    /// Push the current connection status to the watch so it can show a
    /// "connect on your phone" state. Safe to call any time; no-ops until the
    /// session is activated + paired. The token is deliberately never sent.
    @MainActor
    func pushStatus() {
        guard let session, session.activationState == .activated else { return }
        let cfg = AppConfig.shared
        let status = WatchStatus(connected: cfg.isConfigured, displayName: cfg.displayName)
        try? session.updateApplicationContext(status.context)
    }

    // MARK: - WCSessionDelegate

    func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                 error: Error?) {
        // Once active, seed the watch with the current status.
        Task { @MainActor in pushStatus() }
    }

    // Required on iOS; re-activate so a re-paired watch reconnects.
    func sessionDidBecomeInactive(_ session: WCSession) {}
    func sessionDidDeactivate(_ session: WCSession) { session.activate() }

    /// A request expecting a reply (reads + lifecycle writes).
    func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                 replyHandler: @escaping ([String: Any]) -> Void) {
        guard let raw = message[WatchMessageKey.kind] as? String,
              let kind = WatchRequestKind(rawValue: raw) else {
            replyHandler([WatchMessageKey.error: "Unknown request."])
            return
        }
        Task {
            do {
                let payload = try await handle(kind, message: message)
                replyHandler([WatchMessageKey.payload: payload])
            } catch {
                replyHandler([WatchMessageKey.error: error.localizedDescription])
            }
        }
    }

    /// A queued capture write the watch couldn't deliver live.
    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        guard let raw = userInfo[WatchMessageKey.kind] as? String,
              let kind = WatchRequestKind(rawValue: raw) else { return }
        Task { _ = try? await handle(kind, message: userInfo) }
    }

    // MARK: - Dispatch

    /// Run one request against the API and return its response as JSON `Data`
    /// (empty for writes whose body the watch ignores).
    private func handle(_ kind: WatchRequestKind, message: [String: Any]) async throws -> Data {
        // Reads reuse the widget's off-main init (App Group + Keychain), so this
        // runs on the background WCSession queue without a main-actor hop.
        let client = try APIClient(shared: ())
        let empty = Data()

        switch kind {
        case .todayGlance:
            return try JSONEncoder().encode(await assembleGlance(client))
        case .selfCare:
            return try JSONEncoder().encode(try await client.selfCare())
        case .panic:
            return try JSONEncoder().encode(try await client.panic())
        case .markSelfCare:
            guard let key = message[WatchMessageKey.key] as? String else { return empty }
            let undo = message[WatchMessageKey.undo] as? Bool ?? false
            let reset = message[WatchMessageKey.reset] as? Bool ?? false
            try await client.markSelfCare(key: key, undo: undo, reset: reset)
            reloadWatchGlanceSoon()
            return empty
        case .addTodo:
            guard let title = message[WatchMessageKey.title] as? String, !title.isEmpty else { return empty }
            try await client.addTodo(title: title)
            return empty
        case .imBack:
            try await client.returnOuting()
            reloadWatchGlanceSoon()
            return empty
        case .wrapUpFocus:
            try await client.endFocus()
            reloadWatchGlanceSoon()
            return empty
        }
    }

    /// Assemble the Today snapshot from the same endpoints the Home Screen
    /// widget's `Glance.fetch()` uses, mapped into the wire-friendly `WatchGlance`.
    private func assembleGlance(_ client: APIClient) async -> WatchGlance {
        async let depT = try? await client.departureNext()
        async let nowT = try? await client.todosNow(cap: 240)
        async let scT = try? await client.selfCare()
        async let outT = try? await client.outings()
        async let focT = try? await client.focus()
        let dep = await depT, now = await nowT, sc = await scT
        let outing = await outT, focus = await focT

        var g = WatchGlance()
        g.outingIntention = outing?.active.first?.intention
        g.focusTask = focus?.active.first.map { $0.intendedTask ?? "Focusing" }
        if let d = dep?.departure, d.title != nil {
            g.departureTitle = d.title
            g.departureLeaveBy = d.leaveBy
            g.departureLevel = d.level
        }
        if let now {
            g.freeMinutes = Int(now.freeMinutes ?? 0)
            g.nextTitle = now.nextCommitment?.title
            g.nextAt = now.nextCommitment?.startAt
            if let s = now.suggestion, let t = s.title {
                g.suggestionTitle = t
                g.suggestionMinutes = s.estimateMinutes.map { Int($0) }
            }
        }
        if let checks = sc?.checks {
            g.selfCare = checks.filter { $0.enabled }
                .map { .init(key: $0.key, count: $0.count, target: $0.target) }
        }
        return g
    }

    /// A state-changing write may have moved the glance; nudge the watch to
    /// refetch by reloading its complication timelines (the watch app also
    /// refreshes on foreground). Reloads the phone's own widgets too, matching
    /// the App Intents' post-write behavior.
    private func reloadWatchGlanceSoon() {
        WidgetCenter.shared.reloadAllTimelines()
    }
}
