import Foundation

/// Backing state for the watch surfaces. Holds the latest `WatchGlance`
/// (relayed from the phone) and drives refresh + the one-tap writes. The glance
/// already carries the enabled self-care checks, so a single `todayGlance`
/// fetch feeds both the Today and Self-care tabs.
@MainActor
final class WatchModel: ObservableObject {
    @Published var glance = WatchGlance.disconnected
    @Published var loading = false
    @Published var errorText: String?

    private let link = WatchConnectivityClient.shared

    /// Refetch the glance from the phone and cache it for the complication.
    func refresh() async {
        loading = true
        defer { loading = false }
        do {
            let g = try await link.request(.todayGlance, as: WatchGlance.self)
            glance = g
            errorText = nil
            link.cache(g)
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// Log a self-care check. Optimistically updates the local count (tap-at-max
    /// wraps to zero, matching the phone/widget), then relays the write; the next
    /// refresh reconciles with the server.
    func mark(_ check: WatchGlance.WatchCheck) {
        let reset = check.done
        link.capture(.markSelfCare, params: [
            WatchMessageKey.key: check.key,
            WatchMessageKey.reset: reset,
        ])
        if let i = glance.selfCare.firstIndex(where: { $0.key == check.key }) {
            let c = glance.selfCare[i]
            let next = reset ? 0 : min(c.count + 1, c.target)
            glance.selfCare[i] = .init(key: c.key, count: next, target: c.target)
        }
    }

    /// A lifecycle write that needs the phone reachable (I'm back / wrap up).
    func lifecycle(_ kind: WatchRequestKind) async {
        do {
            try await link.fire(kind)
            await refresh()
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// Capture a new todo (queueable — survives an unreachable phone).
    func addTodo(_ title: String) {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        link.capture(.addTodo, params: [WatchMessageKey.title: trimmed])
    }
}
