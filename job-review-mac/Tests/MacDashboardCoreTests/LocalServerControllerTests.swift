import XCTest
import CryptoKit
@testable import MacDashboardCore

final class LocalServerControllerTests: XCTestCase {
    private let config = LocalServerConfiguration(plist: "/fictional/LaunchAgents/app.jobreview.backend.plist",
        arguments: ["/usr/bin/python3", "-B", "/fictional/job-review/tailscale_backend.py", "run", "--db", "/fictional/review.sqlite3"], uid: 501)

    func testStoppedRequiresAbsentAgentProcessAndListener() async throws {
        let runner = ServerCommandFixture(config: config)
        let client = LocalServerController(configuration: { self.config }, runner: runner)
        let status = try await client.status()
        XCTAssertEqual(status.state, .stopped)
        let mutations = await runner.mutations
        XCTAssertTrue(mutations.isEmpty)
    }
    func testStartUsesOnlyExistingAgentAndStopUnloadsExactService() async throws {
        let runner = ServerCommandFixture(config: config)
        let client = LocalServerController(configuration: { self.config }, runner: runner)
        let started = try await client.start()
        XCTAssertEqual(started.state, .running)
        let alreadyStarted = try await client.start()
        XCTAssertEqual(alreadyStarted.state, .running)
        let stopped = try await client.stop()
        XCTAssertEqual(stopped.state, .stopped)
        _ = try await client.stop()
        let commands = await runner.mutations
        XCTAssertEqual(commands, [["bootstrap", "gui/501", config.plist], ["bootout", "gui/501/app.jobreview.backend"]])
        let executables = await runner.executables
        XCTAssertTrue(Set(executables).isSubset(of: ["/bin/launchctl", "/bin/ps", "/usr/sbin/lsof"]))
    }
    func testRegistrationWithoutListenerIsNotRunningAndCanBeUnloaded() async throws {
        let runner = ServerCommandFixture(config: config, state: "registered")
        let client = LocalServerController(configuration: { self.config }, runner: runner)
        let status = try await client.status()
        XCTAssertEqual(status.state, .registered)
        let stopped = try await client.stop()
        XCTAssertEqual(stopped.state, .stopped)
    }
    func testAmbiguousServiceProcessPortAndReadFailuresNeverMutate() async throws {
        for mode in ["orphan", "foreign-port", "wrong-owner", "wrong-arguments", "wrong-agent", "launchctl-error", "ps-error", "lsof-error"] {
            let runner = ServerCommandFixture(config: config, state: mode)
            let client = LocalServerController(configuration: { self.config }, runner: runner)
            do { _ = try await client.start(); XCTFail(mode) } catch { XCTAssertTrue(error is LocalServerError) }
            do { _ = try await client.stop(); XCTFail(mode) } catch { XCTAssertTrue(error is LocalServerError) }
            let mutations = await runner.mutations
            XCTAssertTrue(mutations.isEmpty, mode)
        }
    }
    func testFailedStartAndStopSurfaceExitStatus() async throws {
        for initial in ["stopped", "running"] {
            let runner = ServerCommandFixture(config: config, state: initial)
            await runner.failMutation()
            let client = LocalServerController(configuration: { self.config }, runner: runner)
            do {
                _ = try await (initial == "stopped" ? client.start() : client.stop())
                XCTFail("Expected failure")
            } catch { XCTAssertTrue(error.localizedDescription.contains("exit 5")) }
            let status = try await client.status()
            XCTAssertEqual(status.state, initial == "stopped" ? .stopped : .running)
        }
    }
    func testStopWaitsForProcessAndPortAfterBootoutReturns() async throws {
        let runner = ServerCommandFixture(config: config, state: "running")
        await runner.delayShutdown()
        let client = LocalServerController(configuration: { self.config }, runner: runner)
        let status = try await client.stop()
        XCTAssertEqual(status.state, .stopped)
        let checks = await runner.shutdownChecks
        XCTAssertEqual(checks, 26, "Shutdown must remain pending beyond the former five-second polling window")
    }
    func testConcurrentRefreshOrStartCannotIssueAnotherCommand() async throws {
        let runner = ServerCommandFixture(config: config)
        await runner.holdMutation()
        let client = LocalServerController(configuration: { self.config }, runner: runner)
        let first = Task { try await client.start() }
        await runner.waitUntilHeld()
        do { _ = try await client.start(); XCTFail("Must reject concurrent start") } catch {}
        do { _ = try await client.status(); XCTFail("Must not race the operation") } catch {}
        do { _ = try await client.stop(); XCTFail("Must not race the operation") } catch {}
        await runner.release()
        _ = try await first.value
        let mutations = await runner.mutations
        XCTAssertEqual(mutations.count, 1)
    }
    func testUnavailableConfigurationNeverExecutesAHelper() async {
        let runner = ServerCommandFixture(config: config)
        let client = LocalServerController(configuration: { throw LocalServerError("Missing configuration") }, runner: runner)
        do { _ = try await client.start(); XCTFail() } catch {}
        do { _ = try await client.stop(); XCTFail() } catch {}
        let executables = await runner.executables
        XCTAssertTrue(executables.isEmpty)
    }
}

private actor ServerCommandFixture: LocalCommandRunning {
    let config: LocalServerConfiguration
    var state: String
    var mutationFails = false
    var delayed = false
    var hold = false
    var gate: CheckedContinuation<Void, Never>?
    var observer: CheckedContinuation<Void, Never>?
    private(set) var mutations: [[String]] = []
    private(set) var executables: [String] = []
    private(set) var shutdownChecks = 0
    init(config: LocalServerConfiguration, state: String = "stopped") { self.config = config; self.state = state }
    func failMutation() { mutationFails = true }
    func delayShutdown() { delayed = true }
    func holdMutation() { hold = true }
    func waitUntilHeld() async { if gate == nil { await withCheckedContinuation { observer = $0 } } }
    func release() { gate?.resume(); gate = nil; hold = false }
    func run(_ executable: String, _ args: [String], timeout: TimeInterval) async throws -> LocalCommandResult {
        executables.append(executable)
        if args.first == "bootstrap" || args.first == "bootout" {
            mutations.append(args)
            if hold { await withCheckedContinuation { gate = $0; observer?.resume(); observer = nil } }
            if mutationFails { return .init(code: 5, output: "Fictional failure") }
            state = args.first == "bootstrap" ? "running" : delayed ? "draining" : "stopped"
            return .init(code: 0, output: "")
        }
        if args.first == "print" {
            if state == "draining" { shutdownChecks += 1; if shutdownChecks == 26 { state = "stopped" } }
            if state == "launchctl-error" { return .init(code: 1, output: "Permission denied") }
            if ["stopped", "orphan", "foreign-port", "draining"].contains(state) {
                return .init(code: 113, output: "Could not find service \"app.jobreview.backend\" in domain for user gui: 501")
            }
            return .init(code: 0, output: "path = \(state == "wrong-agent" ? "/foreign.plist" : config.plist)\narguments = {\n" + config.arguments.joined(separator: "\n") + "\n}\n" + (state == "registered" ? "" : "pid = 321\n"))
        }
        if executable == "/bin/ps" {
            if state == "ps-error" { return .init(code: 1, output: "Denied") }
            if ["stopped", "registered", "foreign-port"].contains(state) { return .init(code: 0, output: "100 501 /fictional/app") }
            return .init(code: 0, output: "321 \(state == "wrong-owner" ? 502 : 501) " + config.arguments.joined(separator: " ") + (state == "wrong-arguments" ? " --other" : ""))
        }
        if state == "lsof-error" { return .init(code: 1, output: "Denied") }
        if ["stopped", "registered"].contains(state) { return .init(code: 1, output: "") }
        return .init(code: 0, output: state == "foreign-port" ? "p999\n" : "p321\n")
    }
}

final class LocalServerConfigurationTests: XCTestCase {
    var home: URL!
    var agent: URL!
    var value: [String: Any] = [:]
    var settings: ConnectionSettings!
    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("mac-server-test-\(UUID())")
        let root = home.appendingPathComponent("job-review")
        agent = home.appendingPathComponent("Library/LaunchAgents/app.jobreview.backend.plist")
        try FileManager.default.createDirectory(at: agent.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("state/device"), withIntermediateDirectories: true)
        let token = String(repeating: "T", count: 43)
        settings = try ConnectionSettings(origin: "https://local-server.example.test", credentialFile: root.appendingPathComponent("state/device/tailscale-credential.txt").path)
        let access: [String: Any] = ["schema_version": 1, "public_origin": settings.origin, "token_sha256": SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()]
        for (name, data) in [("tailscale_backend.py", Data()), ("state/review.sqlite3", Data()),
                             ("state/device/tailscale-access.json", try JSONSerialization.data(withJSONObject: access)),
                             ("state/device/tailscale-credential.txt", Data(token.utf8))] {
            let file = root.appendingPathComponent(name); try data.write(to: file)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
        value = ["Label": "app.jobreview.backend", "WorkingDirectory": root.path, "RunAtLoad": true, "KeepAlive": true, "ExitTimeOut": 60,
            "ProgramArguments": ["/usr/bin/python3", "-B", root.appendingPathComponent("tailscale_backend.py").path, "run", "--db", root.appendingPathComponent("state/review.sqlite3").path,
                                 "--access-config", root.appendingPathComponent("state/device/tailscale-access.json").path, "--credential-file", settings.credentialFile, "--port", "8766"]]
        try writeAgent()
    }
    func writeAgent() throws {
        try PropertyListSerialization.data(fromPropertyList: value, format: .xml, options: 0).write(to: agent)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: agent.path)
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: home) }
    func testExactLocalInstallationPassesWithoutReadingDatabaseContents() throws {
        let config = try LocalServerConfiguration.load(home: home, settings: settings)
        XCTAssertEqual(config.plist, agent.path); XCTAssertEqual(config.uid, getuid())
    }
    private func makeProjectPython() throws -> String {
        let root = home.appendingPathComponent("job-review")
        let environment = root.appendingPathComponent(".venv-pool")
        try FileManager.default.createDirectory(at: environment.appendingPathComponent("bin"), withIntermediateDirectories: true)
        let interpreter = root.appendingPathComponent("fixture-python3")
        try Data().write(to: interpreter)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: interpreter.path)
        try Data("home = /fictional/python\n".utf8).write(to: environment.appendingPathComponent("pyvenv.cfg"))
        let python = environment.appendingPathComponent("bin/python")
        try FileManager.default.createSymbolicLink(at: python, withDestinationURL: interpreter)
        return python.path
    }
    func testSystemAndProjectPythonSupportRuntimeAndAPIOnlyModes() async throws {
        let original = try XCTUnwrap(value["ProgramArguments"] as? [String])
        let projectPython = try makeProjectPython()
        let runtime = home.appendingPathComponent("job-review/state/device/runtime.prepared.json")
        try Data("{}".utf8).write(to: runtime)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: runtime.path)
        for executable in ["/usr/bin/python3", projectPython] {
            for suffix in [[], ["--no-workers"], ["--runtime-config", runtime.path], ["--runtime-config", runtime.path, "--no-workers"]] {
                var arguments = original; arguments[0] = executable; arguments += suffix
                value["ProgramArguments"] = arguments; try writeAgent()
                let config = try LocalServerConfiguration.load(home: home, settings: settings)
                XCTAssertEqual(config.arguments, arguments, "Keep the installed mode unchanged")
                let runner = ServerCommandFixture(config: config)
                let controller = LocalServerController(configuration: { config }, runner: runner)
                let started = try await controller.start()
                let stopped = try await controller.stop()
                XCTAssertEqual(started.state, .running); XCTAssertEqual(stopped.state, .stopped)
                let mutations = await runner.mutations
                XCTAssertEqual(mutations, [["bootstrap", config.domain, config.plist], ["bootout", config.target]])
            }
        }
    }
    func testUnknownInterpreterPathsAndChangedArgumentsFailClosed() throws {
        let original = try XCTUnwrap(value["ProgramArguments"] as? [String])
        let root = home.appendingPathComponent("job-review")
        var shell = original; shell[0] = "/bin/sh"
        var otherEnvironment = original; otherEnvironment[0] = root.appendingPathComponent(".venv-other/bin/python").path
        var otherDatabase = original; otherDatabase[5] = "/foreign/review.sqlite3"
        var otherPort = original; otherPort[11] = "9999"
        for arguments in [shell, otherEnvironment, otherDatabase, otherPort,
                          original + ["--no-workers", "--no-workers"],
                          original + ["--runtime-config", "/foreign/runtime.json"],
                          original + ["--unknown"]] {
            value["ProgramArguments"] = arguments; try writeAgent()
            XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings))
        }
    }
    func testUnsafeProjectEnvironmentFailsClosed() throws {
        var arguments = try XCTUnwrap(value["ProgramArguments"] as? [String])
        arguments[0] = try makeProjectPython()
        value["ProgramArguments"] = arguments; try writeAgent()
        let root = home.appendingPathComponent("job-review")
        for relative in [".venv-pool", ".venv-pool/bin", ".venv-pool/pyvenv.cfg", "fixture-python3"] {
            let path = root.appendingPathComponent(relative).path
            let original = try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions]
            try FileManager.default.setAttributes([.posixPermissions: 0o777], ofItemAtPath: path)
            XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings), relative)
            try FileManager.default.setAttributes([.posixPermissions: try XCTUnwrap(original)], ofItemAtPath: path)
        }
        let configuration = root.appendingPathComponent(".venv-pool/pyvenv.cfg")
        try FileManager.default.removeItem(at: configuration)
        try FileManager.default.createSymbolicLink(at: configuration, withDestinationURL: root.appendingPathComponent("fixture-python3"))
        XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings))
    }
    func testAPIOnlyWithoutRuntimeDoesNotRequireRuntimeFile() throws {
        value["ProgramArguments"] = try XCTUnwrap(value["ProgramArguments"] as? [String]) + ["--no-workers"]
        try writeAgent()
        XCTAssertNoThrow(try LocalServerConfiguration.load(home: home, settings: settings))
    }
    func testRemoteConnectionAndCredentialMismatchFailClosed() throws {
        let remote = try ConnectionSettings(origin: "https://remote.example.test", credentialFile: settings.credentialFile)
        XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: remote))
        try Data(String(repeating: "X", count: 43).utf8).write(to: URL(fileURLWithPath: settings.credentialFile))
        XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings))
    }
    func testMissingUnsafeAndChangedLaunchAgentFailClosed() throws {
        let original = value
        for (key, replacement) in [("Label", "foreign" as Any), ("Program", "/bin/sh"), ("EnvironmentVariables", ["PYTHONPATH": "/foreign"]), ("ExitTimeOut", 1), ("KeepAlive", false)] {
            value = original; value[key] = replacement; try writeAgent()
            XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings), key)
        }
        value = original; try writeAgent()
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: agent.path)
        XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings))
        try FileManager.default.removeItem(at: agent)
        XCTAssertThrowsError(try LocalServerConfiguration.load(home: home, settings: settings))
    }
}
