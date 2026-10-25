import Foundation
import ReviewCore

private struct WorkflowFixture: Decodable, Sendable {
    struct Request: Decodable, Sendable {
        var method: String
        var path: String
        var body: [String: JSONValue]?
    }
    struct Response: Decodable, Sendable {
        var status: Int
        var body: [String: JSONValue]
        func result() throws -> HTTPResult { HTTPResult(status: status, body: try JSONEncoder().encode(body)) }
    }
    var name: String
    var request: Request
    var response: Response
}

private struct WorkflowFixtures: Decodable, Sendable {
    var token: String
    var sequence: [WorkflowFixture]
    var errorExamples: [String: WorkflowFixture.Response]
    enum CodingKeys: String, CodingKey { case token, sequence; case errorExamples = "error_examples" }
    static var fileURL: URL {
        let repository = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return ProcessInfo.processInfo.environment["JOB_REVIEW_WORKFLOW_FIXTURES"].map { URL(fileURLWithPath: $0) }
            ?? repository.appendingPathComponent("job-review/fixtures/v1-http.json")
    }
    static func load() throws -> Self { try JSONDecoder().decode(Self.self, from: Data(contentsOf: fileURL)) }
    func item(_ name: String) throws -> WorkflowFixture { try require(sequence.first { $0.name == name }) }
    func body(_ name: String) throws -> [String: JSONValue] { try item(name).response.body }
}

private struct HealthFixtures: Decodable {
    var sequence: [WorkflowFixture]
    static func load() throws -> Self {
        try JSONDecoder().decode(Self.self, from: Data(contentsOf: WorkflowFixtures.fileURL.deletingLastPathComponent().appendingPathComponent("health.json")))
    }
    func body(_ name: String) throws -> [String: JSONValue] {
        try require(sequence.first { $0.name == name }).response.body
    }
}

private struct FixedWorkflowTransport: ReviewTransport {
    let response: HTTPResult
    var expectedAuthorization: String? = nil
    func send(_ request: URLRequest) async throws -> HTTPResult {
        if let expectedAuthorization {
            try check(request.value(forHTTPHeaderField: "Authorization") == expectedAuthorization)
        }
        return response
    }
}

private func unauthorizedWorkflowResponse() -> WorkflowFixture.Response {
    WorkflowFixture.Response(status: 401, body: ["schema_version": .number(1), "error": .object([
        "code": .string("unauthorized"), "message": .string("Authentication required"),
        "reason": .null, "commit_status": .string("not_committed")
    ])])
}

private final class MemoryWorkflowPersistence: WorkflowPersistence, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SavedWorkflowSession?
    private var rejectsWrites = false
    func load() throws -> SavedWorkflowSession? { lock.lock(); defer { lock.unlock() }; return value }
    func save(_ value: SavedWorkflowSession) throws {
        lock.lock(); defer { lock.unlock() }
        if rejectsWrites { throw ReviewError.storage("Synthetic disk failure") }
        self.value = value
    }
    func failWrites() { lock.lock(); defer { lock.unlock() }; rejectsWrites = true }
    func allowWrites() { lock.lock(); defer { lock.unlock() }; rejectsWrites = false }
}

/// A fixture-backed peer that records immutable acknowledgements before optionally losing the response.
private actor WorkflowPeer: ReviewTransport {
    let fixtures: WorkflowFixtures
    let persistence: MemoryWorkflowPersistence
    private var reads: [String: WorkflowFixture.Response] = [:]
    private var failingReads: Set<String> = []
    private var nextRejection: WorkflowFixture.Response?
    private var nextRawRejection: HTTPResult?
    private var feedAfterDecision: [String: JSONValue]?
    private var loseNextResponse = false
    private var failReadsAfterCommit = false
    private var failAcknowledgementSave = false
    private var receipts: [String: (Data, WorkflowFixture.Response)] = [:]
    private var requiredAccessToken: String?
    private(set) var posts: [URLRequest] = []
    private(set) var persistedBeforeEveryPost = true
    var uniqueCommands: Int { receipts.count }

    init(fixtures: WorkflowFixtures, persistence: MemoryWorkflowPersistence) throws {
        self.fixtures = fixtures; self.persistence = persistence
        for name in ["capabilities", "initial_feed", "process_queued", "questions_empty", "tasks_queued"] {
            let fixture = try fixtures.item(name); reads[fixture.request.path] = fixture.response
        }
    }
    func use(_ name: String) throws { let fixture = try fixtures.item(name); reads[fixture.request.path] = fixture.response }
    func use(path: String, body: [String: JSONValue], status: Int = 200) {
        reads[path] = WorkflowFixture.Response(status: status, body: body)
    }
    func failRead(_ path: String) { failingReads.insert(path) }
    func loseResponse() { loseNextResponse = true }
    func loseReadsAfterCommit() { failReadsAfterCommit = true }
    func failAcknowledgementPersistence() { failAcknowledgementSave = true }
    func reject(_ response: WorkflowFixture.Response) { nextRejection = response }
    func reject(_ response: HTTPResult) { nextRawRejection = response }
    func requireAccessToken(_ token: String) { requiredAccessToken = token }
    func useFeedAfterDecision(_ body: [String: JSONValue]) { feedAfterDecision = body }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = try require(request.url?.path)
        if request.httpMethod == "POST" {
            posts.append(request)
            let persistedBody = try persistence.load()?.pending?.body
            persistedBeforeEveryPost = persistedBeforeEveryPost && persistedBody == request.httpBody
        }
        if let requiredAccessToken,
           request.value(forHTTPHeaderField: "Authorization") != "Bearer \(requiredAccessToken)" {
            return try unauthorizedWorkflowResponse().result()
        }
        guard request.httpMethod == "POST" else {
            if failingReads.contains(path) { throw URLError(.notConnectedToInternet) }
            if path == "/api/v1/health", reads[path] == nil {
                return HTTPResult(status: 404, body: Data("{\"error\":\"Older v1 server has no health endpoint.\"}".utf8))
            }
            return try require(reads[path]).result()
        }
        if let rejection = nextRejection { nextRejection = nil; return try rejection.result() }
        if let rejection = nextRawRejection { nextRawRejection = nil; return rejection }
        let data = try require(request.httpBody)
        let command = try JSONDecoder().decode([String: JSONValue].self, from: data)
        let id = try require(command["request_id"]?.text)
        let response: WorkflowFixture.Response
        if let saved = receipts[id] {
            try check(saved.0 == data)
            response = saved.1
        } else {
            var body: [String: JSONValue]
            if path == "/api/v1/decisions" {
                let action = try require(command["action"]?.text)
                body = try fixtures.body(action == "apply" ? "apply_accepted" : "find_more_accepted")
                body["decision_id"] = .string(id); body["card_id"] = command["card_id"]; body["action"] = .string(action)
                if action != "apply" { body["process_ids"] = .array([]) }
                if ["exclude", "pass", "later"].contains(action) { body["task_ids"] = .array([]) }
                if let feedAfterDecision { use(path: "/api/v1/feed", body: feedAfterDecision) }
                else { try use("feed_empty") }
            } else if path == "/api/v1/answers" {
                body = try fixtures.body("answers_accepted"); body["process_ids"] = .array([try require(command["process_id"])])
                try use("questions_answered")
            } else {
                body = try fixtures.body("cancel_accepted"); body["task_ids"] = .array([try require(command["task_id"])])
                try use("tasks_after_cancel")
            }
            body["request_id"] = .string(id)
            response = WorkflowFixture.Response(status: 200, body: body)
            receipts[id] = (data, response)
        }
        if failReadsAfterCommit { failingReads.formUnion(reads.keys.filter { $0 != "/api/v1/capabilities" }) }
        if failAcknowledgementSave { failAcknowledgementSave = false; persistence.failWrites() }
        if loseNextResponse { loseNextResponse = false; throw URLError(.networkConnectionLost) }
        return try response.result()
    }
}

struct WorkflowChecks {
    private let origin = "http://localhost:8973"
    private func fixtures() throws -> WorkflowFixtures { try WorkflowFixtures.load() }
    private func makeSession() throws -> (WorkflowSession, MemoryWorkflowPersistence, WorkflowPeer) {
        let storage = MemoryWorkflowPersistence()
        let peer = try WorkflowPeer(fixtures: fixtures(), persistence: storage)
        return (try session(storage: storage, peer: peer), storage, peer)
    }
    private func session(storage: MemoryWorkflowPersistence, peer: WorkflowPeer, accessToken: String? = "synthetic-access-token") throws -> WorkflowSession {
        try WorkflowSession(api: WorkflowAPI(address: ServerAddress(origin), transport: peer, accessToken: accessToken), persistence: storage)
    }
    private func pending(_ fixture: WorkflowFixture) throws -> PendingSave {
        try PendingSave(origin: origin, path: String(fixture.request.path.dropFirst()), payload: require(fixture.request.body), summary: fixture.name)
    }
    private func decoded<T: Decodable>(_ type: T.Type, name: String, field: String) throws -> T {
        try JSONDecoder().decode(type, from: JSONEncoder().encode(require(fixtures().body(name)[field])))
    }
    private func expectFailure(_ operation: () async throws -> Void) async throws {
        do { try await operation() } catch { return }
        throw CheckFailure("Expected a workflow error")
    }

    private func expectUnauthorized(_ operation: () async throws -> Void) async throws {
        do { try await operation() }
        catch let error as WorkflowHTTPError {
            try check(error.status == 401 && error.code == "unauthorized")
            try check(error.reason == nil && error.commitStatus == "not_committed" && !error.isDefinitiveRejection)
            return
        }
        throw CheckFailure("Expected the workflow authentication error")
    }

    private func readHealth(_ body: [String: JSONValue]) async throws -> WorkflowHealth {
        let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body)), expectedAuthorization: "Bearer synthetic-access-token"), accessToken: "synthetic-access-token")
        let read = try await api.fetchHealth()
        return try require(read?.value)
    }

    private func idleBrowserHealth() throws -> [String: JSONValue] {
        var body = try HealthFixtures.load().body("not_configured")
        var health = try require(body["health"]?.object)
        health["status"] = .string("idle"); health["ready"] = .bool(false)
        health["checked_at"] = .null; health["expires_at"] = .null; health["retry_at"] = .null
        health["checks"] = .array([
            .object(["id": .string("storage"), "status": .string("passed"), "message": .string("Storage is available.")]),
            .object(["id": .string("chrome_attachment"), "status": .string("on_demand"), "message": .string("Checked when a task needs Chrome.")]),
            .object(["id": .string("linkedin_auth"), "status": .string("on_demand"), "message": .string("Checked when a task needs LinkedIn.")])
        ])
        body["health"] = .object(health)
        return body
    }

    func testWorkflowHealthFixturesAndRequiredFields() async throws {
        let fixtures = try HealthFixtures.load()
        let requiredIDs: Set<String> = ["storage", "chrome_attachment", "linkedin_auth"]
        for fixture in fixtures.sequence {
            let health = try await readHealth(fixture.response.body)
            try check(requiredIDs.isSubset(of: Set(health.checks.map(\.id))))
            try check(!health.bootID.isEmpty && health.status == fixture.name)
            try check(health.ready == (fixture.name == "ready"))
        }
        let original = try fixtures.body("not_configured")
        let originalHealth = try require(original["health"]?.object)
        for field in ["boot_id", "status", "ready", "checked_at", "expires_at", "retry_at", "checks"] {
            var health = originalHealth; health.removeValue(forKey: field)
            var body = original; body["health"] = .object(health)
            try await expectFailure { _ = try await readHealth(body) }
        }
        for field in ["boot_id", "status", "ready", "checks"] {
            var health = originalHealth; health[field] = .null
            var body = original; body["health"] = .object(health)
            try await expectFailure { _ = try await readHealth(body) }
        }
        var checking = originalHealth
        checking["status"] = .string("checking")
        checking["checked_at"] = .null; checking["expires_at"] = .null; checking["retry_at"] = .null
        var body = original; body["health"] = .object(checking)
        let started = try await readHealth(body)
        try check(started.checkedAt == nil && started.expiresAt == nil && started.retryAt == nil && !started.ready)
        let originalChecks = try require(originalHealth["checks"]?.array)
        let invalidChecks = requiredIDs.map { missing in originalChecks.filter { $0.object["id"]?.text != missing } }
            + [originalChecks + [originalChecks[0]]]
        for checks in invalidChecks {
            var health = originalHealth; health["checks"] = .array(checks)
            var body = original; body["health"] = .object(health)
            try await expectFailure { _ = try await readHealth(body) }
        }
        for field in ["id", "status", "message"] {
            var item = originalChecks[0].object; item.removeValue(forKey: field)
            var checks = originalChecks; checks[0] = .object(item)
            var health = originalHealth; health["checks"] = .array(checks)
            var body = original; body["health"] = .object(health)
            try await expectFailure { _ = try await readHealth(body) }
        }
        var unknown = originalHealth
        unknown["status"] = .string("future_monitor_status")
        unknown["checks"] = .array(originalChecks + [.object(["id": .string("future_check"), "status": .string("future_check_status"), "message": .string("Future safe explanation")])])
        body["health"] = .object(unknown)
        let future = try await readHealth(body)
        try check(future.status == "future_monitor_status" && future.checks.last?.id == "future_check" && !future.ready)
    }

    func testWorkflowHealthOnDemandAndThreeCheckReadiness() async throws {
        let original = try idleBrowserHealth()
        let health = try await readHealth(original)
        try check(health.status == "idle" && !health.ready && !health.isFresh())
        try check(health.checks.count == 3)
        try check(health.checks.filter { $0.id != "storage" }.allSatisfy { $0.status == "on_demand" })

        var body = original; var value = try require(body["health"]?.object)
        value["status"] = .string("ready"); value["ready"] = .bool(true)
        value["checked_at"] = .string("2026-10-12T23:00:00Z")
        value["expires_at"] = .string("2026-10-12T23:05:00Z")
        body["health"] = .object(value)
        // On-demand checks are not evidence that the browser is ready.
        try await expectFailure { _ = try await readHealth(body) }
        value["checks"] = .array(health.checks.map { .object([
            "id": .string($0.id), "status": .string("passed"), "message": .string("Check passed.")
        ]) })
        body["health"] = .object(value)
        let ready = try await readHealth(body)
        let checked = try require(ISO8601DateFormatter().date(from: "2026-10-12T23:00:00Z"))
        try check(ready.ready && ready.isFresh(at: checked))
        let legacyChecks = ["linkedin_job_read", "explorer_agent", "ranker_agent", "application_agent"].map {
            JSONValue.object(["id": .string($0), "status": .string("passed"), "message": .string("Legacy check passed.")])
        }
        value["checks"] = .array(try require(value["checks"]?.array) + legacyChecks)
        body["health"] = .object(value)
        let legacy = try await readHealth(body)
        try check(legacy.checks.count == 7 && legacy.isFresh(at: checked))
    }

    func testWorkflowIdleBrowserDoesNotHideRankOrTaskFailures() async throws {
        let (session, storage, peer) = try makeSession()
        await peer.use(path: "/api/v1/health", body: try idleBrowserHealth())
        var discovery = try fixtures().body("capabilities")
        var capability = try require(discovery["capabilities"]?.object)
        capability["worker_enabled"] = .bool(true); capability["worker_status"] = .string("idle")
        discovery["capabilities"] = .object(capability)
        await peer.use(path: "/api/v1/capabilities", body: discovery)
        var feed = try fixtures().body("initial_feed"); feed["capabilities"] = .object(capability)
        await peer.use(path: "/api/v1/feed", body: feed)

        var tasks = try fixtures().body("tasks_queued")
        var apply = try require(tasks["tasks"]?.array.first?.object)
        let failure = "Chrome was not available for this task."
        apply["status"] = .string("retry_wait"); apply["error"] = .string(failure)
        apply["attempts"] = .number(1); apply["failure_count"] = .number(1)
        var rank = apply
        rank["id"] = .string("00000000-0000-4000-8000-000000000003")
        rank["kind"] = .string("rank"); rank["status"] = .string("running")
        rank["resource"] = .null; rank["error"] = .null; rank["failure_count"] = .number(0)
        tasks["tasks"] = .array([.object(apply), .object(rank)])
        await peer.use(path: "/api/v1/tasks", body: tasks)

        let view = try await session.refresh()
        try check(view.online && view.readErrors.isEmpty && view.capabilities?.workerEnabled == true)
        try check(view.health?.status == "idle" && view.health?.ready == false)
        let running = try require(view.tasks.first { $0.kind == "rank" })
        let retrying = try require(view.tasks.first { $0.kind == "apply" })
        try check(running.status == "running" && running.resource == nil && running.error == nil)
        try check(retrying.status == "retry_wait" && retrying.error == failure && retrying.failureCount == 1)
        try check(try storage.load()?.tasks == view.tasks)
    }

    func testWorkflowHealthFreshnessExpiresAndRejectsInvalidDates() async throws {
        let fixture = try HealthFixtures.load().body("ready")
        let health = try await readHealth(fixture)
        let formatter = ISO8601DateFormatter()
        func fixtureDate(_ value: String?) throws -> Date {
            let value = try require(value)
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = formatter.date(from: value) { return date }
            formatter.formatOptions = [.withInternetDateTime]
            return try require(formatter.date(from: value))
        }
        let checked = try fixtureDate(health.checkedAt)
        let expires = try fixtureDate(health.expiresAt)
        try check(expires > checked)
        try check(health.isFresh(at: checked))
        try check(health.isFresh(at: expires.addingTimeInterval(-1)))
        try check(!health.isFresh(at: expires))
        try check(!health.isFresh(at: checked.addingTimeInterval(-1)))
        for key in ["checked_at", "expires_at", "retry_at"] {
            var body = fixture; var invalid = try require(body["health"]?.object)
            invalid[key] = .string("not a timestamp")
            body["health"] = .object(invalid)
            try await expectFailure { _ = try await readHealth(body) }
        }
        var stale = health; stale.expiresAt = stale.checkedAt
        try check(!stale.isFresh(at: checked))
        stale = health; stale.status = "stale"
        try check(!stale.isFresh(at: checked))
    }

    func testWorkflowHealth404DoesNotDowngradeV1() async throws {
        let notFound = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 404, body: Data("{}".utf8))))
        let health = try await notFound.fetchHealth()
        try check(health == nil)
        for status in [401, 403, 500] {
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: status, body: Data("{}".utf8))))
            try await expectFailure { _ = try await api.fetchHealth() }
        }
        let (session, _, _) = try makeSession()
        let before = try await session.refresh()
        try check(before.capabilities?.apiVersion == 1 && before.health == nil && before.online)
        try check(before.readErrors["health"] == nil)
        let card = try require(before.feed?.cards.first)
        let after = try await session.decide(card: card, action: "find_more", selected: [])
        try check(after.lastAcknowledgement?.action == "find_more" && after.pending == nil)
    }

    func testWorkflowHealthFailureDoesNotBlockDurableCommands() async throws {
        let (session, storage, peer) = try makeSession()
        let body = try HealthFixtures.load().body("ready")
        await peer.use(path: "/api/v1/health", body: body)
        let before = try await session.refresh()
        try check(before.health?.ready == true)
        try check(try storage.load()?.health == before.health)
        await peer.failRead("/api/v1/health")
        let partial = try await session.refresh()
        try check(partial.online && partial.readErrors["health"] != nil)
        try check(partial.health == before.health && partial.feed == before.feed)
        let card = try require(partial.feed?.cards.first)
        let after = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key])
        try check(after.pending == nil && after.lastAcknowledgement?.accepted == true)
        try check(after.readErrors["health"] != nil)
        let count = await peer.uniqueCommands
        try check(count == 1)
    }

    func testWorkflowHealthCacheAcceptsOlderSessionsAndHasNoToken() async throws {
        let (session, storage, peer) = try makeSession()
        await peer.use(path: "/api/v1/health", body: try HealthFixtures.load().body("not_configured"))
        let before = try await session.refresh()
        let saved = try require(storage.load())
        let bytes = try JSONEncoder().encode(saved)
        try check(!String(decoding: bytes, as: UTF8.self).contains("fixture-session-token"))
        let restored = try self.session(storage: storage, peer: peer)
        let cached = await restored.view()
        try check(cached.health == before.health && !cached.online)
        var oldCache = try JSONDecoder().decode([String: JSONValue].self, from: bytes)
        oldCache.removeValue(forKey: "health")
        let oldSaved = try JSONDecoder().decode(SavedWorkflowSession.self, from: JSONEncoder().encode(oldCache))
        try check(oldSaved.health == nil)
        let oldStorage = MemoryWorkflowPersistence(); try oldStorage.save(oldSaved)
        let oldSession = try self.session(storage: oldStorage, peer: peer)
        let oldView = await oldSession.view()
        try check(oldView.health == nil && oldView.feed == before.feed)
        await peer.use(path: "/api/v1/health", body: [:], status: 404)
        let refreshed = try await restored.refresh()
        try check(refreshed.health == nil && refreshed.capabilities?.apiVersion == 1)
    }

    func testEveryWorkflowHTTPFixture() async throws {
        let fixtures = try fixtures()
        try check(fixtures.sequence.count == 19)
        for fixture in fixtures.sequence {
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: try fixture.response.result(), expectedAuthorization: "Bearer synthetic-access-token"), accessToken: "synthetic-access-token")
            if fixture.request.method == "POST" {
                do {
                    let result = try await api.send(pending(fixture), sessionToken: fixtures.token)
                    try check(fixture.response.status == 200)
                    try check(result.value.accepted)
                    try check(result.value.requestID == fixture.request.body?["request_id"]?.text)
                } catch let error as WorkflowHTTPError {
                    try check(fixture.response.status == error.status)
                    try check(error.code == "conflict")
                    try check(error.isDefinitiveRejection == (fixture.name == "stale_card_conflict"))
                }
            } else {
                switch fixture.request.path {
                case "/api/v1/capabilities":
                    let discovery = try await api.discover()
                    let capability = try require(discovery)
                    try check(!capability.workerEnabled && capability.explicitApply && !capability.remoteAccess)
                case "/api/v1/feed": _ = try await api.fetchFeed()
                case "/api/v1/processes": _ = try await api.fetchProcesses()
                case "/api/v1/questions": _ = try await api.fetchQuestions()
                case "/api/v1/tasks": _ = try await api.fetchTasks()
                default: throw CheckFailure("Fixture route is not covered")
                }
            }
        }
        for response in fixtures.errorExamples.values {
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: try response.result()))
            do { _ = try await api.send(pending(fixtures.item("apply_accepted")), sessionToken: fixtures.token); record("Expected fixture error") }
            catch let error as WorkflowHTTPError {
                try check(error.status == response.status)
                try check(error.isDefinitiveRejection == [400, 404].contains(response.status))
            }
        }
    }

    func testWorkflowDiscoveryFallsBackOnlyFor404() async throws {
        for status in [401, 403, 404, 500] {
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: status, body: Data("{}".utf8))))
            if status == 404 { let found = try await api.discover(); try check(found == nil) }
            else { try await expectFailure { _ = try await api.discover() } }
        }
        let original = try fixtures().body("capabilities")
        for field in ["schema_version", "revision", "complete", "next_cursor", "token", "capabilities"] {
            var body = original; body.removeValue(forKey: field)
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body))))
            try await expectFailure { _ = try await api.discover() }
        }
        var unsupported = original
        var capability = try require(unsupported["capabilities"]?.object)
        capability["api_version"] = .number(2); unsupported["capabilities"] = .object(capability)
        let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(unsupported))))
        try await expectFailure { _ = try await api.discover() }
    }

    func testWorkflowAuthenticationFailureDoesNotDowngradeOrClaimFreshHealth() async throws {
        let (session, storage, peer) = try makeSession()
        await peer.requireAccessToken("synthetic-access-token")
        await peer.use(path: "/api/v1/health", body: try HealthFixtures.load().body("ready"))
        let before = try await session.refresh()
        try check(before.online && before.health?.ready == true && before.readErrors.isEmpty)

        let unauthorized = unauthorizedWorkflowResponse()
        await peer.use(path: "/api/v1/health", body: unauthorized.body, status: unauthorized.status)
        let partial = try await session.refresh()
        try check(partial.online && partial.capabilities?.apiVersion == 1)
        try check(partial.health == before.health && partial.readErrors["health"] != nil)

        await peer.requireAccessToken("replacement-access-token")
        try await expectUnauthorized { _ = try await session.refresh() }
        let offline = await session.view()
        try check(!offline.online && offline.capabilities?.apiVersion == 1)
        try check(offline.health == before.health && offline.readErrors["capabilities"] != nil)
        try check(try storage.load()?.health == before.health)

        for token: String? in [nil, "wrong-access-token"] {
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: peer, accessToken: token)
            try await expectUnauthorized { _ = try await api.discover() }
            try await expectUnauthorized { _ = try await api.fetchFeed() }
            try await expectUnauthorized { _ = try await api.fetchProcesses() }
            try await expectUnauthorized { _ = try await api.fetchQuestions() }
            try await expectUnauthorized { _ = try await api.fetchTasks() }
            try await expectUnauthorized { _ = try await api.fetchHealth() }
        }
    }

    func testWorkflowCredentialRecoveryKeepsExactPendingRequest() async throws {
        let (session, storage, peer) = try makeSession()
        await peer.requireAccessToken("synthetic-access-token")
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.requireAccessToken("replacement-access-token")
        try await expectUnauthorized {
            _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], note: "Keep this approved request")
        }
        let pending = try require(storage.load()?.pending)
        let failed = await session.view()
        try check(failed.pending == pending && !failed.online && failed.lastAcknowledgement == nil)

        let noCredential = try self.session(storage: storage, peer: peer, accessToken: nil)
        try await expectUnauthorized { _ = try await noCredential.retryPending() }
        try check(try storage.load()?.pending == pending)
        let rejectedPosts = await peer.posts
        try check(rejectedPosts.count == 1) // Failed discovery must not send the pending command.

        let recoveredSession = try self.session(storage: storage, peer: peer, accessToken: "replacement-access-token")
        await peer.reject(unauthorizedWorkflowResponse())
        try await expectUnauthorized { _ = try await recoveredSession.retryPending() }
        try check(try storage.load()?.pending == pending)
        let recovered = try await recoveredSession.retryPending()
        try check(recovered.pending == nil && recovered.lastAcknowledgement?.accepted == true)
        let posts = await peer.posts
        try check(posts.count == 3)
        try check(posts.allSatisfy { $0.httpBody == pending.body && $0.url == posts[0].url })
        try check(posts[0].value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-access-token")
        try check(posts.dropFirst().allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer replacement-access-token" })
        let committed = await peer.uniqueCommands
        let durable = await peer.persistedBeforeEveryPost
        try check(committed == 1 && durable)
    }

    func testWorkflowApplyAdvancesToNextCompanyWithExactSelection() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let cards = try require(before.feed?.cards)
        try check(cards.count == 2 && cards[0].company.id != cards[1].company.id)
        let card = cards[0]
        try check(card.jobs.count == 3)
        let selected: Set<String> = [card.jobs[1].key]
        var nextFeed = try fixtures().body("initial_feed")
        let rawCards = try require(nextFeed["cards"]?.array)
        nextFeed["cards"] = .array(Array(rawCards.dropFirst()))
        nextFeed["revision"] = .string("after-direct-apply")
        nextFeed["counts"] = .object([
            "ready_cards": .number(1), "ready_jobs": .number(Double(cards[1].jobs.count))
        ])
        await peer.useFeedAfterDecision(nextFeed)

        let after = try await session.decide(card: card, action: "apply", selected: selected)

        try check(after.pending == nil && after.lastAcknowledgement?.accepted == true)
        try check(after.lastAcknowledgement?.action == "apply")
        try check(after.feed?.cards == [cards[1]] && after.feed?.counts.readyCards == 1)
        try check(try storage.load()?.feed == after.feed)
        let posts = await peer.posts
        try check(posts.count == 1 && posts[0].url?.path == "/api/v1/decisions")
        let command = try JSONDecoder().decode(WorkflowDecisionRequest.self, from: require(posts[0].httpBody))
        let expected = try WorkflowDecisionRequest(card: card, action: "apply", selected: selected, requestID: command.requestID)
        try check(command == expected) // Card revision binds the exact reviewed job versions.
        let durable = await peer.persistedBeforeEveryPost
        try check(durable)
    }

    func testWorkflowLostAcknowledgementRestartsWithExactBytes() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.loseResponse()
        try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], note: "Exact approved version") }
        let saved = try require(storage.load()?.pending)
        let uncertain = await session.view()
        try check(uncertain.feed == before.feed)
        try check(uncertain.pending == saved)
        try await expectFailure { _ = try await session.decide(card: card, action: "exclude", selected: []) }
        let restarted = try self.session(storage: storage, peer: peer)
        let restored = await restarted.view()
        try check(restored.pending == saved && !restored.online)
        var rotated = try fixtures().body("capabilities"); rotated["token"] = .string("rotated-session-token")
        await peer.use(path: "/api/v1/capabilities", body: rotated)
        let recovered = try await restarted.retryPending()
        let acknowledgement = try require(recovered.lastAcknowledgement)
        try check(recovered.pending == nil && acknowledgement.accepted)
        try check(recovered.feed?.cards.isEmpty == true)
        let posts = await peer.posts
        let committed = await peer.uniqueCommands
        let durable = await peer.persistedBeforeEveryPost
        try check(posts.count == 2 && committed == 1 && durable)
        try check(posts.allSatisfy { $0.httpBody == saved.body && $0.url?.path == "/api/v1/decisions" })
        try check(posts.allSatisfy { $0.value(forHTTPHeaderField: "Origin") == origin })
        try check(posts[0].value(forHTTPHeaderField: "X-Review-Token") == "fixture-session-token")
        try check(posts[1].value(forHTTPHeaderField: "X-Review-Token") == "rotated-session-token")
        let afterRestart = try self.session(storage: storage, peer: peer)
        let afterRefresh = try await afterRestart.refresh()
        try check(afterRefresh.lastAcknowledgement == acknowledgement)
        try await expectFailure { _ = try await afterRestart.retryPending() }
        let finalPosts = await peer.posts.count
        try check(finalPosts == 2)
    }

    func testWorkflowPendingDecisionAllowsIndependentReads() async throws {
        let (session, _, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.loseResponse()
        try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
        try await peer.use("process_waiting_for_user")
        try await peer.use("questions_waiting")
        try await peer.use("tasks_after_cancel")
        let updated = try await session.refresh()
        try check(updated.pending != nil && updated.feed == before.feed)
        try check(updated.processes.first?.taskStatus == "waiting_for_user")
        try check(updated.questions.count == 3)
        try check(updated.tasks.contains { $0.status == "cancelled" })
        try check(updated.readErrors.isEmpty)
    }

    func testWorkflowDiskFailureStopsBeforeWire() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        storage.failWrites()
        try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
        let count = await peer.posts.count
        try check(count == 0)
        try check(try storage.load()?.pending == nil)
    }

    func testWorkflowAcknowledgementSurvivesFollowupReadFailure() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.loseReadsAfterCommit()
        let after = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key])
        try check(after.pending == nil && after.lastAcknowledgement?.accepted == true)
        try check(after.feed == nil && after.readErrors.count == 4 && !after.online)
        let restarted = try self.session(storage: storage, peer: peer)
        try await expectFailure { _ = try await restarted.retryPending() }
        let count = await peer.posts.count
        try check(count == 1)
    }

    func testWorkflowAcknowledgementSaveFailureKeepsRecoveryCommand() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.failAcknowledgementPersistence()
        try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
        let pending = try require(storage.load()?.pending)
        try check(try storage.load()?.lastAcknowledgement == nil)
        storage.allowWrites()
        let restarted = try self.session(storage: storage, peer: peer)
        let recovered = try await restarted.retryPending()
        try check(recovered.pending == nil && recovered.lastAcknowledgement?.accepted == true)
        let posts = await peer.posts
        let unique = await peer.uniqueCommands
        try check(unique == 1 && posts.count == 2 && posts.allSatisfy { $0.httpBody == pending.body })
    }

    func testWorkflowAcknowledgementMustMatchCapturedCommand() async throws {
        let fixtures = try fixtures()
        let fixture = try fixtures.item("apply_accepted")
        let mutations: [(String, JSONValue)] = [
            ("request_id", .string("another-command")), ("decision_id", .string("another-decision")),
            ("card_id", .string("another-card")), ("action", .string("exclude")),
            ("accepted", .bool(false)), ("token", .string("")),
            ("process_ids", .array([])), ("task_ids", .array([])),
            ("process_ids", .array([.string("duplicate"), .string("duplicate")]))
        ]
        for (field, value) in mutations {
            var body = fixture.response.body; body[field] = value
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body))))
            try await expectFailure { _ = try await api.send(pending(fixture), sessionToken: fixtures.token) }
        }
        var body = fixture.response.body; body.removeValue(forKey: "task_ids")
        let missing = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body))))
        try await expectFailure { _ = try await missing.send(pending(fixture), sessionToken: fixtures.token) }
    }

    func testWorkflowDecisionRequiresAnExplicitValidSelection() throws {
        let cards: [WorkflowCard] = try decoded([WorkflowCard].self, name: "initial_feed", field: "cards")
        let card = try require(cards.first)
        for action in ["apply", "pass", "later"] {
            let request = try WorkflowDecisionRequest(card: card, action: action, selected: [card.jobs[1].key], requestID: "exact-intent")
            try check(request.cardID == card.id && request.cardRevision == card.revision && request.selected == [card.jobs[1].key])
            try checkThrows { try WorkflowDecisionRequest(card: card, action: action, selected: []) }
            try checkThrows { try WorkflowDecisionRequest(card: card, action: action, selected: ["foreign-job"]) }
        }
        for action in ["find_more", "exclude"] {
            _ = try WorkflowDecisionRequest(card: card, action: action, selected: [])
            try checkThrows { try WorkflowDecisionRequest(card: card, action: action, selected: [card.jobs[0].key]) }
        }
        let exclusions = Set(card.jobs.map(\.key))
        let research = try WorkflowDecisionRequest(card: card, action: "find_more", selected: [], excludeJobKeys: exclusions, note: "Exact user note")
        try check(research.excludeJobKeys == exclusions.sorted() && research.note == "Exact user note")
        try checkThrows { try WorkflowDecisionRequest(card: card, action: "apply", selected: [card.jobs[0].key], excludeJobKeys: exclusions) }
        try checkThrows { try WorkflowDecisionRequest(card: card, action: "approve", selected: [card.jobs[0].key]) }
        try checkThrows { try WorkflowDecisionRequest(card: card, action: "exclude", selected: [], requestID: " ") }
    }

    func testWorkflowRankedContentBindsExactOrderedJobVersions() async throws {
        let original = try fixtures().body("initial_feed")
        let originalCards = try require(original["cards"]?.array)
        let first = try require(originalCards.first?.object)
        let jobs = try require(first["jobs"]?.array)
        let reason = "Verified backend evidence. <strong>This remains plain text.</strong>"
        let content: [String: JSONValue] = [
            "schema_version": .number(1), "source": .string("ranker"), "summary": .string("Ranked company context."),
            "jobs": .array(jobs.map { job in .object([
                "key": job.object["key"]!, "revision": job.object["revision"]!, "reason": .string(reason),
                "brief": .object(["work": .string("Build backend services."), "fit": .string("Confirmed backend experience."), "gap": .string("Kubernetes remains unconfirmed.")])
            ]) }), "future_metadata": .string("ignored")
        ]
        func fetch(_ contentValue: JSONValue?) async throws -> WorkflowCard {
            var body = original; var cards = originalCards; var card = first
            card["content"] = contentValue; cards[0] = .object(card); body["cards"] = .array(cards)
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body))))
            let feed = try await api.fetchFeed()
            return try require(feed.value.cards.first)
        }
        let absent = try await fetch(nil)
        let empty = try await fetch(.null)
        try check(absent.content == nil && empty.content == nil)
        let ranked = try await fetch(.object(content))
        try check(ranked.content?.summary == "Ranked company context.")
        try check(ranked.content?.jobs.first?.reason == reason)
        try check(ranked.content?.jobs.map(\.key) == ranked.jobs.map(\.key))
        _ = try WorkflowDecisionRequest(card: ranked, action: "apply", selected: [ranked.jobs[0].key])
        for (field, value) in [("schema_version", JSONValue.number(2)), ("source", .string("unknown_source")),
                               ("summary", .string(" \n ")), ("summary", .string(String(repeating: "x", count: 1_001)))] {
            var invalid = content; invalid[field] = value
            try await expectFailure { _ = try await fetch(.object(invalid)) }
        }
        let originalItems = try require(content["jobs"]?.array)
        var reordered = content; reordered["jobs"] = .array(originalItems.reversed())
        try await expectFailure { _ = try await fetch(.object(reordered)) }
        var fewer = content; fewer["jobs"] = .array(Array(originalItems.dropLast()))
        try await expectFailure { _ = try await fetch(.object(fewer)) }
        for (field, value) in [("key", JSONValue.string("other-job")), ("revision", .number(99)),
                               ("reason", .string("")), ("reason", .string(String(repeating: "x", count: 5_001)))] {
            var item = originalItems[0].object; item[field] = value
            var items = originalItems; items[0] = .object(item)
            var invalid = content; invalid["jobs"] = .array(items)
            try await expectFailure { _ = try await fetch(.object(invalid)) }
        }
        for field in ["work", "fit", "gap"] {
            var item = originalItems[0].object
            var brief = try require(item["brief"]?.object); brief[field] = .string(String(repeating: "x", count: 601))
            item["brief"] = .object(brief)
            var items = originalItems; items[0] = .object(item)
            var invalid = content; invalid["jobs"] = .array(items)
            try await expectFailure { _ = try await fetch(.object(invalid)) }
        }
        var changed = ranked; changed.jobs[0].revision += 1
        try checkThrows { try WorkflowDecisionRequest(card: changed, action: "apply", selected: [changed.jobs[0].key]) }
    }

    func testWorkflowDiscoveryDowngradePreservesPendingCommand() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.loseResponse()
        try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
        let original = try require(storage.load()?.pending)
        await peer.use(path: "/api/v1/capabilities", body: ["error": .string("Legacy endpoint not found")], status: 404)
        try await expectFailure { _ = try await session.refresh() }
        try await expectFailure { _ = try await session.retryPending() }
        try check(try storage.load()?.pending == original)
        let after = await session.view()
        try check(!after.online && after.feed == before.feed)
        let posts = await peer.posts.count
        try check(posts == 1)
    }

    func testWorkflowSubmissionAndEventsCannotClaimUnsupportedSuccess() async throws {
        let original = try fixtures().body("process_waiting_for_user")
        let originalProcess = try require(original["processes"]?.array.first?.object)
        func fetch(_ process: [String: JSONValue]) async throws -> WorkflowProcess {
            var body = original; body["processes"] = .array([.object(process)])
            let api = WorkflowAPI(address: try ServerAddress(origin), transport: FixedWorkflowTransport(response: HTTPResult(status: 200, body: try JSONEncoder().encode(body))))
            let result = try await api.fetchProcesses()
            return try require(result.value.first)
        }
        var future = originalProcess
        future["submission_status"] = .string("future_submission_state")
        future["recruiting_status"] = .string("future_recruiting_state")
        future["task_status"] = .string("future_task_state")
        let forwardCompatible = try await fetch(future)
        try check(forwardCompatible.submissionStatus == "future_submission_state" && !forwardCompatible.canAnswer)
        var submitted = originalProcess; submitted["submission_status"] = .string("submitted")
        try await expectFailure { _ = try await fetch(submitted) } // No receipt means no proven submission.
        let receipt: [String: JSONValue] = ["url": .string("https://jobs.example/confirmation"), "evidence": .string("Synthetic confirmation"), "observed_at": .string("2026-10-12T23:23:16Z"), "reference": .string("synthetic-reference")]
        submitted["receipt"] = .object(receipt)
        let proven = try await fetch(submitted)
        try check(proven.receipt?.safeURL?.host == "jobs.example")
        var unsafe = receipt; unsafe["url"] = .string("javascript:alert(1)")
        submitted["receipt"] = .object(unsafe)
        try await expectFailure { _ = try await fetch(submitted) }
        let events = try require(originalProcess["events"]?.array)
        var reordered = originalProcess; reordered["events"] = .array(events.reversed())
        try await expectFailure { _ = try await fetch(reordered) }
        var duplicate = originalProcess; duplicate["events"] = .array([events[0], events[0]])
        try await expectFailure { _ = try await fetch(duplicate) }
    }

    func testWorkflowRejectionRecoveryUsesReasonAndCommitStatus() async throws {
        let cases: [(Int, String, String?, String, Bool)] = [
            (409, "conflict", "stale_revision", "not_committed", false),
            (409, "conflict", "operation_rejected", "not_committed", false),
            (409, "conflict", "request_id_conflict", "not_committed", true),
            (409, "conflict", nil, "not_committed", true),
            (409, "conflict", "future_reason", "not_committed", true),
            (409, "conflict", "stale_revision", "unknown", true),
            (403, "forbidden", nil, "not_committed", true),
            (401, "unauthorized", nil, "not_committed", true),
            (500, "internal_error", nil, "unknown", true),
            (400, "invalid_request", nil, "not_committed", false)
        ]
        for (status, code, reason, commit, keep) in cases {
            let (session, storage, peer) = try makeSession()
            let before = try await session.refresh()
            let card = try require(before.feed?.cards.first)
            var error: [String: JSONValue] = ["code": .string(code), "message": .string("Synthetic command rejection"), "commit_status": .string(commit)]
            if let reason { error["reason"] = .string(reason) }
            await peer.reject(WorkflowFixture.Response(status: status, body: ["schema_version": .number(1), "error": .object(error)]))
            try await expectFailure { _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
            try check((try storage.load()?.pending != nil) == keep)
            let posts = await peer.posts
            try check(posts.count == 1)
            if keep {
                let original = try require(storage.load()?.pending)
                _ = try await session.retryPending()
                let retries = await peer.posts
                try check(retries.count == 2 && retries.last?.httpBody == original.body)
            }
        }
    }

    func testWorkflowPlainTextGatewayErrorsPreserveExactRequest() async throws {
        for status in [411, 413, 421, 502] {
            let (session, storage, peer) = try makeSession()
            let before = try await session.refresh()
            let card = try require(before.feed?.cards.first)
            await peer.reject(HTTPResult(status: status, body: Data("Gateway request failed\n".utf8)))
            do {
                _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key])
                throw CheckFailure("Expected a gateway failure")
            } catch let error as WorkflowHTTPError {
                try check(error.status == status && !error.isDefinitiveRejection)
                try check(error.code == nil && error.reason == nil && error.commitStatus == nil)
                try check(error.message.contains("HTTP \(status)"))
            }
            let pending = try require(storage.load()?.pending)
            let failed = await session.view()
            try check(failed.pending == pending && !failed.online && failed.lastAcknowledgement == nil)
            let restarted = try self.session(storage: storage, peer: peer)
            let restored = await restarted.view()
            try check(restored.pending == pending)
            let recovered = try await restarted.retryPending()
            let posts = await peer.posts
            let commits = await peer.uniqueCommands
            try check(recovered.pending == nil && recovered.lastAcknowledgement?.accepted == true)
            try check(posts.count == 2 && commits == 1)
            try check(posts.allSatisfy { $0.httpBody == pending.body && $0.url == posts[0].url })
            try check(posts.allSatisfy { $0.value(forHTTPHeaderField: "Origin") == origin })
        }
    }

    func testWorkflowStorageHasNoTokensAndPartitionsOrigins() async throws {
        let (session, storage, peer) = try makeSession()
        let before = try await session.refresh()
        let card = try require(before.feed?.cards.first)
        await peer.loseResponse()
        try await expectFailure { _ = try await session.decide(card: card, action: "find_more", selected: []) }
        let saved = try require(storage.load())
        let json = String(decoding: try JSONEncoder().encode(saved), as: UTF8.self)
        try check(!json.contains("fixture-session-token") && !json.contains("synthetic-access-token"))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let one = try WorkflowFilePersistence(directory: directory, origin: origin)
        let two = try WorkflowFilePersistence(directory: directory, origin: "https://other.example")
        let legacy = try FilePersistence(directory: directory, origin: origin)
        try one.save(saved)
        try check(one.file != two.file && one.file != legacy.file)
        try check(try one.load()?.pending == saved.pending)
        try check(try two.load() == nil && legacy.load() == nil)
        let wrongAPI = WorkflowAPI(address: try ServerAddress("https://other.example"), transport: peer)
        try checkThrows { try WorkflowSession(api: wrongAPI, persistence: one) }
        try Data("corrupt cache".utf8).write(to: one.file)
        try checkThrows { try one.load() }
        try check(FileManager.default.fileExists(atPath: one.file.path))
    }

    func testWorkflowTypedAnswersCaptureVersionsAndAllowPartialRounds() throws {
        let processes: [WorkflowProcess] = try decoded([WorkflowProcess].self, name: "process_waiting_for_user", field: "processes")
        let process = try require(processes.first)
        let questions: [WorkflowQuestion] = try decoded([WorkflowQuestion].self, name: "questions_waiting", field: "questions")
        let text = try require(questions.first { $0.type == "text" })
        let choice = try require(questions.first { $0.type == "single_choice" })
        let multi = try require(questions.first { $0.type == "multi_choice" })
        let partial = try WorkflowAnswerRequest(process: process, questions: questions, values: [text.id: .string("My own answer")], requestID: "partial-round")
        try check(partial.processRevision == process.revision && partial.formVersion == process.formVersion)
        try check(partial.answers.count == 1 && partial.answers.first?.questionRevision == text.revision)
        try check(partial.answers.first?.questionID == text.id)
        _ = try WorkflowAnswerRequest(process: process, questions: questions, values: [choice.id: .string("sf"), multi.id: .array([])])
        try checkThrows { try text.validate(.string(" \n ")) }
        try checkThrows { try text.validate(.string(String(repeating: "x", count: 20_001))) }
        try checkThrows { try choice.validate(.string("San Francisco")) } // Labels are not choice IDs.
        try checkThrows { try multi.validate(.array([.string("morning"), .string("morning")])) }
        try checkThrows { try multi.validate(.array([.string("not-listed")])) }
        try checkThrows { try multi.validate(.null) }
        var requiredMulti = multi; requiredMulti.required = true
        try checkThrows { try requiredMulti.validate(.array([])) }
        var unsupported = text; unsupported.scope = "profile"
        try checkThrows { try unsupported.validate(.string("Do not reuse as a profile fact")) }
        unsupported = text; unsupported.type = "future_type"
        try check(!unsupported.isSupported)
        try checkThrows { try unsupported.validate(.string("answer")) }
        var stale = text; stale.formVersion = "older-form"
        try checkThrows { try WorkflowAnswerRequest(process: process, questions: [stale], values: [stale.id: .string("answer")]) }
        try checkThrows { try WorkflowAnswerRequest(process: process, questions: questions, values: ["foreign-question": .string("answer")]) }
        try checkThrows { try WorkflowAnswerRequest(process: process, questions: questions, values: [:]) }
    }

    func testWorkflowAnswersRejectChangedSnapshotsAndReplayExactBytes() async throws {
        let (session, _, peer) = try makeSession()
        try await peer.use("process_waiting_for_user"); try await peer.use("questions_waiting")
        let before = try await session.refresh()
        let process = try require(before.processes.first)
        let question = try require(before.questions.first { $0.type == "text" })
        var stale = question; stale.revision += 1
        try await expectFailure { _ = try await session.answer(process: process, questions: [stale], values: [stale.id: .string("Own answer")]) }
        var changedProcess = process; changedProcess.revision += 1
        try await expectFailure { _ = try await session.answer(process: changedProcess, questions: before.questions, values: [question.id: .string("Own answer")]) }
        let rejectedCount = await peer.posts.count
        try check(rejectedCount == 0)
        await peer.loseResponse()
        try await expectFailure { _ = try await session.answer(process: process, questions: before.questions, values: [question.id: .string("Own answer")]) }
        let recovered = try await session.retryPending()
        try check(recovered.lastAcknowledgement?.taskIDs == [process.taskID])
        try check(recovered.pending == nil)
        let posts = await peer.posts
        try check(posts.count == 2 && posts[0].httpBody == posts[1].httpBody)
        let request = try JSONDecoder().decode(WorkflowAnswerRequest.self, from: require(posts[0].httpBody))
        try check(request.answers.count == 1 && request.answers[0].value == .string("Own answer"))
    }

    func testWorkflowCancellationGuardsAndReplay() async throws {
        let tasks: [WorkflowTask] = try decoded([WorkflowTask].self, name: "tasks_queued", field: "tasks")
        let task = try require(tasks.first)
        for status in ["queued", "retry_wait", "running", "waiting_for_user", "succeeded", "failed", "cancelled", "future_status"] {
            var variant = task; variant.status = status
            if ["queued", "retry_wait"].contains(status) { _ = try WorkflowCancelRequest(task: variant) }
            else { try checkThrows { try WorkflowCancelRequest(task: variant) } }
        }
        var quarantined = task; quarantined.resourceQuarantined = true
        try checkThrows { try WorkflowCancelRequest(task: quarantined) }
        let (session, _, peer) = try makeSession()
        _ = try await session.refresh()
        var processes = try fixtures().body("process_queued")
        var process = try require(processes["processes"]?.array.first?.object)
        process["submission_status"] = .string("submission_unknown")
        processes["processes"] = .array([.object(process)])
        await peer.use(path: "/api/v1/processes", body: processes)
        _ = try await session.refresh()
        try await expectFailure { _ = try await session.cancel(task: task) }
        let rejected = await peer.posts.count
        try check(rejected == 0)
        try await peer.use("process_queued")
        _ = try await session.refresh()
        await peer.loseResponse()
        try await expectFailure { _ = try await session.cancel(task: task) }
        let recovered = try await session.retryPending()
        try check(recovered.pending == nil && recovered.lastAcknowledgement?.taskIDs == [task.id])
        let posts = await peer.posts
        try check(posts.count == 2 && posts[0].httpBody == posts[1].httpBody)
    }

    func testWorkflowFailedReadsBlockOnlyTheirWrites() async throws {
        let (session, _, peer) = try makeSession()
        try await peer.use("process_waiting_for_user"); try await peer.use("questions_waiting")
        let before = try await session.refresh()
        let process = try require(before.processes.first)
        let question = try require(before.questions.first { $0.type == "text" })
        await peer.failRead("/api/v1/questions")
        let partial = try await session.refresh()
        try check(partial.online && partial.readErrors["questions"] != nil)
        try check(partial.questions == before.questions)
        try await expectFailure { _ = try await session.answer(process: process, questions: before.questions, values: [question.id: .string("Own answer")]) }
        let card = try require(partial.feed?.cards.last)
        let allowed = try await session.decide(card: card, action: "find_more", selected: [])
        try check(allowed.pending == nil && allowed.lastAcknowledgement?.action == "find_more")
    }

    func testWorkflowIsolatedPythonServerContract() async throws {
        guard let address = ProcessInfo.processInfo.environment["JOB_REVIEW_WORKFLOW_TEST_SERVER_URL"] else { return }
        let api = WorkflowAPI(address: try ServerAddress(address), transport: URLSessionTransport())
        let capabilities = try await api.discover()
        try check(capabilities?.workerEnabled == false && capabilities?.explicitApply == true)
        let health = try await api.fetchHealth()
        let requiredHealthIDs: Set<String> = ["storage", "chrome_attachment", "linkedin_auth"]
        try check(health?.value.ready == false && requiredHealthIDs.isSubset(of: Set(health?.value.checks.map(\.id) ?? [])))
        let initial = try await api.fetchFeed()
        try check(initial.value.cards.count >= 3)
        try check(initial.value.cards.flatMap(\.jobs).allSatisfy { $0.officialURL?.host == "example.com" && $0.jobID.hasPrefix("demo-") })

        // The same server retains legacy Approve and manual Queue meanings.
        let legacy = ReviewAPI(address: try ServerAddress(address), transport: URLSessionTransport())
        let legacyBefore = try await legacy.fetch()
        let legacyCard = try require(legacyBefore.flashcards.first)
        let approval = try PendingSave(origin: api.address.origin, path: "api/card", payload: CardRequest(card: legacyCard, action: .approve, selected: [legacyCard.jobs[0].key]), summary: "Synthetic legacy approval")
        let approved = try await legacy.send(approval, sessionToken: require(legacyBefore.token))
        try check(approved.queue.isEmpty && approved.readyJobs.map(\.key) == [legacyCard.jobs[0].key])
        let noProcesses = try await api.fetchProcesses()
        try check(noProcesses.value.isEmpty)
        let legacySession = try ReviewSession(api: legacy, persistence: MemoryPersistence())
        _ = try await legacySession.refresh()
        _ = try await legacySession.queue(keys: [legacyCard.jobs[0].key])
        let stillNoProcesses = try await api.fetchProcesses()
        try check(stillNoProcesses.value.isEmpty)

        let fresh = try await api.fetchFeed()
        let card = try require(fresh.value.cards.first { $0.company.id != legacyCard.companyID })
        let command = try WorkflowDecisionRequest(card: card, action: "apply", selected: [card.jobs[0].key])
        let apply = try PendingSave(origin: api.address.origin, path: "api/v1/decisions", payload: command, summary: "Synthetic explicit Apply")
        let accepted = try await api.send(apply, sessionToken: fresh.token)
        let replayed = try await api.send(apply, sessionToken: accepted.token)
        try check(accepted.value == replayed.value && accepted.value.processIDs.count == 1)
        let processes = try await api.fetchProcesses()
        try check(processes.value.count == 1 && processes.value.first?.submissionStatus == "not_started")
        try check(processes.value.first?.taskStatus == "queued" && processes.value.first?.receipt == nil)
        let tasks = try await api.fetchTasks()
        let applyTask = try require(tasks.value.first { $0.id == accepted.value.taskIDs.first })
        let cancel = try PendingSave(origin: api.address.origin, path: "api/v1/tasks/cancel", payload: WorkflowCancelRequest(task: applyTask), summary: "Synthetic cancel")
        let cancelled = try await api.send(cancel, sessionToken: tasks.token)
        let cancelReplay = try await api.send(cancel, sessionToken: cancelled.token)
        try check(cancelled.value == cancelReplay.value)
        let oldApplyReplay = try await api.send(apply, sessionToken: cancelReplay.token)
        try check(oldApplyReplay.value == accepted.value) // The receipt is immutable after cancellation.
        let next = try await api.fetchFeed()
        let researchCard = try require(next.value.cards.first { $0.company.id != legacyCard.companyID && $0.company.id != card.company.id })
        let research = try PendingSave(origin: api.address.origin, path: "api/v1/decisions", payload: WorkflowDecisionRequest(card: researchCard, action: "find_more", selected: [], excludeJobKeys: [researchCard.jobs[0].key]), summary: "Synthetic company research")
        let researchAck = try await api.send(research, sessionToken: next.token)
        try check(researchAck.value.processIDs.isEmpty && !researchAck.value.taskIDs.isEmpty)
        let researchReplay = try await api.send(research, sessionToken: researchAck.token)
        try check(researchReplay.value == researchAck.value)
    }

    func cases() -> [(String, () async throws -> Void)] {
        [
            ("testWorkflowHealthFixturesAndRequiredFields", { try await testWorkflowHealthFixturesAndRequiredFields() }),
            ("testWorkflowHealthOnDemandAndThreeCheckReadiness", { try await testWorkflowHealthOnDemandAndThreeCheckReadiness() }),
            ("testWorkflowIdleBrowserDoesNotHideRankOrTaskFailures", { try await testWorkflowIdleBrowserDoesNotHideRankOrTaskFailures() }),
            ("testWorkflowHealthFreshnessExpiresAndRejectsInvalidDates", { try await testWorkflowHealthFreshnessExpiresAndRejectsInvalidDates() }),
            ("testWorkflowHealth404DoesNotDowngradeV1", { try await testWorkflowHealth404DoesNotDowngradeV1() }),
            ("testWorkflowHealthFailureDoesNotBlockDurableCommands", { try await testWorkflowHealthFailureDoesNotBlockDurableCommands() }),
            ("testWorkflowHealthCacheAcceptsOlderSessionsAndHasNoToken", { try await testWorkflowHealthCacheAcceptsOlderSessionsAndHasNoToken() }),
            ("testEveryWorkflowHTTPFixture", { try await testEveryWorkflowHTTPFixture() }),
            ("testWorkflowDiscoveryFallsBackOnlyFor404", { try await testWorkflowDiscoveryFallsBackOnlyFor404() }),
            ("testWorkflowAuthenticationFailureDoesNotDowngradeOrClaimFreshHealth", { try await testWorkflowAuthenticationFailureDoesNotDowngradeOrClaimFreshHealth() }),
            ("testWorkflowCredentialRecoveryKeepsExactPendingRequest", { try await testWorkflowCredentialRecoveryKeepsExactPendingRequest() }),
            ("testWorkflowApplyAdvancesToNextCompanyWithExactSelection", { try await testWorkflowApplyAdvancesToNextCompanyWithExactSelection() }),
            ("testWorkflowLostAcknowledgementRestartsWithExactBytes", { try await testWorkflowLostAcknowledgementRestartsWithExactBytes() }),
            ("testWorkflowPendingDecisionAllowsIndependentReads", { try await testWorkflowPendingDecisionAllowsIndependentReads() }),
            ("testWorkflowDiskFailureStopsBeforeWire", { try await testWorkflowDiskFailureStopsBeforeWire() }),
            ("testWorkflowAcknowledgementSurvivesFollowupReadFailure", { try await testWorkflowAcknowledgementSurvivesFollowupReadFailure() }),
            ("testWorkflowAcknowledgementSaveFailureKeepsRecoveryCommand", { try await testWorkflowAcknowledgementSaveFailureKeepsRecoveryCommand() }),
            ("testWorkflowAcknowledgementMustMatchCapturedCommand", { try await testWorkflowAcknowledgementMustMatchCapturedCommand() }),
            ("testWorkflowDecisionRequiresAnExplicitValidSelection", { try testWorkflowDecisionRequiresAnExplicitValidSelection() }),
            ("testWorkflowRankedContentBindsExactOrderedJobVersions", { try await testWorkflowRankedContentBindsExactOrderedJobVersions() }),
            ("testWorkflowDiscoveryDowngradePreservesPendingCommand", { try await testWorkflowDiscoveryDowngradePreservesPendingCommand() }),
            ("testWorkflowSubmissionAndEventsCannotClaimUnsupportedSuccess", { try await testWorkflowSubmissionAndEventsCannotClaimUnsupportedSuccess() }),
            ("testWorkflowRejectionRecoveryUsesReasonAndCommitStatus", { try await testWorkflowRejectionRecoveryUsesReasonAndCommitStatus() }),
            ("testWorkflowPlainTextGatewayErrorsPreserveExactRequest", { try await testWorkflowPlainTextGatewayErrorsPreserveExactRequest() }),
            ("testWorkflowStorageHasNoTokensAndPartitionsOrigins", { try await testWorkflowStorageHasNoTokensAndPartitionsOrigins() }),
            ("testWorkflowTypedAnswersCaptureVersionsAndAllowPartialRounds", { try testWorkflowTypedAnswersCaptureVersionsAndAllowPartialRounds() }),
            ("testWorkflowAnswersRejectChangedSnapshotsAndReplayExactBytes", { try await testWorkflowAnswersRejectChangedSnapshotsAndReplayExactBytes() }),
            ("testWorkflowCancellationGuardsAndReplay", { try await testWorkflowCancellationGuardsAndReplay() }),
            ("testWorkflowFailedReadsBlockOnlyTheirWrites", { try await testWorkflowFailedReadsBlockOnlyTheirWrites() }),
            ("testWorkflowIsolatedPythonServerContract", { try await testWorkflowIsolatedPythonServerContract() })
        ]
    }
}
