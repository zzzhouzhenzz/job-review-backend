import Foundation
import CryptoKit

public struct PendingSave: Codable, Equatable, Sendable {
    public var origin: String
    public var path: String
    /// Keep the original bytes. A retry must never regenerate a request ID or selection.
    public var body: Data
    public var summary: String
    public var createdAt: Date
    public init<T: Encodable>(origin: String, path: String, payload: T, summary: String) throws {
        self.origin = origin; self.path = path; self.summary = summary; createdAt = Date()
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        body = try encoder.encode(payload)
    }
}

public struct SavedSession: Codable, Sendable {
    public var schemaVersion = 1
    public var origin: String
    public var state: ReviewState?
    public var savedAt: Date?
    public var pending: PendingSave?
    public init(origin: String, state: ReviewState? = nil, savedAt: Date? = nil, pending: PendingSave? = nil) {
        self.origin = origin; self.state = state?.withoutToken; self.savedAt = savedAt; self.pending = pending
    }
}

public protocol SessionPersistence: Sendable {
    func load() throws -> SavedSession?
    func save(_ value: SavedSession) throws
}

public final class FilePersistence: SessionPersistence, @unchecked Sendable {
    public let file: URL
    public init(directory: URL, origin: String) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var folder = directory; try folder.setResourceValues(values)
        let name = SHA256.hash(data: Data(origin.utf8)).map { String(format: "%02x", $0) }.joined()
        file = directory.appendingPathComponent(name + ".json")
    }
    public func load() throws -> SavedSession? {
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        do {
            let value = try JSONDecoder().decode(SavedSession.self, from: Data(contentsOf: file))
            guard value.schemaVersion == 1 else { throw ReviewError.storage("This saved data uses a newer app version.") }
            return value
        } catch { throw ReviewError.storage("Could not read the saved review. Keep the file for recovery. \(error.localizedDescription)") }
    }
    public func save(_ value: SavedSession) throws {
        do {
            var safe = value; safe.state = value.state?.withoutToken
            let data = try JSONEncoder().encode(safe)
            #if os(iOS)
            try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            #else
            try data.write(to: file, options: .atomic)
            #endif
        } catch { throw ReviewError.storage("Could not save this request on the device. \(error.localizedDescription)") }
    }
}
