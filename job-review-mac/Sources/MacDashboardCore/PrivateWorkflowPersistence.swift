import CryptoKit
import Darwin
import Foundation
import ReviewCore

/// A private, origin-specific journal. A failed durable save prevents the shared session's POST.
public final class PrivateWorkflowPersistence: WorkflowPersistence, @unchecked Sendable {
    public let file: URL
    private let directory: URL
    private let origin: String
    private let lock = NSLock()
    private static let maximumBytes = 48_000_000
    private static var storageError: ReviewError {
        .storage("The private workflow cache could not be read or saved. Keep it for recovery. Its directory must use mode 0700 and its files mode 0600, belong to your account, and not be symbolic links.")
    }

    public init(directory: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Job Review"), origin: String) throws {
        guard directory.isFileURL, !directory.path.contains("\0") else { throw Self.storageError }
        self.directory = directory
        self.origin = origin
        let digest = SHA256.hash(data: Data(origin.utf8)).map { String(format: "%02x", $0) }.joined()
        file = directory.appendingPathComponent("workflow-" + digest + ".json")
        do {
            if !PrivateFile.exists(directory) {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                       attributes: [.posixPermissions: 0o700])
            }
            let descriptor = try openDirectory()
            defer { close(descriptor) }
            try checkExistingFile(directoryDescriptor: descriptor)
        } catch { throw Self.storageError }
    }

    public func load() throws -> SavedWorkflowSession? {
        lock.lock(); defer { lock.unlock() }
        do {
            let folder = try openDirectory(); defer { close(folder) }
            let descriptor = file.lastPathComponent.withCString { openat(folder, $0, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK) }
            guard descriptor >= 0 else {
                if errno == ENOENT { return nil }
                throw Self.storageError
            }
            defer { close(descriptor) }
            var info = stat()
            guard fstat(descriptor, &info) == 0, Self.isPrivateFile(info),
                  info.st_size >= 0, info.st_size <= Self.maximumBytes else { throw Self.storageError }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 16_384)
            while true {
                let count = Darwin.read(descriptor, &buffer, buffer.count)
                if count < 0 && errno == EINTR { continue }
                guard count >= 0 else { throw Self.storageError }
                if count == 0 { break }
                guard data.count + count <= Self.maximumBytes else { throw Self.storageError }
                data.append(contentsOf: buffer.prefix(count))
            }
            let saved = try JSONDecoder().decode(SavedWorkflowSession.self, from: data)
            try validate(saved)
            return saved
        } catch { throw Self.storageError }
    }

    public func save(_ value: SavedWorkflowSession) throws {
        lock.lock(); defer { lock.unlock() }
        do {
            try validate(value)
            let data = try JSONEncoder().encode(value)
            guard data.count <= Self.maximumBytes else { throw Self.storageError }
            let folder = try openDirectory(); defer { close(folder) }
            try checkExistingFile(directoryDescriptor: folder)
            let temporary = ".workflow-\(UUID().uuidString).tmp"
            let descriptor = temporary.withCString { openat(folder, $0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600) }
            guard descriptor >= 0 else { throw Self.storageError }
            defer {
                close(descriptor)
                // The successful rename already removed this name. On failure, remove only our temporary file.
                _ = temporary.withCString { unlinkat(folder, $0, 0) }
            }
            guard fchmod(descriptor, 0o600) == 0 else { throw Self.storageError }
            try data.withUnsafeBytes { bytes in
                var offset = 0
                while offset < bytes.count {
                    let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                    if count < 0 && errno == EINTR { continue }
                    guard count > 0 else { throw Self.storageError }
                    offset += count
                }
            }
            guard fsync(descriptor) == 0 else { throw Self.storageError }
            try checkExistingFile(directoryDescriptor: folder)
            let moved = temporary.withCString { source in
                file.lastPathComponent.withCString { destination in renameat(folder, source, folder, destination) }
            }
            guard moved == 0, fsync(folder) == 0 else { throw Self.storageError }
        } catch { throw Self.storageError }
    }

    private func validate(_ value: SavedWorkflowSession) throws {
        guard value.schemaVersion == 1, value.origin == origin,
              value.pending == nil || value.pending?.origin == origin else { throw Self.storageError }
    }

    private func openDirectory() throws -> Int32 {
        let descriptor = directory.path.withCString { open($0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC) }
        guard descriptor >= 0 else { throw Self.storageError }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
              info.st_mode & 0o7777 == 0o700, info.st_uid == geteuid() else {
            close(descriptor); throw Self.storageError
        }
        return descriptor
    }

    private func checkExistingFile(directoryDescriptor: Int32) throws {
        var info = stat()
        let result = file.lastPathComponent.withCString { fstatat(directoryDescriptor, $0, &info, AT_SYMLINK_NOFOLLOW) }
        if result != 0 {
            guard errno == ENOENT else { throw Self.storageError }
            return
        }
        guard Self.isPrivateFile(info) else { throw Self.storageError }
    }

    private static func isPrivateFile(_ info: stat) -> Bool {
        info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG) && info.st_mode & 0o7777 == 0o600 &&
            info.st_uid == geteuid() && info.st_nlink == 1
    }
}
