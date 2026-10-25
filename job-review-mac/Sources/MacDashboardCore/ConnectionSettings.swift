import Darwin
import Foundation

public struct ConnectionSettings: Equatable, Sendable {
    public let origin: String
    public let credentialFile: String

    /// HTTP is available only in DEBUG builds, with explicit loopback test opt-in.
    public init(origin: String, credentialFile: String, allowHTTPForTesting: Bool = false) throws {
        guard var parts = URLComponents(string: origin.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(), let host = parts.host?.lowercased(), !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/", parts.url != nil,
              parts.port == nil || (1...65535).contains(parts.port!),
              credentialFile.hasPrefix("/"), !credentialFile.contains("\0") else {
            throw DashboardError.configuration
        }
        var allowed = scheme == "https"
        #if DEBUG
        allowed = allowed || (allowHTTPForTesting && scheme == "http" &&
                              ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host))
        #endif
        guard allowed else { throw DashboardError.configuration }
        parts.scheme = scheme
        parts.host = host
        parts.path = ""
        if (scheme == "https" && parts.port == 443) || (scheme == "http" && parts.port == 80) { parts.port = nil }
        guard let normalized = parts.url?.absoluteString else { throw DashboardError.configuration }
        self.origin = normalized
        self.credentialFile = credentialFile
    }

    /// Explicit settings win. Invalid explicit settings never fall back to another service.
    public static func loadDefault() throws -> ConnectionSettings {
        try loadDefault(homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
                        executableURL: Bundle.main.executableURL,
                        currentDirectory: URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
    }

    static func loadDefault(homeDirectory: URL, executableURL: URL?, currentDirectory: URL) throws -> ConnectionSettings {
        let config = homeDirectory.appendingPathComponent(".config/job-review-mac/connection.json")
        if PrivateFile.exists(config) {
            struct Configuration: Decodable {
                let origin: String
                let credentialFile: String
                enum CodingKeys: String, CodingKey { case origin; case credentialFile = "credential_file" }
            }
            do {
                let value = try JSONDecoder().decode(Configuration.self, from: PrivateFile.read(config, limit: 65_536))
                let path = (value.credentialFile as NSString).expandingTildeInPath
                let settings = try ConnectionSettings(origin: value.origin, credentialFile: path)
                _ = try settings.readCredential()
                return settings
            } catch { throw DashboardError.configuration }
        }

        var roots = [URL]()
        for start in [executableURL?.deletingLastPathComponent(), currentDirectory].compactMap({ $0 }) {
            var cursor = start.standardizedFileURL
            for _ in 0..<12 {
                roots.append(cursor)
                let parent = cursor.deletingLastPathComponent()
                if parent.path == cursor.path { break }
                cursor = parent
            }
        }
        roots.append(homeDirectory.appendingPathComponent("ml-workspace/job-hunting"))
        var seen = Set<String>()
        for root in roots {
            let directory = root.appendingPathComponent("job-review/state/device")
            guard seen.insert(directory.path).inserted else { continue }
            let access = directory.appendingPathComponent("tailscale-access.json")
            guard PrivateFile.exists(access) else { continue }
            struct Access: Decodable {
                let schemaVersion: Int
                let publicOrigin: String
                enum CodingKeys: String, CodingKey { case schemaVersion = "schema_version", publicOrigin = "public_origin" }
            }
            do {
                let value = try JSONDecoder().decode(Access.self, from: PrivateFile.read(access, limit: 65_536))
                guard value.schemaVersion == 1 else { throw DashboardError.configuration }
                let settings = try ConnectionSettings(origin: value.publicOrigin,
                                                      credentialFile: directory.appendingPathComponent("tailscale-credential.txt").path)
                _ = try settings.readCredential()
                return settings
            } catch { throw DashboardError.configuration }
        }
        throw DashboardError.configuration
    }

    func readCredential() throws -> String {
        let data = try PrivateFile.read(URL(fileURLWithPath: credentialFile), limit: 1_024)
        guard let value = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              value.utf8.count == 43,
              value.utf8.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95 }) else {
            throw DashboardError.configuration
        }
        return value
    }
}

/// Open without following a final symlink, then inspect and read the same file descriptor.
enum PrivateFile {
    static func exists(_ url: URL) -> Bool {
        var info = stat()
        return url.path.withCString { lstat($0, &info) } == 0
    }

    static func read(_ url: URL, limit: Int) throws -> Data {
        let descriptor = url.path.withCString { open($0, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK) }
        guard descriptor >= 0 else { throw DashboardError.configuration }
        defer { close(descriptor) }
        var info = stat()
        guard fstat(descriptor, &info) == 0,
              info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              info.st_mode & 0o7777 == 0o600, info.st_uid == geteuid(),
              info.st_size >= 0, info.st_size <= limit else { throw DashboardError.configuration }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count < 0 && errno == EINTR { continue }
            guard count >= 0 else { throw DashboardError.configuration }
            if count == 0 { break }
            guard result.count + count <= limit else { throw DashboardError.configuration }
            result.append(contentsOf: buffer.prefix(count))
        }
        return result
    }
}
