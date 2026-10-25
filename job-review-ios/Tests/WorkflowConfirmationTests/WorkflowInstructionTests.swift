import XCTest
@testable import ReviewCore
@testable import JobReviewPreview

private let instructionStamp = "2026-10-19T20:00:00.000000+00:00"

private func instructionTarget(process: Bool = true, mode: String = "manual_review") -> WorkflowInstructionTarget {
    WorkflowInstructionTarget(taskID: "fictional-task", taskUpdatedAt: instructionStamp,
        taskStatus: "waiting_for_user", processID: process ? "fictional-process" : nil,
        processRevision: process ? 7 : nil, blockReason: "fictional_blocker", canSubmit: true,
        mode: mode, unavailableReason: nil)
}

private final class InstructionPersistence: WorkflowPersistence, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SavedWorkflowSession?
    var failWrites = false
    func load() throws -> SavedWorkflowSession? { lock.lock(); defer { lock.unlock() }; return value }
    func save(_ value: SavedWorkflowSession) throws {
        lock.lock(); defer { lock.unlock() }
        if failWrites { throw ReviewError.storage("Fictional storage failure") }
        self.value = value
    }
}

private actor InstructionPeer: ReviewTransport {
    let persistence: InstructionPersistence
    var target = instructionTarget()
    var entries: [[String: JSONValue]] = []
    var reads: [String: [String: JSONValue]] = [:]
    var snapshotOverride: [String: JSONValue]?
    var acknowledgementOverride: [String: JSONValue] = [:]
    var loseResponse = false
    var rejection: String?
    var failReads = false
    var supportsInstructions = true
    var receipts: [String: (Data, [String: JSONValue])] = [:]
    var posts: [URLRequest] = []
    var instructionReads: [URLRequest] = []
    var persistedBeforeSend = true

    init(persistence: InstructionPersistence) throws {
        self.persistence = persistence
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let fixture = try JSONDecoder().decode([String: JSONValue].self,
            from: Data(contentsOf: root.appendingPathComponent("job-review/fixtures/v1-http.json")))
        for item in fixture["sequence"]!.array {
            let object = item.object
            guard ["capabilities", "initial_feed", "process_queued", "questions_empty", "tasks_queued"].contains(object["name"]?.text ?? "") else { continue }
            reads[object["request"]!.object["path"]!.text!] = object["response"]!.object["body"]!.object
        }
    }
    func configure(target: WorkflowInstructionTarget? = nil, loseResponse: Bool = false,
                   rejection: String? = nil, supported: Bool = true, ack: [String: JSONValue] = [:]) {
        if let target { self.target = target }
        self.loseResponse = loseResponse; self.rejection = rejection; supportsInstructions = supported
        acknowledgementOverride = ack
    }
    func overrideSnapshot(_ object: [String: JSONValue]) { snapshotOverride = object }
    func disableReads() { failReads = true }
    func snapshot() throws -> [String: JSONValue] {
        var result = try JSONDecoder().decode([String: JSONValue].self, from: JSONEncoder().encode(target))
        // Optional nil fields are mandatory nulls in the wire contract.
        for name in ["process_id", "process_revision", "block_reason", "unavailable_reason"] where result[name] == nil { result[name] = .null }
        return ["target": .object(result), "instructions": .array(entries.map(JSONValue.object))]
    }
    private func response(_ data: [String: JSONValue], status: Int = 200, read: Bool = false) throws -> HTTPResult {
        var object = data
        object["schema_version"] = .number(1); object["token"] = .string("fictional-review-token")
        if read { object["revision"] = .string("fixture-revision"); object["complete"] = .bool(true); object["next_cursor"] = .null }
        return HTTPResult(status: status, body: try JSONEncoder().encode(object))
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path
        if request.httpMethod != "POST" {
            if failReads { throw URLError(.notConnectedToInternet) }
            if path == "/api/v1/unblock-instructions" {
                instructionReads.append(request)
                return try response(snapshotOverride ?? snapshot(), read: true)
            }
            if path == "/api/v1/health" { return HTTPResult(status: 404, body: Data()) }
            guard var body = reads[path] else { throw URLError(.badURL) }
            if var capabilities = body["capabilities"]?.object {
                capabilities["unblock_instructions"] = supportsInstructions ? .bool(true) : nil
                body["capabilities"] = .object(capabilities)
            }
            return try response(body, read: true)
        }
        posts.append(request)
        let persistedBody = try persistence.load()?.pending?.body
        persistedBeforeSend = persistedBeforeSend && persistedBody == request.httpBody
        if let reason = rejection {
            rejection = nil
            return try response(["error": .object(["code": .string("conflict"), "reason": .string(reason),
                "commit_status": .string("not_committed"), "message": .string("Fictional conflict")])], status: 409)
        }
        let data = request.httpBody!
        let command = try JSONDecoder().decode([String: JSONValue].self, from: data)
        let id = command["request_id"]!.text!
        if let receipt = receipts[id] {
            guard receipt.0 == data else { throw ReviewError.conflict("Request ID reused") }
            return try response(receipt.1)
        }
        let wake = target.mode == "browser_recheck" ? "recheck_scheduled" : target.mode == "preparation_retry" ? "retry_scheduled" : "saved_only"
        var receipt: [String: JSONValue] = ["request_id": .string(id), "accepted": .bool(true),
            "instruction_id": .string("instruction-\(receipts.count + 1)"), "task_ids": .array([.string(target.taskID)]),
            "process_ids": .array(target.processID.map { [.string($0)] } ?? []),
            "mode": .string(target.mode), "wake_status": .string(wake), "task_updated_at": .string("2026-10-19T20:00:01Z")]
        for (key, value) in acknowledgementOverride { receipt[key] = value }
        receipts[id] = (data, receipt)
        var entry = command
        entry["id"] = receipt["instruction_id"]; entry["mode"] = receipt["mode"]; entry["wake_status"] = receipt["wake_status"]
        entry["created_at"] = receipt["task_updated_at"]; entry["status"] = .string("current")
        entries.append(entry)
        target.taskUpdatedAt = "2026-10-19T20:00:01Z"
        if loseResponse { loseResponse = false; throw URLError(.networkConnectionLost) }
        return try response(receipt)
    }
}

final class WorkflowInstructionTests: XCTestCase {
    private func setup() throws -> (InstructionPersistence, InstructionPeer, WorkflowAPI, WorkflowSession) {
        let storage = InstructionPersistence()
        let peer = try InstructionPeer(persistence: storage)
        let api = WorkflowAPI(address: try ServerAddress("http://localhost:8973"), transport: peer, accessToken: "fictional-device-token")
        return (storage, peer, api, try WorkflowSession(api: api, persistence: storage))
    }

    func testExploreRequestUsesExactlySixFieldsAndExplicitNulls() throws {
        let request = try WorkflowInstructionRequest(target: instructionTarget(process: false), text: "Use the existing tab.", requestID: "fixture-request")
        let object = try JSONDecoder().decode([String: JSONValue].self, from: JSONEncoder().encode(request))
        XCTAssertEqual(Set(object.keys), ["request_id", "task_id", "task_updated_at", "process_id", "process_revision", "text"])
        XCTAssertEqual(object["process_id"], .null); XCTAssertEqual(object["process_revision"], .null)
    }

    func testTextLimitsAndUnavailableModesFailBeforeSend() throws {
        for text in ["", " \n\t", String(repeating: "x", count: 4_001)] {
            XCTAssertThrowsError(try WorkflowInstructionRequest(target: instructionTarget(), text: text))
        }
        XCTAssertNoThrow(try WorkflowInstructionRequest(target: instructionTarget(), text: String(repeating: "🙂", count: 4_000)))
        var target = instructionTarget(); target.canSubmit = false
        XCTAssertThrowsError(try WorkflowInstructionRequest(target: target, text: "Draft"))
        target.canSubmit = true; target.mode = "future_mode"
        XCTAssertThrowsError(try WorkflowInstructionRequest(target: target, text: "Draft"))
        target.mode = "manual_review"; target.processRevision = nil
        XCTAssertThrowsError(try WorkflowInstructionRequest(target: target, text: "Draft"))
    }

    func testAbsentCapabilityDoesNotCallInstructionEndpoint() async throws {
        let (_, peer, _, session) = try setup()
        await peer.configure(supported: false)
        let view = try await session.refresh()
        XCTAssertNil(view.capabilities?.unblockInstructions)
        do { _ = try await session.instructions(taskID: "fictional-task"); XCTFail("Unsupported server") } catch {}
        let reads = await peer.instructionReads
        XCTAssertTrue(reads.isEmpty)
    }

    func testTaskQueryIsEncodedAndOnlyUsesTheInstructionRoute() async throws {
        let (_, peer, api, _) = try setup()
        var target = instructionTarget(process: false); target.taskID = "task/a+b & c?"
        await peer.configure(target: target)
        let read = try await api.fetchInstructions(taskID: target.taskID)
        XCTAssertEqual(read.value.target.taskID, target.taskID)
        let reads = await peer.instructionReads
        let request = try XCTUnwrap(reads.first)
        XCTAssertEqual(request.url?.path, "/api/v1/unblock-instructions")
        XCTAssertTrue(request.url!.absoluteString.contains("%2B"))
        XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems, [URLQueryItem(name: "task_id", value: target.taskID)])
    }

    func testMalformedOrWrongTaskSnapshotIsRejected() async throws {
        for defect in ["missing_null", "wrong_task", "partial"] {
            let (_, peer, api, _) = try setup()
            var body = try await peer.snapshot()
            var target = body["target"]!.object
            if defect == "missing_null" { target.removeValue(forKey: "unavailable_reason") }
            if defect == "wrong_task" { target["task_id"] = .string("another-task") }
            if defect == "partial" { body.removeValue(forKey: "instructions") }
            body["target"] = .object(target); await peer.overrideSnapshot(body)
            do { _ = try await api.fetchInstructions(taskID: "fictional-task"); XCTFail(defect) } catch {}
        }
    }

    func testFreshReadRejectsChangedTargetWithoutPosting() async throws {
        let (_, peer, _, session) = try setup()
        _ = try await session.refresh()
        let snapshot = try await session.instructions(taskID: "fictional-task")
        var changed = snapshot.target; changed.processRevision = 8
        await peer.configure(target: changed)
        do { _ = try await session.sendInstructions(target: snapshot.target, text: "Keep this draft."); XCTFail("Stale target") } catch {}
        let posts = await peer.posts; let view = await session.view()
        XCTAssertTrue(posts.isEmpty); XCTAssertNil(view.pending)
    }

    func testLostReplyPersistsAndReplaysTheExactRequestAfterRestart() async throws {
        let (storage, peer, api, session) = try setup()
        _ = try await session.refresh()
        let snapshot = try await session.instructions(taskID: "fictional-task")
        await peer.configure(loseResponse: true)
        do { _ = try await session.sendInstructions(target: snapshot.target, text: "Fictional guidance."); XCTFail("Lost reply") } catch {}
        let pending = try XCTUnwrap(storage.load()?.pending)
        XCTAssertEqual(pending.path, "api/v1/unblock-instructions")
        // Feature discovery must not prevent replay of an already accepted request.
        await peer.configure(supported: false)
        let restarted = try WorkflowSession(api: api, persistence: storage)
        let recovered = try await restarted.retryPending()
        let posts = await peer.posts; let count = await peer.receipts.count; let durable = await peer.persistedBeforeSend
        XCTAssertEqual(posts.count, 2); XCTAssertEqual(posts[0].httpBody, posts[1].httpBody)
        XCTAssertEqual(posts[1].httpBody, pending.body); XCTAssertEqual(count, 1); XCTAssertTrue(durable)
        XCTAssertNil(recovered.pending); XCTAssertEqual(recovered.lastAcknowledgement?.wakeStatus, "saved_only")
        XCTAssertEqual(posts[0].value(forHTTPHeaderField: "Origin"), "http://localhost:8973")
        XCTAssertEqual(posts[0].value(forHTTPHeaderField: "X-Review-Token"), "fictional-review-token")
        XCTAssertEqual(posts[0].value(forHTTPHeaderField: "Authorization"), "Bearer fictional-device-token")
    }

    func testDefinitiveConflictsClearPendingButRequestIDConflictDoesNot() async throws {
        for reason in ["stale_revision", "task_busy", "task_not_blocked", "task_closed", "authorization_changed", "request_id_conflict"] {
            let (storage, peer, _, session) = try setup()
            _ = try await session.refresh()
            await peer.configure(rejection: reason)
            do { _ = try await session.sendInstructions(target: instructionTarget(), text: "Draft retained by the editor."); XCTFail(reason) } catch {}
            XCTAssertEqual(try storage.load()?.pending != nil, reason == "request_id_conflict", reason)
        }
    }

    func testMismatchedAcknowledgementKeepsPendingRequest() async throws {
        for overrides: [String: JSONValue] in [
            ["task_ids": .array([.string("other-task")])],
            ["process_ids": .array([])],
            ["mode": .string("reconciliation_only"), "wake_status": .string("retry_scheduled")]
        ] {
            let (storage, peer, _, session) = try setup()
            _ = try await session.refresh(); await peer.configure(ack: overrides)
            do { _ = try await session.sendInstructions(target: instructionTarget(), text: "Fictional guidance."); XCTFail("Mismatched acknowledgement") } catch {}
            XCTAssertNotNil(try storage.load()?.pending)
        }
    }

    func testStorageFailureDoesNotPost() async throws {
        let (storage, peer, _, session) = try setup()
        _ = try await session.refresh(); storage.failWrites = true
        do { _ = try await session.sendInstructions(target: instructionTarget(), text: "Fictional guidance."); XCTFail("Disk failure") } catch {}
        let posts = await peer.posts
        XCTAssertTrue(posts.isEmpty)
    }

    func testAllModesReturnHistoryAndAnAccurateReceipt() async throws {
        for mode in WorkflowInstructionTarget.supportedModes {
            let (_, peer, _, session) = try setup()
            let target = instructionTarget(mode: mode)
            await peer.configure(target: target); _ = try await session.refresh()
            let result = try await session.sendInstructions(target: target, text: "Use Back, then review the current form.")
            XCTAssertNil(result.pending); XCTAssertEqual(result.lastAcknowledgement?.mode, mode)
            let history = try await session.instructions(taskID: target.taskID)
            XCTAssertEqual(history.instructions.count, 1)
            XCTAssertEqual(history.instructions[0].text, "Use Back, then review the current form.")
            XCTAssertEqual(history.instructions[0].status, "current")
            let wake = result.lastAcknowledgement!.wakeStatus!
            let message = workflowInstructionReceipt(mode: mode, wakeStatus: wake)
            XCTAssertFalse(message.contains("has resumed"))
            if mode == "questions_pending" { XCTAssertTrue(message.contains("Questions")) }
            if mode == "reconciliation_only" { XCTAssertTrue(message.contains("No new submission")) }
        }
    }
}
