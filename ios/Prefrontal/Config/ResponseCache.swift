import Foundation

/// A durable, App-Group-backed cache of the last successful **GET** response for
/// each read endpoint — the read-side twin of `OfflineQueue`, which covers
/// writes. Off the tailnet a GET would otherwise throw `APIError.transport` and
/// the screen would blank out behind an error banner; `APIClient.get` writes each
/// successful body here and, on a transport failure, replays the cached body
/// instead — so Today / Todos / Calendar / the widget stay readable offline,
/// clearly marked stale via `ConnectionStore`.
///
/// It lives in the App Group so **every** reader benefits — the app, the widget,
/// and the App Intents all share one cache. Entries are namespaced by a stable
/// hash of the token (a different user's cache is never read) and bounded (oldest
/// evicted past a count *and* byte cap) so the store can't grow without limit.
///
/// It holds the same class of personal data the write queue already keeps in
/// App-Group `UserDefaults` — never the token itself, which stays in the
/// Keychain (`KeychainStore`). A server/user switch clears it (`AppConfig`).
enum ResponseCache {
    private static let bodiesKey = "responseCacheBodies"
    private static let orderKey = "responseCacheOrder"
    private static let maxEntries = 64
    private static let maxTotalBytes = 2 * 1024 * 1024      // 2 MB across all entries

    private static var defaults: UserDefaults { SharedStore.defaults }

    /// A stable cache key for a GET, namespaced by token so a server/user switch
    /// can't surface another account's cached reads. The token is hashed (not
    /// stored verbatim) so a bearer credential never lands in `UserDefaults`.
    static func key(token: String, path: String, query: [String: String]) -> String {
        let q = query.sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }
            .joined(separator: "&")
        return "\(stableHash(token))|\(path)|\(q)"
    }

    /// The cached body for `key`, or nil if we've never cached this read.
    static func load(_ key: String) -> Data? { bodies()[key] }

    /// Cache a fresh body, evicting the oldest entries past the count/byte caps
    /// (LRU by last write). A single body larger than the whole budget is skipped
    /// rather than evicting everything else to make room for it.
    static func store(_ key: String, data: Data) {
        guard data.count <= maxTotalBytes else { return }
        var b = bodies()
        var order = self.order()
        b[key] = data
        order.removeAll { $0 == key }
        order.append(key)
        while order.count > maxEntries || total(of: b) > maxTotalBytes {
            guard let oldest = order.first else { break }
            order.removeFirst()
            b.removeValue(forKey: oldest)
        }
        save(bodies: b, order: order)
    }

    /// Drop every cached read — called on a token change / disconnect so an old
    /// server's data can't linger.
    static func clear() {
        defaults.removeObject(forKey: bodiesKey)
        defaults.removeObject(forKey: orderKey)
    }

    // MARK: storage

    private static func bodies() -> [String: Data] {
        (defaults.dictionary(forKey: bodiesKey) as? [String: Data]) ?? [:]
    }
    private static func order() -> [String] {
        (defaults.array(forKey: orderKey) as? [String]) ?? []
    }
    private static func total(of bodies: [String: Data]) -> Int {
        bodies.values.reduce(0) { $0 + $1.count }
    }
    private static func save(bodies: [String: Data], order: [String]) {
        defaults.set(bodies, forKey: bodiesKey)
        defaults.set(order, forKey: orderKey)
    }

    /// A deterministic 64-bit FNV-1a hash rendered as hex. Swift's `hashValue` is
    /// per-process randomized, which would break cache hits across launches and
    /// between the app and widget processes — so we hash stably ourselves. This
    /// only namespaces entries (collision avoidance), not a security boundary;
    /// `clear()` on a token change is the real isolation.
    private static func stableHash(_ s: String) -> String {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in s.utf8 { hash = (hash ^ UInt64(byte)) &* 0x0000_0100_0000_01b3 }
        return String(hash, radix: 16)
    }
}

/// Tracks whether the app is currently serving **fresh** (network) or **stale**
/// (cached) reads, plus when the last successful sync happened — so the UI can
/// show an honest "Offline — showing data from HH:MM" banner. Written by
/// `APIClient.get`; mirrored on the main actor by `OfflineState` for SwiftUI.
///
/// Lives in the App Group like `ResponseCache`, so the widget process observes
/// the same state. Notifications post only on an online↔stale *transition*, so a
/// screen firing a dozen parallel GETs doesn't spam observers.
enum ConnectionStore {
    private static let onlineKey = "connOnline"
    private static let lastSyncKey = "connLastSyncAt"
    private static var defaults: UserDefaults { SharedStore.defaults }

    /// A GET reached the server: record the sync time and clear the stale flag.
    static func markOnline() {
        let wasOffline = isOffline
        defaults.set(true, forKey: onlineKey)
        defaults.set(Date().timeIntervalSince1970, forKey: lastSyncKey)
        if wasOffline { notifyChanged() }
    }

    /// A GET fell back to the cache (server unreachable). Leaves `lastSyncedAt`
    /// pointing at the last real sync, so the banner can say when that was.
    static func markStale() {
        let wasOffline = isOffline
        defaults.set(false, forKey: onlineKey)
        if !wasOffline { notifyChanged() }
    }

    /// Forget connectivity state (token change / disconnect).
    static func reset() {
        defaults.removeObject(forKey: onlineKey)
        defaults.removeObject(forKey: lastSyncKey)
        notifyChanged()
    }

    /// True once a read has fallen back to cache and no later read has succeeded.
    /// Absent key → not offline (nothing has failed yet), so no banner on a fresh
    /// install before the first request.
    static var isOffline: Bool {
        defaults.object(forKey: onlineKey) != nil && !defaults.bool(forKey: onlineKey)
    }

    /// When the last successful GET landed, or nil if none yet.
    static var lastSyncedAt: Date? {
        let t = defaults.double(forKey: lastSyncKey)
        return t > 0 ? Date(timeIntervalSince1970: t) : nil
    }

    private static func notifyChanged() {
        NotificationCenter.default.post(name: .prefrontalConnectionChanged, object: nil)
    }
}

extension Notification.Name {
    /// Posted by `ConnectionStore` when connectivity flips (online↔stale) or
    /// resets, so `OfflineState` can republish on the main actor.
    static let prefrontalConnectionChanged = Notification.Name("PrefrontalConnectionChanged")
}
