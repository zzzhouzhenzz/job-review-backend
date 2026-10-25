import Foundation
import CryptoKit
import Darwin

public enum LocalServerState: String, Sendable { case stopped, registered, running }
public struct LocalServerStatus: Equatable, Sendable {
    public let state: LocalServerState
    public init(_ state: LocalServerState) { self.state = state }
}
public protocol LocalServerServing: Sendable {
    func status() async throws -> LocalServerStatus
    func start() async throws -> LocalServerStatus
    func stop() async throws -> LocalServerStatus
}

struct LocalServerError: LocalizedError, Equatable {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// The sole supported service is the existing current-user Job Review LaunchAgent.
struct LocalServerConfiguration: Sendable {
    static let label = "app.jobreview.backend"
    let plist: String
    let arguments: [String]
    let uid: uid_t
    var domain: String { "gui/\(uid)" }
    var target: String { "\(domain)/\(Self.label)" }
    var script: String { arguments[2] }

    static func load() throws -> Self {
        try load(home: FileManager.default.homeDirectoryForCurrentUser, settings: ConnectionSettings.loadDefault())
    }
    static func load(home: URL, settings: ConnectionSettings) throws -> Self {
        let plist = home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
        guard let value = try? PropertyListSerialization.propertyList(from: PrivateFile.read(plist, limit: 65_536), format: nil) as? [String: Any],
              value["Label"] as? String == label, let directory = value["WorkingDirectory"] as? String,
              directory.hasPrefix("/"), let args = value["ProgramArguments"] as? [String],
              value["Program"] == nil, value["RootDirectory"] == nil, value["EnvironmentVariables"] == nil,
              value["RunAtLoad"] as? Bool == true, value["KeepAlive"] as? Bool == true,
              value["EnablePressuredExit"] as? Bool != true, value["EnableTransactions"] as? Bool != true,
              (value["ExitTimeOut"] as? Int ?? 0) >= 60 else {
            throw LocalServerError("The local Job Review LaunchAgent is missing or does not match the supported installation.")
        }
        let root = URL(fileURLWithPath: directory).standardizedFileURL
        let script = root.appendingPathComponent("tailscale_backend.py")
        let access = root.appendingPathComponent("state/device/tailscale-access.json")
        let credential = root.appendingPathComponent("state/device/tailscale-credential.txt")
        let database = root.appendingPathComponent("state/review.sqlite3")
        let runtime = root.appendingPathComponent("state/device/runtime.prepared.json")
        let projectPython = root.appendingPathComponent(".venv-pool/bin/python")
        guard let python = args.first, ["/usr/bin/python3", projectPython.path].contains(python) else {
            throw LocalServerError("The local server uses an unsupported Python installation. Local controls are unavailable.")
        }
        let expected = [python, "-B", script.path, "run", "--db", database.path,
                        "--access-config", access.path, "--credential-file", credential.path, "--port", "8766"]
        let withRuntime = expected + ["--runtime-config", runtime.path]
        let usesRuntime = args == withRuntime || args == withRuntime + ["--no-workers"]
        guard args == expected || args == expected + ["--no-workers"] || usesRuntime,
              settings.credentialFile == credential.path,
              root.resolvingSymlinksInPath().path == root.path else {
            throw LocalServerError("This connection is not the verified local Job Review server. Local controls are unavailable.")
        }
        if python == projectPython.path { try validateProjectPython(projectPython, root: root) }
        for file in [script, database] + (usesRuntime ? [runtime] : []) {
            var info = stat()
            guard file.resolvingSymlinksInPath().path == file.path,
                  lstat(file.path, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
                  info.st_uid == geteuid(), info.st_mode & 0o022 == 0 else {
                throw LocalServerError("A required local server file is missing or has unsafe ownership or permissions.")
            }
        }
        struct Access: Decodable { let schema_version: Int; let public_origin: String; let token_sha256: String }
        guard let binding = try? JSONDecoder().decode(Access.self, from: PrivateFile.read(access, limit: 65_536)),
              binding.schema_version == 1,
              let localSettings = try? ConnectionSettings(origin: binding.public_origin, credentialFile: credential.path),
              settings == localSettings, let token = try? settings.readCredential(),
              SHA256.hash(data: Data(token.utf8)).map({ String(format: "%02x", $0) }).joined() == binding.token_sha256 else {
            throw LocalServerError("The app connection does not match the local server's private access configuration.")
        }
        return Self(plist: plist.path, arguments: args, uid: geteuid())
    }

    /// Virtual environments normally link their interpreter to the installed Python.
    /// Keep the environment at its exact local path and validate both it and the resolved binary.
    private static func validateProjectPython(_ python: URL, root: URL) throws {
        let environment = root.appendingPathComponent(".venv-pool")
        for (file, type) in [(environment, S_IFDIR), (environment.appendingPathComponent("bin"), S_IFDIR),
                             (environment.appendingPathComponent("pyvenv.cfg"), S_IFREG)] {
            var info = stat()
            guard file.resolvingSymlinksInPath().path == file.path,
                  lstat(file.path, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(type),
                  info.st_uid == geteuid(), info.st_mode & 0o022 == 0 else {
                throw LocalServerError("The local server's Python environment has unsafe ownership or permissions.")
            }
        }
        var info = stat()
        let resolved = python.resolvingSymlinksInPath()
        guard lstat(resolved.path, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              info.st_uid == 0 || info.st_uid == geteuid(), info.st_mode & 0o022 == 0,
              access(python.path, X_OK) == 0 else {
            throw LocalServerError("The local server's Python executable is missing or has unsafe ownership or permissions.")
        }
    }
}

/// No shell, sudo, server API, or direct Python launch. A fresh local check precedes every change.
public actor LocalServerController: LocalServerServing {
    private let configuration: @Sendable () throws -> LocalServerConfiguration
    private let runner: any LocalCommandRunning
    private var busy = false
    public init() { configuration = { try LocalServerConfiguration.load() }; runner = LocalCommandRunner() }
    init(configuration: @escaping @Sendable () throws -> LocalServerConfiguration, runner: any LocalCommandRunning) {
        self.configuration = configuration; self.runner = runner
    }
    public func status() async throws -> LocalServerStatus {
        guard !busy else { throw LocalServerError("A server operation is already in progress.") }
        busy = true; defer { busy = false }
        return try await inspect(configuration())
    }
    public func start() async throws -> LocalServerStatus {
        guard !busy else { throw LocalServerError("A server operation is already in progress.") }
        busy = true; defer { busy = false }
        let config = try configuration()
        let current = try await inspect(config)
        guard current.state == .stopped else { return current }
        let result = try await runner.run("/bin/launchctl", ["bootstrap", config.domain, config.plist], timeout: 15)
        guard result.code == 0 else { throw LocalServerError("Start server failed (launchctl exit \(result.code)). Refresh status before trying again.") }
        // Registration is not readiness. Report the observed process/listener, never HTTPS reachability.
        for _ in 0..<20 {
            let state = try await inspect(config)
            if state.state == .running { return state }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        return try await inspect(config)
    }
    public func stop() async throws -> LocalServerStatus {
        guard !busy else { throw LocalServerError("A server operation is already in progress.") }
        busy = true; defer { busy = false }
        let config = try configuration()
        let current = try await inspect(config)
        guard current.state != .stopped else { return current }
        let deadline = ContinuousClock.now.advanced(by: .seconds(65))
        // bootout sends SIGTERM and removes KeepAlive. The installed agent allows 60 seconds for cleanup.
        let result = try await runner.run("/bin/launchctl", ["bootout", config.target], timeout: 75)
        guard result.code == 0 else { throw LocalServerError("Stop server failed (launchctl exit \(result.code)). Refresh status to check whether the server is still running.") }
        var lastError: Error?
        repeat {
            do {
                let state = try await inspect(config)
                if state.state == .stopped { return state }
            } catch { lastError = error }
            try await Task.sleep(nanoseconds: 250_000_000)
        } while ContinuousClock.now < deadline
        throw lastError ?? LocalServerError("Shutdown has not been confirmed. Refresh status before taking another action.")
    }

    private func inspect(_ config: LocalServerConfiguration) async throws -> LocalServerStatus {
        let registration = try await runner.run("/bin/launchctl", ["print", config.target], timeout: 5)
        let missing = registration.code == 113 && registration.output.contains("Could not find service \"\(LocalServerConfiguration.label)\"")
        guard registration.code == 0 || missing else { throw LocalServerError("Local launchd status could not be read (exit \(registration.code)).") }
        let processes = try await runner.run("/bin/ps", ["-ww", "-axo", "pid=,uid=,command="], timeout: 5)
        guard processes.code == 0 else { throw LocalServerError("Local process identity could not be checked.") }
        let rows = processes.output.split(separator: "\n").map { $0.split(maxSplits: 2, whereSeparator: { $0.isWhitespace }).map(String.init) }
        let backendRows = rows.filter { $0.count == 3 && $0[2].contains(config.script + " run ") }
        let port = try await runner.run("/usr/sbin/lsof", ["-nP", "-iTCP:8766", "-sTCP:LISTEN", "-Fp"], timeout: 5)
        guard port.code == 0 || (port.code == 1 && port.output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) else {
            throw LocalServerError("The local server port could not be checked.")
        }
        let listeners = Set(port.output.split(separator: "\n").compactMap { $0.first == "p" ? Int($0.dropFirst()) : nil })
        guard port.code != 0 || !listeners.isEmpty else { throw LocalServerError("The local listener identity is unavailable.") }
        if missing {
            guard backendRows.isEmpty, listeners.isEmpty else {
                throw LocalServerError("The agent is unloaded, but a backend process or port 8766 is still active. Local controls are disabled to avoid a second server.")
            }
            return LocalServerStatus(.stopped)
        }
        let lines = registration.output.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.contains("path = \(config.plist)"), let begin = lines.firstIndex(of: "arguments = {"),
              let end = lines[(begin + 1)...].firstIndex(of: "}"), Array(lines[(begin + 1)..<end]) == config.arguments else {
            throw LocalServerError("The loaded service does not match the verified local Job Review LaunchAgent.")
        }
        let pid = lines.first(where: { $0.hasPrefix("pid = ") }).flatMap { Int($0.dropFirst(6)) }
        guard let pid else {
            guard backendRows.isEmpty, listeners.isEmpty else { throw LocalServerError("The local service identity is inconsistent. Refresh status.") }
            return LocalServerStatus(.registered)
        }
        guard backendRows.count == 1, backendRows[0][0] == String(pid), backendRows[0][1] == String(config.uid),
              backendRows[0][2].hasSuffix(" " + config.arguments.dropFirst().joined(separator: " ")),
              listeners.isSubset(of: [pid]) else {
            throw LocalServerError("The server process or listener does not match the registered current-user service.")
        }
        return LocalServerStatus(listeners.isEmpty ? .registered : .running)
    }
}
