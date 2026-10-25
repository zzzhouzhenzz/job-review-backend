import Foundation
import ReviewCore
import XCTest
@testable import MacDashboardCore

final class MacWorkflowClientTests: XCTestCase {
    private var root: URL!
    private let origin = "https://workflow-fixture.example"
    private let bearer = String(repeating: "M", count: 43)

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("workflow-client-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    private func settings() throws -> ConnectionSettings {
        let credential = root.appendingPathComponent("credential")
        try Data(bearer.utf8).write(to: credential)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: credential.path)
        return try ConnectionSettings(origin: origin, credentialFile: credential.path)
    }
    private func setup() throws -> (any MacWorkflowServing, PrivateWorkflowPersistence, WorkflowMacPeer) {
        let cache = try PrivateWorkflowPersistence(directory: root.appendingPathComponent("cache-\(UUID().uuidString)"), origin: origin)
        let peer = try WorkflowMacPeer(cache: cache)
        let session = try MacWorkflowClient.make(settings: settings(), persistence: cache, transport: peer)
        return (session, cache, peer)
    }

    func testRefreshReadsEveryAllowedEndpointAndKeepsCredentialsOutOfCache() async throws {
        let (session, cache, peer) = try setup()
        let view = try await session.refresh()
        XCTAssertTrue(view.online)
        XCTAssertFalse(try XCTUnwrap(view.feed).cards.isEmpty)
        let requests = await peer.requests
        XCTAssertEqual(Set(requests.compactMap { $0.url?.path }), MacWorkflowTransport.readPaths.subtracting(["/api/v1/question-sync"]))
        XCTAssertEqual(requests.count, 6)
        for request in requests {
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(request.httpBody)
            XCTAssertNil(request.value(forHTTPHeaderField: "Origin"))
            XCTAssertNil(request.value(forHTTPHeaderField: "X-Review-Token"))
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(bearer)")
        }
        let saved = String(decoding: try Data(contentsOf: cache.file), as: UTF8.self)
        XCTAssertFalse(saved.contains(bearer))
        XCTAssertFalse(saved.contains(WorkflowMacPeer.csrf))
    }

    func testLostResponseRestoresAndRetriesExactDurableCommand() async throws {
        let (session, cache, peer) = try setup()
        let before = try await session.refresh()
        let card = try XCTUnwrap(before.feed?.cards.first)
        await peer.loseNextResponse()
        do {
            _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], excludeJobKeys: [], note: "Fixture choice")
            XCTFail("Expected lost acknowledgement")
        } catch { XCTAssertEqual(error as? DashboardError, .timeout) }
        let pending = try XCTUnwrap(cache.load()?.pending)
        let restored = try MacWorkflowClient.make(settings: settings(), persistence: cache, transport: peer)
        let restoredView = await restored.view()
        XCTAssertEqual(restoredView.pending, pending)
        let completed = try await restored.retryPending()
        let posts = await peer.posts
        XCTAssertEqual(posts.count, 2)
        XCTAssertEqual(posts[0].httpBody, pending.body)
        XCTAssertEqual(posts[1].httpBody, pending.body)
        XCTAssertEqual(posts[0].value(forHTTPHeaderField: "Origin"), origin)
        XCTAssertEqual(posts[0].value(forHTTPHeaderField: "X-Review-Token"), WorkflowMacPeer.csrf)
        let persisted = await peer.persistedBeforeEveryPost
        let commands = await peer.uniqueCommands
        XCTAssertTrue(persisted)
        XCTAssertEqual(commands, 1)
        XCTAssertNil(completed.pending)
        XCTAssertNotNil(completed.lastAcknowledgement)
        XCTAssertNil(try cache.load()?.pending)
    }

    func testInstructionReadAndLostReceiptRetryUseExactPrivateJournal() async throws {
        let (session, cache, peer) = try setup()
        _ = try await session.refresh()
        let history = try await session.instructions(taskID: "fixture-guidance-task")
        await peer.loseNextResponse()
        do {
            _ = try await session.sendInstructions(target: history.target, text: "I completed sign-in in the existing tab.")
            XCTFail("Expected a lost acknowledgement")
        } catch { XCTAssertEqual(error as? DashboardError, .timeout) }
        let pending = try XCTUnwrap(cache.load()?.pending)
        XCTAssertEqual(pending.path, "api/v1/unblock-instructions")
        let restored = try MacWorkflowClient.make(settings: settings(), persistence: cache, transport: peer)
        let result = try await restored.retryPending()
        let posts = await peer.posts
        XCTAssertEqual(posts.count, 2)
        XCTAssertEqual(posts.map(\.httpBody), [pending.body, pending.body])
        XCTAssertNil(result.pending)
        XCTAssertEqual(result.lastAcknowledgement?.wakeStatus, "saved_only")
        let requests = await peer.requests
        let reads = requests.filter { $0.httpMethod != "POST" && $0.url?.path == MacWorkflowTransport.instructionPath }
        XCTAssertEqual(reads.count, 2, "A fresh GET must precede the first send")
        for request in reads {
            XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems,
                           [URLQueryItem(name: "task_id", value: "fixture-guidance-task")])
        }
        let persisted = await peer.persistedBeforeEveryPost
        XCTAssertTrue(persisted)
        let saved = String(decoding: try Data(contentsOf: cache.file), as: UTF8.self)
        XCTAssertFalse(saved.contains(bearer)); XCTAssertFalse(saved.contains(WorkflowMacPeer.csrf))
    }

    func testStructuredRejectionsPreserveCommitGuarantees() async throws {
        let cases: [(Int, String, String?, String, Bool)] = [
            (409, "conflict", "stale_revision", "not_committed", false),
            (409, "conflict", "operation_rejected", "not_committed", false),
            (409, "conflict", "request_id_conflict", "not_committed", true),
            (409, "conflict", "future_reason", "not_committed", true),
            (409, "conflict", "stale_revision", "unknown", true),
            (400, "invalid_request", nil, "not_committed", false),
            (404, "not_found", nil, "not_committed", false),
            (403, "forbidden", nil, "not_committed", true)
        ]
        for (status, code, reason, commit, keep) in cases {
            let (session, cache, peer) = try setup()
            let before = try await session.refresh()
            let card = try XCTUnwrap(before.feed?.cards.first)
            let body: [String: Any] = ["schema_version": 1, "error": ["code": code, "reason": reason as Any? ?? NSNull(),
                "commit_status": commit, "message": "Unsafe \(bearer) \(WorkflowMacPeer.csrf)"]]
            await peer.reject(HTTPResult(status: status, body: try JSONSerialization.data(withJSONObject: body)))
            do {
                _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], excludeJobKeys: [], note: "")
                XCTFail("Expected rejection")
            } catch let error as WorkflowHTTPError {
                XCTAssertEqual(error.status, status)
                XCTAssertEqual(error.code, code)
                XCTAssertEqual(error.reason, reason)
                XCTAssertEqual(error.commitStatus, commit)
                XCTAssertEqual(error.isDefinitiveRejection, !keep)
                XCTAssertFalse(error.message.contains(bearer))
                XCTAssertFalse(error.message.contains(WorkflowMacPeer.csrf))
            }
            XCTAssertEqual(try cache.load()?.pending != nil, keep)
        }
    }

    func testPlainGatewayErrorKeepsPendingBodyAndSafeHTTPError() async throws {
        let (session, cache, peer) = try setup()
        let before = try await session.refresh()
        let card = try XCTUnwrap(before.feed?.cards.first)
        await peer.reject(HTTPResult(status: 502, body: Data("Private upstream detail \(bearer)".utf8)))
        do {
            _ = try await session.decide(card: card, action: "later", selected: [card.jobs[0].key], excludeJobKeys: [], note: "")
            XCTFail("Expected gateway error")
        } catch let error as WorkflowHTTPError {
            XCTAssertEqual(error.status, 502)
            XCTAssertNil(error.code)
            XCTAssertFalse(error.isDefinitiveRejection)
            XCTAssertFalse(error.message.contains(bearer))
        }
        XCTAssertNotNil(try cache.load()?.pending)
    }

    func testNoPostWhenJournalCannotBeSavedPrivately() async throws {
        let (session, cache, peer) = try setup()
        let before = try await session.refresh()
        let card = try XCTUnwrap(before.feed?.cards.first)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cache.file.deletingLastPathComponent().path)
        do {
            _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], excludeJobKeys: [], note: "")
            XCTFail("Expected journal failure")
        } catch { XCTAssertTrue(error is ReviewError) }
        let postCount = await peer.posts.count
        XCTAssertEqual(postCount, 0)
    }

    func testMismatchedAcknowledgementKeepsPendingCommand() async throws {
        let (session, cache, peer) = try setup()
        let before = try await session.refresh()
        let card = try XCTUnwrap(before.feed?.cards.first)
        await peer.mismatchNextAcknowledgement()
        do {
            _ = try await session.decide(card: card, action: "apply", selected: [card.jobs[0].key], excludeJobKeys: [], note: "")
            XCTFail("Expected acknowledgement mismatch")
        } catch { XCTAssertTrue(error is ReviewError) }
        XCTAssertNotNil(try cache.load()?.pending)
        XCTAssertNil(try cache.load()?.lastAcknowledgement)
    }

    func testTransportGuardsOriginMethodRoutesCredentialsAndCSRF() async throws {
        let (_, _, peer) = try setup()
        let transport = MacWorkflowTransport(origin: origin, credential: bearer, underlying: peer)
        func request(_ path: String, method: String = "GET") -> URLRequest {
            var request = URLRequest(url: URL(string: origin + path)!)
            request.httpMethod = method
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
            return request
        }
        _ = try await transport.send(request("/api/v1/capabilities"))
        var validPost = request("/api/v1/decisions", method: "POST")
        validPost.httpBody = Data("{}".utf8)
        validPost.setValue(origin, forHTTPHeaderField: "Origin")
        validPost.setValue(WorkflowMacPeer.csrf, forHTTPHeaderField: "X-Review-Token")
        validPost.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var invalid = [request("/api/v1/tasks?token=fixture"), request("/api/v1/tasks#fragment"),
                       request("/api/v1/decisions"), request("/api/v1/project-consent"), request("/api/v1/tasks", method: "DELETE"),
                       request("/api/v1/unblock-instructions"), request("/api/v1/unblock-instructions?task_id="),
                       request("/api/v1/unblock-instructions?task_id=one&task_id=two"),
                       request("/api/v1/unblock-instructions?task_id=one&token=secret"),
                       request("/api/v1/unblock-instructions?task_id=%20"),
                       request("/api/v1/unblock-instructions?task_id=" + String(repeating: "a", count: 201)),
                       request("/api/v1/%75nblock-instructions?task_id=one")]
        var foreign = request("/api/v1/tasks"); foreign.url = URL(string: "https://another-fixture.example/api/v1/tasks"); invalid.append(foreign)
        var bearerless = request("/api/v1/tasks"); bearerless.setValue(nil, forHTTPHeaderField: "Authorization"); invalid.append(bearerless)
        var getBody = request("/api/v1/tasks"); getBody.httpBody = Data("{}".utf8); invalid.append(getBody)
        var getCSRF = request("/api/v1/tasks"); getCSRF.setValue(WorkflowMacPeer.csrf, forHTTPHeaderField: "X-Review-Token"); invalid.append(getCSRF)
        var wrongOrigin = validPost; wrongOrigin.setValue("https://another-fixture.example", forHTTPHeaderField: "Origin"); invalid.append(wrongOrigin)
        var wrongCSRF = validPost; wrongCSRF.setValue("not-issued", forHTTPHeaderField: "X-Review-Token"); invalid.append(wrongCSRF)
        var tooLarge = validPost; tooLarge.httpBody = Data(repeating: 0, count: 1_000_001); invalid.append(tooLarge)
        var noBody = validPost; noBody.httpBody = nil; invalid.append(noBody)
        var cookie = validPost; cookie.setValue("session=fixture", forHTTPHeaderField: "Cookie"); invalid.append(cookie)
        var queriedPost = validPost; queriedPost.url = URL(string: origin + "/api/v1/unblock-instructions?task_id=one"); invalid.append(queriedPost)
        for request in invalid {
            do { _ = try await transport.send(request); XCTFail("Expected transport rejection") }
            catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        }
        let requestCount = await peer.requests.count
        XCTAssertEqual(requestCount, 1)
        XCTAssertFalse(String(reflecting: transport).contains(bearer))
    }

    func testTransportPreservesActiveTokenAndRedactsDiagnosticCopies() async throws {
        let body = try JSONSerialization.data(withJSONObject: ["token": WorkflowMacPeer.csrf,
            "diagnostic": "\(bearer) and \(WorkflowMacPeer.csrf)"])
        let transport = MacWorkflowTransport(origin: origin, credential: bearer,
                                             underlying: WorkflowMacFixedTransport(result: HTTPResult(status: 200, body: body)))
        var request = URLRequest(url: URL(string: origin + "/api/v1/tasks")!)
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        let result = try await transport.send(request)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: result.body) as? [String: String])
        XCTAssertEqual(object["token"], WorkflowMacPeer.csrf)
        XCTAssertEqual(object["diagnostic"], "[redacted] and [redacted]")
    }

    func testAllWriteRoutesRequireLatestIssuedToken() async throws {
        let peer = WorkflowMacRotatingTransport()
        let transport = MacWorkflowTransport(origin: origin, credential: bearer, underlying: peer)
        func command(_ path: String, token: String) -> URLRequest {
            var request = URLRequest(url: URL(string: origin + path)!)
            request.httpMethod = "POST"
            request.httpBody = Data("{}".utf8)
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
            request.setValue(origin, forHTTPHeaderField: "Origin")
            request.setValue(token, forHTTPHeaderField: "X-Review-Token")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            return request
        }
        do { _ = try await transport.send(command("/api/v1/decisions", token: "issued-token-1")); XCTFail("No token has been issued") }
        catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        var discover = URLRequest(url: URL(string: origin + "/api/v1/capabilities")!)
        discover.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        _ = try await transport.send(discover)
        for (index, path) in ["/api/v1/answers", "/api/v1/tasks/cancel", "/api/v1/decisions", "/api/v1/unblock-instructions"].enumerated() {
            _ = try await transport.send(command(path, token: "issued-token-\(index + 1)"))
        }
        do { _ = try await transport.send(command("/api/v1/decisions", token: "issued-token-1")); XCTFail("Old token must be rejected") }
        catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        let count = await peer.count
        XCTAssertEqual(count, 5)
    }

    func testTransportRejectsOversizedResponsesAndUnsafeTokens() async throws {
        let bodies = [Data(repeating: 0, count: 8_000_001), Data("{\"token\":\"unsafe\\nheader\"}".utf8), Data("not JSON".utf8)]
        for body in bodies {
            let transport = MacWorkflowTransport(origin: origin, credential: bearer,
                underlying: WorkflowMacFixedTransport(result: HTTPResult(status: 200, body: body)))
            var request = URLRequest(url: URL(string: origin + "/api/v1/tasks")!)
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
            do { _ = try await transport.send(request); XCTFail("Expected invalid response") }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }
}

private struct WorkflowMacFixedTransport: ReviewTransport {
    let result: HTTPResult
    func send(_ request: URLRequest) async throws -> HTTPResult { result }
}

private actor WorkflowMacRotatingTransport: ReviewTransport {
    private(set) var count = 0
    func send(_ request: URLRequest) async throws -> HTTPResult {
        count += 1
        return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["token": "issued-token-\(count)"]))
    }
}

/// All data comes from the checked-in fictional contract fixture. No network is used.
private actor WorkflowMacPeer: ReviewTransport {
    static let csrf = "mac-fictional-csrf-session"
    private let cache: PrivateWorkflowPersistence
    private var reads: [String: HTTPResult] = [:]
    private var acknowledgements: [String: HTTPResult] = [:]
    private var nextRejection: HTTPResult?
    private var loseResponse = false
    private var mismatch = false
    private(set) var requests: [URLRequest] = []
    private(set) var posts: [URLRequest] = []
    private(set) var persistedBeforeEveryPost = true
    var uniqueCommands: Int { acknowledgements.count }

    init(cache: PrivateWorkflowPersistence) throws {
        self.cache = cache
        let repository = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: repository.appendingPathComponent("job-review/fixtures/v1-http.json"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let sequence = try XCTUnwrap(object["sequence"] as? [[String: Any]])
        for name in ["capabilities", "initial_feed", "process_queued", "questions_empty", "tasks_queued"] {
            let item = try XCTUnwrap(sequence.first { $0["name"] as? String == name })
            let request = try XCTUnwrap(item["request"] as? [String: Any])
            let response = try XCTUnwrap(item["response"] as? [String: Any])
            var body = try XCTUnwrap(response["body"] as? [String: Any])
            if var capabilities = body["capabilities"] as? [String: Any] {
                capabilities["unblock_instructions"] = true; body["capabilities"] = capabilities
            }
            body["token"] = Self.csrf
            reads[try XCTUnwrap(request["path"] as? String)] = HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: body))
        }
    }
    func loseNextResponse() { loseResponse = true }
    func mismatchNextAcknowledgement() { mismatch = true }
    func reject(_ result: HTTPResult) { nextRejection = result }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let path = try XCTUnwrap(request.url?.path)
        guard request.httpMethod == "POST" else {
            if path == MacWorkflowTransport.instructionPath {
                let target: [String: Any] = ["task_id": "fixture-guidance-task", "task_updated_at": "2026-10-19T20:00:00Z",
                    "task_status": "waiting_for_user", "process_id": NSNull(), "process_revision": NSNull(),
                    "block_reason": "tool_approval_required", "can_submit": true, "mode": "manual_review", "unavailable_reason": NSNull()]
                return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version": 1,
                    "revision": "fixture-guidance-revision", "complete": true, "next_cursor": NSNull(),
                    "token": Self.csrf, "target": target, "instructions": []]))
            }
            return reads[path] ?? HTTPResult(status: 404, body: Data())
        }
        posts.append(request)
        let persistedBody = try cache.load()?.pending?.body
        persistedBeforeEveryPost = persistedBeforeEveryPost && persistedBody == request.httpBody
        if let rejection = nextRejection { nextRejection = nil; return rejection }
        let command = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any])
        let id = try XCTUnwrap(command["request_id"] as? String)
        let response: HTTPResult
        if let existing = acknowledgements[id] { response = existing }
        else if path == MacWorkflowTransport.instructionPath {
            response = HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version": 1,
                "accepted": true, "request_id": id, "instruction_id": "fixture-instruction-id", "task_ids": [command["task_id"]!],
                "process_ids": [], "mode": "manual_review", "wake_status": "saved_only",
                "task_updated_at": "2026-10-19T20:00:01Z", "token": Self.csrf]))
            acknowledgements[id] = response
        }
        else {
            let action = try XCTUnwrap(command["action"] as? String)
            let selected = command["selected"] as? [Any] ?? []
            let body: [String: Any] = ["schema_version": 1, "status": "accepted", "accepted": true,
                "request_id": mismatch ? UUID().uuidString : id, "decision_id": id,
                "card_id": command["card_id"]!, "action": action, "token": Self.csrf,
                "process_ids": action == "apply" ? selected.indices.map { "fixture-process-\($0)" } : [],
                "task_ids": action == "apply" ? selected.indices.map { "fixture-task-\($0)" } : []]
            response = HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: body))
            acknowledgements[id] = response
        }
        if loseResponse { loseResponse = false; throw URLError(.timedOut, userInfo: [NSLocalizedDescriptionKey: "Unsafe fixture upstream detail"]) }
        return response
    }
}
