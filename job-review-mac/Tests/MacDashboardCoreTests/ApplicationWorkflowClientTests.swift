import Foundation
import ReviewCore
import XCTest
@testable import MacDashboardCore

final class ApplicationWorkflowClientTests: XCTestCase {
    private let credential = String(repeating: "W", count: 43)
    private let token = "fictional-test-only"
    private var directory: URL!
    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("credential")
        try credential.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: directory) }
    private func client(_ transport: any ReviewTransport) throws -> ApplicationWorkflowClient {
        try ApplicationWorkflowClient(settings: ConnectionSettings(origin: "https://fixture.example", credentialFile: directory.appendingPathComponent("credential").path), transport: transport)
    }
    private func fixture() throws -> [String: JSONValue] {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try JSONDecoder().decode([String: JSONValue].self, from: Data(contentsOf: root.appendingPathComponent("job-review/fixtures/application-workflow.json")))
    }
    private func response(_ object: [String: JSONValue]) throws -> HTTPResult { HTTPResult(status: 200, body: try JSONEncoder().encode(object)) }
    private func capabilities(_ value: JSONValue? = .bool(true)) throws -> HTTPResult {
        var object = try fixture(); object.removeValue(forKey: "application_workflow")
        var caps: [String: JSONValue] = ["api_version": .number(1)]
        caps["application_workflow"] = value; object["capabilities"] = .object(caps)
        return try response(object)
    }
    func testCapabilityAbsentFalseAndLegacyNeverRequestWorkflow() async throws {
        for cap in [try capabilities(nil), try capabilities(.bool(false)), HTTPResult(status: 404, body: Data())] {
            let transport = ObservationTransport([cap])
            let result = try await client(transport).fetch()
            XCTAssertEqual(result, .unsupported)
            let requests = await transport.requests
            XCTAssertEqual(requests.map { $0.url!.path }, ["/api/v1/capabilities"])
        }
    }
    func testCanonicalBackendFixtureDecodesAndRequestsAreFixedAuthenticatedGETs() async throws {
        let transport = ObservationTransport([try capabilities(), try response(fixture())])
        let client = try client(transport)
        guard case .observed(let result) = try await client.fetch() else { return XCTFail("Expected observation") }
        XCTAssertEqual(result.workers.count, 3); XCTAssertEqual(result.workers[1].state, "paused")
        XCTAssertEqual(result.questionLock.ownerKind, "answer_handler")
        let requests = await transport.requests
        XCTAssertEqual(requests.map { $0.url!.path }, ["/api/v1/capabilities", "/api/v1/application-workflow"])
        for request in requests {
            XCTAssertEqual(request.httpMethod, "GET"); XCTAssertNil(request.httpBody); XCTAssertNil(request.url?.query)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(credential)")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        }
        XCTAssertFalse(String(reflecting: client).contains(credential))
    }
    func testSupportIsDiscoveredAgainAfterPreviousSuccess() async throws {
        let transport = ObservationTransport([try capabilities(), try response(fixture()), try capabilities(.bool(false))])
        let client = try client(transport); _ = try await client.fetch()
        let second = try await client.fetch(); XCTAssertEqual(second, .unsupported)
        let count = await transport.requests.count; XCTAssertEqual(count, 3)
    }
    func testMalformedCapabilityCannotAuthorizeEndpoint() async throws {
        for cap in [JSONValue.string("true"), .number(1), .null] {
            let transport = ObservationTransport([try capabilities(cap)])
            do { _ = try await client(transport).fetch(); XCTFail("Expected error") } catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
            let count = await transport.requests.count; XCTAssertEqual(count, 1)
        }
    }
    func testMissingOrIncompleteEnvelopeNeverLooksSupported() async throws {
        for key in ["schema_version", "complete", "next_cursor", "revision", "token"] {
            var object = try fixture(); object.removeValue(forKey: key)
            let transport = ObservationTransport([try capabilities(), try response(object)])
            do { _ = try await client(transport).fetch(); XCTFail("Missing \(key)") } catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
        for patch in [["complete": JSONValue.bool(false)], ["next_cursor": .string("more")], ["schema_version": .number(2)]] {
            var object = try fixture(); object.merge(patch) { _, b in b }
            do { _ = try await client(ObservationTransport([try capabilities(), try response(object)])).fetch(); XCTFail("Bad envelope") } catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }
    func testInvalidSlotsCountsDatesAndMissingNullableFieldsAreRejected() throws {
        let original = try XCTUnwrap(fixture()["application_workflow"])
        var variants: [JSONValue] = []
        for (key, value) in [("pool_size", JSONValue.number(4)), ("revision", .number(-1)), ("generated_at", .string("yesterday"))] {
            var changed = original.object; changed[key] = value; variants.append(.object(changed))
        }
        for key in ["task_id", "process_id", "stage_started_at", "company", "updated_at"] {
            var changed = original.object, workers = original.object["workers"]!.array
            var worker = workers[0].object; worker.removeValue(forKey: key); workers[0] = .object(worker)
            changed["workers"] = .array(workers); variants.append(.object(changed))
        }
        var duplicate = original.object; duplicate["workers"] = .array(Array(repeating: original.object["workers"]!.array[0], count: 3)); variants.append(.object(duplicate))
        var negative = original.object; negative["question_pool"] = .object(["pending_count": .number(-1), "answered_count": .number(0), "revision": .number(1)]); variants.append(.object(negative))
        for value in variants { XCTAssertThrowsError(try ApplicationWorkflowClient.decode(value)) }
    }
    func testUnknownEnumsRetainObservationWithoutInventingKnownState() throws {
        var raw = try XCTUnwrap(fixture()["application_workflow"]).object
        var workers = raw["workers"]!.array, worker = raw["workers"]!.array[0].object
        worker["state"] = .string("future_state"); worker["stage"] = .string("future_stage"); workers[0] = .object(worker); raw["workers"] = .array(workers)
        let value = try ApplicationWorkflowClient.decode(.object(raw))
        XCTAssertFalse(value.workers[0].knownState); XCTAssertFalse(value.workers[0].knownStage)
        XCTAssertEqual(value.workers[0].stage, "future_stage")
    }
    func testDiagnosticsDiscardUnexpectedSecretsAndPrivateFields() async throws {
        var object = try fixture(), raw = try XCTUnwrap(fixture()["application_workflow"]).object
        var workers = raw["workers"]!.array, worker = workers[0].object
        worker["company"] = .string("\(credential) \(token)"); workers[0] = .object(worker); raw["workers"] = .array(workers)
        raw["answer_text"] = .string("PRIVATE ANSWER"); raw["local_path"] = .string("/private/answers.md")
        object["application_workflow"] = .object(raw)
        guard case .observed(let result) = try await client(ObservationTransport([try capabilities(), try response(object)])).fetch() else { return XCTFail() }
        let encoded = String(decoding: try JSONEncoder().encode(result), as: UTF8.self)
        for secret in [credential, token, "PRIVATE ANSWER", "/private/answers.md"] { XCTAssertFalse(encoded.contains(secret)) }
    }
    func testHTTPFailureDoesNotFallBackToFakeIdleOrExposeRawBody() async throws {
        for status in [301, 401, 403, 404, 500] {
            let transport = ObservationTransport([try capabilities(), HTTPResult(status: status, body: Data(credential.utf8))])
            do { _ = try await client(transport).fetch(); XCTFail() }
            catch { XCTAssertEqual(error as? DashboardError, [401, 403].contains(status) ? .authentication : .http(status)); XCTAssertFalse(error.localizedDescription.contains(credential)) }
        }
    }
    private func browserAccess() -> JSONValue {
        func ticket(_ id: String, _ worker: JSONValue, _ state: String) -> JSONValue {
            .object(["ticket_id": .string(id), "task_id": .string("task-" + id), "worker_id": worker,
                     "role": .string(worker == .null ? "discovery" : "apply"), "enqueued_at": .string("2026-10-25T20:00:00Z"),
                     "acquired_at": .null, "started_at": .null, "state": .string(state)])
        }
        return .object(["capacity": .number(1), "policy": .string("chrome_fifo_v1"), "revision": .number(3), "state": .string("busy"),
                        "owner": ticket("owner", .string("apply-1"), "active"),
                        "queue": .array([ticket("search", .null, "waiting"), ticket("apply", .string("apply-2"), "waiting")]),
                        "blocked_by": .array([.object(["task_id": .string("uncertain-task"), "resource": .string("browser")])])])
    }
    func testBrowserAccessIsAdditiveAndPreservesFIFOIncludingDiscovery() throws {
        var raw = try XCTUnwrap(fixture()["application_workflow"]).object
        raw.removeValue(forKey: "browser_access")
        XCTAssertNil(try ApplicationWorkflowClient.decode(.object(raw)).browserAccess)
        raw["browser_access"] = browserAccess()
        let access = try XCTUnwrap(ApplicationWorkflowClient.decode(.object(raw)).browserAccess)
        XCTAssertEqual(access.capacity, 1); XCTAssertEqual(access.owner?.workerId, "apply-1")
        XCTAssertEqual(access.queue.map(\.id), ["search", "apply"])
        XCTAssertNil(access.queue[0].workerId); XCTAssertEqual(access.queue[0].role, "discovery")
        XCTAssertEqual(access.blockedBy[0].taskId, "uncertain-task")
    }
    func testIncompleteBrowserTelemetryIsRejectedRatherThanShownAsFree() throws {
        var raw = try XCTUnwrap(fixture()["application_workflow"]).object
        let base = browserAccess().object
        var variants: [JSONValue] = [.null]
        for key in ["capacity", "policy", "revision", "state", "owner", "queue", "blocked_by"] {
            var access = base; access.removeValue(forKey: key); variants.append(.object(access))
        }
        for (key, value) in [("capacity", JSONValue.number(2)), ("revision", .number(-1))] {
            var access = base; access[key] = value; variants.append(.object(access))
        }
        for key in ["worker_id", "enqueued_at", "acquired_at", "started_at"] {
            var access = base, owner = base["owner"]!.object
            owner.removeValue(forKey: key); access["owner"] = .object(owner); variants.append(.object(access))
        }
        var duplicate = base; duplicate["queue"] = .array([base["owner"]!]); variants.append(.object(duplicate))
        var badDate = base, owner = base["owner"]!.object; owner["started_at"] = .string("yesterday")
        badDate["owner"] = .object(owner); variants.append(.object(badDate))
        for variant in variants {
            raw["browser_access"] = variant
            XCTAssertThrowsError(try ApplicationWorkflowClient.decode(.object(raw)))
        }
    }
    func testFutureBrowserStateRemainsUnknownAndPrivateFieldsAreDiscarded() throws {
        var raw = try XCTUnwrap(fixture()["application_workflow"]).object
        var access = browserAccess().object
        access["state"] = .string("future_state"); access["private_prompt"] = .string("DO NOT STORE")
        var owner = access["owner"]!.object; owner["answer"] = .string("PRIVATE ANSWER"); access["owner"] = .object(owner)
        raw["browser_access"] = .object(access)
        let observation = try ApplicationWorkflowClient.decode(.object(raw))
        XCTAssertFalse(ApplicationBrowserAccess.states.contains(observation.browserAccess!.state))
        let saved = String(decoding: try JSONEncoder().encode(observation), as: UTF8.self)
        XCTAssertFalse(saved.contains("DO NOT STORE")); XCTAssertFalse(saved.contains("PRIVATE ANSWER"))
    }
}
private actor ObservationTransport: ReviewTransport {
    var responses: [HTTPResult]
    var requests: [URLRequest] = []
    init(_ responses: [HTTPResult]) { self.responses = responses }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !responses.isEmpty else { throw URLError(.cannotConnectToHost) }
        return responses.removeFirst()
    }
}
