import Foundation
import MacDashboardCore

struct ConsentIntent: Codable, Equatable {
    let origin: String
    let policyVersion: Int
    let requestID: String
    func matches(_ origin: String) -> Bool {
        self.origin == origin && policyVersion == ProjectConsent.policyVersion && UUID(uuidString: requestID) != nil
    }
}

protocol ConsentIntentStoring {
    func load() throws -> ConsentIntent?
    func save(_ intent: ConsentIntent) throws
    func clear() throws
}

/// Stores only a user's pending Agree intent, never a grant or a credential.
struct ConsentIntentFile: ConsentIntentStoring {
    let url: URL
    init(url: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Job Review/pending-project-consent.json")) { self.url = url }
    func load() throws -> ConsentIntent? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let values = try url.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey, .fileSizeKey])
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard values.isSymbolicLink != true, values.isRegularFile == true, (values.fileSize ?? 0) < 4096,
              attributes[.posixPermissions] as? Int == 0o600,
              attributes[.ownerAccountID] as? UInt32 == getuid() else { throw DashboardError.configuration }
        return try JSONDecoder().decode(ConsentIntent.self, from: Data(contentsOf: url))
    }
    func save(_ intent: ConsentIntent) throws {
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        guard try directory.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink != true else { throw DashboardError.configuration }
        let data = try JSONEncoder().encode(intent)
        let temporary = directory.appendingPathComponent(".consent-\(UUID().uuidString).tmp")
        guard FileManager.default.createFile(atPath: temporary.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            throw DashboardError.configuration
        }
        defer { try? FileManager.default.removeItem(at: temporary) }
        // rename atomically replaces the pending file; a final symlink is replaced, never followed.
        guard rename(temporary.path, url.path) == 0 else { throw DashboardError.configuration }
    }
    func clear() throws {
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
    }
}
