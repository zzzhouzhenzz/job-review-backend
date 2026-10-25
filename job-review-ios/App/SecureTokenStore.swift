import Foundation
import Security

enum SecureTokenStore {
    static let service = "app.jobreview.server-access"
    static func read(for origin: String) throws -> String? {
        var query = identity(origin)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data, let token = String(data: data, encoding: .utf8) else { throw keychainError(status) }
        return token
    }
    /// Empty text explicitly removes the saved credential. A failed update keeps the old item.
    static func save(_ token: String, for origin: String) throws {
        let query = identity(origin)
        if token.isEmpty {
            let status = SecItemDelete(query as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw keychainError(status) }
            return
        }
        let data = Data(token.utf8)
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData: data] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query; item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let result = SecItemAdd(item as CFDictionary, nil)
            guard result == errSecSuccess else { throw keychainError(result) }
        } else if status != errSecSuccess { throw keychainError(status) }
    }
    private static func identity(_ origin: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: origin]
    }
    private static func keychainError(_ status: OSStatus) -> NSError {
        NSError(domain: "JobReview.Keychain", code: Int(status), userInfo: [NSLocalizedDescriptionKey: "Could not access the saved server credential (\(status))."])
    }
}
