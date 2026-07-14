import Foundation
import WatchConnectivity
import WidgetKit

/// The watch half of the relay (see `WatchProtocol.swift`). Sends request
/// messages to the paired iPhone and decodes the JSON it relays back; the watch
/// itself never touches the network or the token.
///
/// - Reads + lifecycle writes use `sendMessage(_:replyHandler:)`, surfaced as
///   `async` via a continuation. They require the phone to be reachable.
/// - Capture writes (self-care, add todo) fall back to `transferUserInfo` when
///   the phone isn't reachable, so they're delivered when it next is — the
///   watch-side twin of the phone's `OfflineQueue`.
@MainActor
final class WatchConnectivityClient: NSObject, ObservableObject, WCSessionDelegate {
    static let shared = WatchConnectivityClient()

    /// Connection status pushed from the phone (`updateApplicationContext`).
    @Published var status = WatchStatus(connected: false, displayName: "")
    /// Whether the phone is currently reachable for live (reply-bearing) requests.
    @Published var reachable = false

    private var session: WCSession? { WCSession.isSupported() ? .default : nil }

    func activate() {
        guard let session else { return }
        session.delegate = self
        session.activate()
    }

    // MARK: - Requests

    enum RelayError: LocalizedError {
        case unreachable, phoneError(String), badResponse
        var errorDescription: String? {
            switch self {
            case .unreachable: return "Open Prefrontal on your iPhone."
            case let .phoneError(m): return m
            case .badResponse: return "Couldn't read the reply."
            }
        }
    }

    /// Send a read/lifecycle request and decode the phone's reply.
    func request<T: Decodable>(_ kind: WatchRequestKind, params: [String: Any] = [:],
                               as type: T.Type) async throws -> T {
        let data = try await sendForReply(kind, params: params)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw RelayError.badResponse }
    }

    /// A lifecycle write whose reply body we ignore (I'm back, wrap up focus).
    /// Requires reachability — never queued, matching the phone.
    func fire(_ kind: WatchRequestKind, params: [String: Any] = [:]) async throws {
        _ = try await sendForReply(kind, params: params)
    }

    /// A capture write: sent live when reachable, else queued via
    /// `transferUserInfo` for at-least-once delivery.
    func capture(_ kind: WatchRequestKind, params: [String: Any] = [:]) {
        guard let session else { return }
        var message = params
        message[WatchMessageKey.kind] = kind.rawValue
        if session.isReachable {
            session.sendMessage(message, replyHandler: { _ in }, errorHandler: { _ in
                session.transferUserInfo(message)
            })
        } else {
            session.transferUserInfo(message)
        }
    }

    private func sendForReply(_ kind: WatchRequestKind, params: [String: Any]) async throws -> Data {
        guard let session, session.isReachable else { throw RelayError.unreachable }
        var message = params
        message[WatchMessageKey.kind] = kind.rawValue
        return try await withCheckedThrowingContinuation { cont in
            session.sendMessage(message, replyHandler: { reply in
                if let err = reply[WatchMessageKey.error] as? String {
                    cont.resume(throwing: RelayError.phoneError(err))
                } else if let data = reply[WatchMessageKey.payload] as? Data {
                    cont.resume(returning: data)
                } else {
                    cont.resume(returning: Data())  // ack with no body
                }
            }, errorHandler: { error in
                cont.resume(throwing: RelayError.phoneError(error.localizedDescription))
            })
        }
    }

    // MARK: - Glance cache (for the complication)

    /// Persist the latest glance to the shared App Group and reload complications.
    func cache(_ glance: WatchGlance) {
        WatchGlanceCache.write(glance)
        WidgetCenter.shared.reloadAllTimelines()
    }

    // MARK: - WCSessionDelegate

    // Delegate callbacks arrive on a background queue, so they're `nonisolated`
    // and hop to the main actor to touch published state (this also satisfies the
    // WCSessionDelegate conformance from a @MainActor class under Swift 6).
    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                             error: Error?) {
        let reachable = session.isReachable
        Task { @MainActor in self.reachable = reachable }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in self.reachable = reachable }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext ctx: [String: Any]) {
        guard let status = WatchStatus(context: ctx) else { return }
        Task { @MainActor in self.status = status }
    }
}
