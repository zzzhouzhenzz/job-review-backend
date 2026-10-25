import Foundation
import ReviewCore
import XCTest
@testable import MacDashboardCore

final class DashboardClientTests: XCTestCase {
    private var directory: URL!
    private let bearer = String(repeating: "F", count: 43)
    private let csrf = "fictional-response-session-token"

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
    }

    override func tearDownWithError() throws {
        try FileManager.default.removeItem(at: directory)
    }

    private func settings() throws -> ConnectionSettings {
        let file = directory.appendingPathComponent("credential.txt")
        try bearer.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return try ConnectionSettings(origin: "https://fixture.example", credentialFile: file.path)
    }

    private func envelope(_ value: [String: Any]) throws -> Data {
        var object: [String: Any] = ["schema_version": 1, "revision": "fixture-revision", "complete": true,
                                     "next_cursor": NSNull(), "token": csrf]
        object.merge(value) { _, new in new }
        return try JSONSerialization.data(withJSONObject: object)
    }

    private func responses() throws -> [String: HTTPResult] {
        let health: [String: Any] = [
            "boot_id": "fictional-boot", "status": "idle", "ready": false,
            "checked_at": "2026-10-17T08:00:00Z", "expires_at": NSNull(), "retry_at": NSNull(),
            "checks": [
                ["id": "storage", "status": "passed", "message": "Saved data is available."],
                ["id": "chrome_attachment", "status": "on_demand", "message": "Checked when needed."],
                ["id": "linkedin_auth", "status": "on_demand", "message": "Checked when needed."]
            ]
        ]
        let capabilities: [String: Any] = ["api_version": 1, "worker_enabled": true, "worker_status": "idle",
                                           "explicit_apply": true, "questions": true, "feed_delivery": "poll",
                                           "question_scopes": ["application"], "remote_access": true]
        let task: [String: Any] = ["id": "fictional-task", "kind": "rank", "status": "queued", "attempts": 0,
                                  "failure_count": 0, "max_attempts": 3, "available_at": "2026-10-17T08:00:00Z",
                                  "resource": NSNull(), "resource_quarantined": false, "error": NSNull(),
                                  "created_at": "2026-10-17T08:00:00Z", "updated_at": "2026-10-17T08:00:00Z"]
        return [
            "/api/v1/feed": HTTPResult(status: 200, body: try envelope([
                "cards": [], "counts": ["ready_cards": 0, "ready_jobs": 0],
                "refill": ["low_watermark": 3, "target_cards": 5, "next_allowed_at": NSNull()], "capabilities": capabilities])),
            "/api/v1/health": HTTPResult(status: 200, body: try envelope(["health": health])),
            "/api/v1/tasks": HTTPResult(status: 200, body: try envelope(["tasks": [task]])),
            "/api/v1/processes": HTTPResult(status: 200, body: try envelope(["processes": []])),
            "/api/v1/questions": HTTPResult(status: 200, body: try envelope(["questions": []]))
        ]
    }

    private func changing(_ responses: [String: HTTPResult], path: String,
                          _ update: (inout [String: Any]) -> Void) throws -> [String: HTTPResult] {
        var result = responses
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: result[path]!.body) as? [String: Any])
        update(&object)
        result[path] = HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: object))
        return result
    }

    func testCompleteSnapshotUsesOnlyAuthenticatedReadsAndKeepsIdleHonest() async throws {
        let transport = FixtureTransport(try responses())
        let client = try DashboardClient(settings: settings(), transport: transport)
        let before = Date()
        let snapshot = try await client.fetch()
        XCTAssertEqual(snapshot.health.status, "idle")
        XCTAssertFalse(snapshot.health.ready)
        XCTAssertEqual(snapshot.health.checks[1].status, "on_demand")
        XCTAssertEqual(snapshot.tasks.first?.kind, "rank")
        XCTAssertGreaterThanOrEqual(snapshot.fetchedAt, before)
        let requests = await transport.requests
        XCTAssertEqual(Set(requests.compactMap { $0.url?.path }), DashboardReadTransport.paths)
        XCTAssertEqual(requests.count, 5)
        for request in requests {
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(request.httpBody)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(bearer)")
            XCTAssertNil(request.value(forHTTPHeaderField: "X-Review-Token"))
            XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        }
        let encoded = String(decoding: try JSONEncoder().encode(snapshot), as: UTF8.self)
        XCTAssertFalse(encoded.contains(csrf))
        XCTAssertFalse(encoded.contains(bearer))
        XCTAssertFalse(String(reflecting: client).contains(bearer))
    }

    func testIncompleteEnvelopeRejectsWholeRefresh() async throws {
        for key in ["complete", "next_cursor", "token", "revision", "schema_version"] {
            let values = try changing(responses(), path: "/api/v1/tasks") { $0.removeValue(forKey: key) }
            let client = try DashboardClient(settings: settings(), transport: FixtureTransport(values))
            do { _ = try await client.fetch(); XCTFail("Expected failure for missing \(key)") }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }

    func testNonemptyApplicationAndQuestionRemainObservationOnly() async throws {
        let process: [String: Any] = [
            "id": "fictional-process", "job_key": "fictional-job", "task_id": "fictional-task",
            "decision_id": "fictional-approved-decision", "submission_status": "preparing",
            "recruiting_status": "unknown", "task_status": "waiting_for_user", "revision": 1,
            "receipt": NSNull(), "block_reason": "A required answer is missing.", "form_version": "fictional-form-1",
            "created_at": "2026-10-17T08:00:00Z", "updated_at": "2026-10-17T08:00:00Z", "events": [],
            "snapshot": ["company_id": "fictional-company", "company_name": "Example Company",
                         "job_key": "fictional-job", "job_id": "example-123", "job_revision": 1,
                         "material_hash": "fictional-hash", "title": "Example Engineer", "url": "https://example.com/jobs/123"]
        ]
        let question: [String: Any] = [
            "id": "fictional-question", "process_id": "fictional-process", "form_version": "fictional-form-1",
            "revision": 1, "text": "Fictional required answer", "type": "text", "choices": [], "required": true,
            "scope": "application", "reason": "Missing fact", "answer": NSNull(), "answered_at": NSNull(),
            "created_at": "2026-10-17T08:00:00Z", "updated_at": "2026-10-17T08:00:00Z"
        ]
        var values = try responses()
        values["/api/v1/processes"] = HTTPResult(status: 200, body: try envelope(["processes": [process]]))
        values["/api/v1/questions"] = HTTPResult(status: 200, body: try envelope(["questions": [question]]))
        let transport = FixtureTransport(values)
        let snapshot = try await DashboardClient(settings: settings(), transport: transport).fetch()
        XCTAssertEqual(snapshot.processes.first?.submissionStatus, "preparing")
        XCTAssertEqual(snapshot.processes.first?.taskStatus, "waiting_for_user")
        XCTAssertEqual(snapshot.questions.first?.processID, snapshot.processes.first?.id)
        XCTAssertNil(snapshot.questions.first?.answer)
        XCTAssertNil(snapshot.processes.first?.receipt)
        let requests = await transport.requests
        XCTAssertTrue(requests.allSatisfy { $0.httpMethod == "GET" && $0.httpBody == nil })
    }

    func testMissingHealthCheckRejectsResponse() async throws {
        let values = try changing(responses(), path: "/api/v1/health") { object in
            var health = object["health"] as! [String: Any]
            health["checks"] = [["id": "storage", "status": "passed", "message": "Available"]]
            object["health"] = health
        }
        let client = try DashboardClient(settings: settings(), transport: FixtureTransport(values))
        do { _ = try await client.fetch(); XCTFail("Expected missing check rejection") }
        catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
    }

    func testFalseCompleteAndNonNullCursorAreRejected() async throws {
        for patch: [String: Any] in [["complete": false], ["next_cursor": "more"], ["schema_version": 2]] {
            let values = try changing(responses(), path: "/api/v1/feed") { $0.merge(patch) { _, new in new } }
            let client = try DashboardClient(settings: settings(), transport: FixtureTransport(values))
            do { _ = try await client.fetch(); XCTFail("Expected envelope rejection") }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }

    func testMalformedJSONDoesNotSurfaceBody() async throws {
        var values = try responses()
        values["/api/v1/tasks"] = HTTPResult(status: 200, body: Data((bearer + csrf).utf8))
        let client = try DashboardClient(settings: settings(), transport: FixtureTransport(values))
        do { _ = try await client.fetch(); XCTFail("Expected malformed response") }
        catch {
            XCTAssertEqual(error as? DashboardError, .invalidResponse)
            XCTAssertFalse(error.localizedDescription.contains(bearer))
            XCTAssertFalse(error.localizedDescription.contains(csrf))
        }
    }

    func testHTTPAndAuthenticationErrorsIgnoreRawErrorText() async throws {
        for status in [301, 302, 401, 403, 404, 429, 500, 503] {
            var values = try responses()
            values["/api/v1/tasks"] = HTTPResult(status: status, body: Data((bearer + csrf).utf8))
            let client = try DashboardClient(settings: settings(), transport: FixtureTransport(values))
            do { _ = try await client.fetch(); XCTFail("Expected HTTP rejection") }
            catch {
                XCTAssertEqual(error as? DashboardError, [401, 403].contains(status) ? .authentication : .http(status))
                XCTAssertFalse(error.localizedDescription.contains(bearer))
                XCTAssertFalse(error.localizedDescription.contains(csrf))
            }
        }
    }

    func testKnownSecretsInPublicDiagnosticsAreRedacted() async throws {
        let values = try changing(responses(), path: "/api/v1/tasks") { object in
            var tasks = object["tasks"] as! [[String: Any]]
            tasks[0]["error"] = "Failure with \(bearer) and \(csrf)"
            object["tasks"] = tasks
        }
        let snapshot = try await DashboardClient(settings: settings(), transport: FixtureTransport(values)).fetch()
        XCTAssertEqual(snapshot.tasks.first?.error, "Failure with [redacted] and [redacted]")
        let saved = String(decoding: try JSONEncoder().encode(snapshot), as: UTF8.self)
        XCTAssertFalse(saved.contains(bearer))
        XCTAssertFalse(saved.contains(csrf))
    }

    func testTransportGateRefusesCommandsOrCredentialForwarding() async throws {
        let underlying = FixtureTransport(try responses())
        let transport = DashboardReadTransport(origin: "https://fixture.example", credential: bearer, underlying: underlying)
        let invalid: [(String, String)] = [("https://fixture.example/api/v1/tasks", "POST"),
                                          ("https://fixture.example/api/v1/tasks/cancel", "GET"),
                                          ("https://elsewhere.example/api/v1/tasks", "GET"),
                                          ("https://fixture.example/api/v1/tasks?token=test", "GET")]
        for (url, method) in invalid {
            var request = URLRequest(url: URL(string: url)!)
            request.httpMethod = method
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
            do { _ = try await transport.send(request); XCTFail("Expected request gate rejection") }
            catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        }
        let count = await underlying.requests.count
        XCTAssertEqual(count, 0)
    }

    func testRedirectDelegateNeverForwardsRequest() {
        let blocker = DashboardRedirectBlocker()
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let url = URL(string: "https://fixture.example/api/v1/tasks")!
        let task = session.dataTask(with: url)
        let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: nil,
                                       headerFields: ["Location": "https://elsewhere.example"])!
        var called = false
        blocker.urlSession(session, task: task, willPerformHTTPRedirection: response,
                           newRequest: URLRequest(url: URL(string: "https://elsewhere.example")!)) { request in
            called = true
            XCTAssertNil(request)
        }
        XCTAssertTrue(called)
    }

    func testNetworkErrorsUseSafeCategories() async throws {
        let cases: [(URLError.Code, DashboardError)] = [(.timedOut, .timeout), (.cannotConnectToHost, .network),
                                                       (.serverCertificateUntrusted, .tls)]
        for (code, expected) in cases {
            let client = try DashboardClient(settings: settings(), transport: FailedTransport(code: code))
            do { _ = try await client.fetch(); XCTFail("Expected network error") }
            catch { XCTAssertEqual(error as? DashboardError, expected) }
        }
    }

    func testCancellationRemainsCancellation() async throws {
        let client = try DashboardClient(settings: settings(), transport: FailedTransport(code: .cancelled))
        do { _ = try await client.fetch(); XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }
}

private actor FixtureTransport: ReviewTransport {
    let responses: [String: HTTPResult]
    var requests: [URLRequest] = []
    init(_ responses: [String: HTTPResult]) { self.responses = responses }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        return responses[request.url!.path] ?? HTTPResult(status: 404, body: Data())
    }
}

private struct FailedTransport: ReviewTransport {
    let code: URLError.Code
    func send(_ request: URLRequest) async throws -> HTTPResult {
        throw URLError(code, userInfo: [NSLocalizedDescriptionKey: "Unsafe upstream detail must be discarded"])
    }
}
