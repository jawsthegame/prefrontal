import Foundation
import Security

/// The bearer token's resting place: a **shared Keychain access group** both the
/// app and the widget extension (and any future Watch/extension) can read (#496).
///
/// Why the Keychain and not the App Group `UserDefaults` the rest of `SharedStore`
/// uses: a `UserDefaults` value is stored unencrypted in the container and swept
/// into unencrypted device backups — the wrong home for a credential. The Keychain
/// gives hardware-backed protection and a per-item accessibility class.
///
/// The item is written with **no** explicit `kSecAttrAccessGroup`, so it lands in
/// the app's first `keychain-access-groups` entitlement — the shared
/// `…prefrontal.shared` group declared on both targets (see `project.yml`). That
/// avoids hard-coding the team's `AppIdentifierPrefix` while still sharing across
/// targets. Accessibility is `AfterFirstUnlock` so the widget and background
/// refresh can read it while the device is locked (but not before first unlock
/// after a reboot).
enum KeychainStore {
    /// Service + account identifying the single token item.
    private static let service = "com.morningstatic.prefrontal"
    private static let account = "prefrontal.token"

    private static var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    /// The stored token, or `nil` if none is set (or the item can't be read).
    static func token() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data,
              let value = String(data: data, encoding: .utf8)
        else { return nil }
        return value
    }

    /// Store (or replace) the token. An empty string clears it, so callers can
    /// treat "" as "disconnected" the same way the old `UserDefaults` path did.
    static func setToken(_ value: String) {
        guard !value.isEmpty else { deleteToken(); return }
        let data = Data(value.utf8)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = baseQuery
            add.merge(attributes) { _, new in new }
            SecItemAdd(add as CFDictionary, nil)
        }
    }

    static func deleteToken() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
