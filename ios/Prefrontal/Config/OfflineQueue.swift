import Foundation

/// A durable, App-Group-backed queue of capture writes that couldn't reach the
/// server (the phone was off the tailnet). Off-tailnet, an iOS Shortcut's write
/// just fails and the capture is lost — this catches those instead: the write
/// is persisted and replayed on reconnect, so an "Add todo" or "Ate" tap you
/// make on the train lands when you're home.
///
/// It lives in the App Group so **every** writer benefits — the app, the widget
/// buttons, and the App Intents all share one queue. `APIClient.post(…,
/// queueable: true)` enqueues on a transport failure; `flush()` replays.
///
/// Only *capture* writes opt in (Add Todo, self-care, made/missed) — these are
/// timestamp-tolerant, so a deferred replay is still correct. Stateful lifecycle
/// writes (focus/outing start/return) deliberately do **not** queue: replaying a
/// "start focus" long after the fact would log a bogus session.
///
/// Delivery is at-least-once: a transport failure means no response came back,
/// so the request usually didn't land — but if it did, a replay double-applies.
/// Acceptable for todos/self-care on a personal deployment.
enum OfflineQueue {
    private static let key = "offlineWrites"
    private static var defaults: UserDefaults { SharedStore.defaults }

    /// Persist a write for later replay. `body` must be JSON-serializable
    /// (it already passed through `JSONSerialization` in `APIClient`).
    static func enqueue(path: String, body: [String: Any]) {
        var items = load()
        items.append(["path": path, "body": body])
        save(items)
    }

    /// How many writes are waiting — surfaced in the UI so a queued capture
    /// isn't invisible.
    static var count: Int { load().count }

    /// Replay queued writes oldest-first. Stops at the first transport failure
    /// (still offline) so order is preserved and nothing is dropped; a write
    /// that fails with a real HTTP error is discarded so it can't wedge the
    /// queue. Returns how many were flushed.
    @discardableResult
    static func flush() async -> Int {
        var items = load()
        guard !items.isEmpty else { return 0 }
        let client: APIClient
        do { client = try APIClient(shared: ()) } catch { return 0 }

        var flushed = 0
        while let item = items.first {
            guard let path = item["path"] as? String, let body = item["body"] as? [String: Any] else {
                items.removeFirst(); save(items); continue      // malformed → drop
            }
            do {
                // queueable defaults to false → a transport failure throws here
                // (rather than re-queueing) so we can stop and keep the rest.
                try await client.post(path, json: body)
                items.removeFirst(); flushed += 1; save(items)
            } catch APIError.transport {
                break                                            // still offline
            } catch {
                items.removeFirst(); flushed += 1; save(items)   // real error → drop
            }
        }
        return flushed
    }

    private static func load() -> [[String: Any]] {
        guard let data = defaults.data(forKey: key),
              let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else { return [] }
        return arr
    }

    private static func save(_ items: [[String: Any]]) {
        if items.isEmpty { defaults.removeObject(forKey: key); return }
        if let data = try? JSONSerialization.data(withJSONObject: items) {
            defaults.set(data, forKey: key)
        }
    }
}
