import Foundation
import XCTest
@testable import MacDashboardCore
@testable import ReviewCore

final class QuestionSyncCoreTests: XCTestCase {
    private func fixture() throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: Bundle.module.url(forResource: "question-sync", withExtension: "json", subdirectory: "Fixtures")!)) as? [String: Any])
    }
    private func api(_ peer: any ReviewTransport) throws -> WorkflowAPI {
        WorkflowAPI(address: try ServerAddress("https://sync.example.test"), transport: peer)
    }
    private func session(_ peer: SyncPeer, _ cache: SyncCache = SyncCache()) throws -> WorkflowSession {
        try WorkflowSession(api: api(peer), persistence: cache)
    }
    func testExactFixtureKeepsServerMappedOptionsAndBlockedForm() async throws {
        let peer = try SyncPeer(snapshot: fixture())
        let snapshot = try await api(peer).fetchQuestionSync().value
        XCTAssertEqual(snapshot.questions?.map(\.answer), [.string("yes-0"), .string("yes-1")])
        XCTAssertEqual(snapshot.questions?.map(\.snapshotPoolRevision), [4, 4])
        XCTAssertEqual(snapshot.processes?.last?.blockReason, "tool_approval_required")
        XCTAssertEqual(snapshot.sharedQuestions?.first?.answerDisplayText, "Yes")
    }
    func testUnboundSavedRecordDoesNotRequireAnApplication() async throws {
        var body = try fixture(); var snapshot = body["question_sync"] as! [String: Any]
        var shared = snapshot["shared_questions"] as! [[String: Any]]
        var unbound = shared[0]; unbound["id"] = "saved-personal-unbound"; unbound["binding_ids"] = [String]()
        shared.append(unbound); snapshot["shared_questions"] = shared; body["question_sync"] = snapshot
        let peer = try SyncPeer(snapshot: body)
        let view = try await session(peer).refresh()
        XCTAssertEqual(view.sharedQuestions.count, 2)
        XCTAssertEqual(view.sharedQuestions.last?.bindingIDs, [])
        XCTAssertTrue(view.sharedQuestions.last?.hasAcceptedAnswer == true)
    }
    func testHistoricalBindingCanBeReadButCannotAnswerCurrentForm() async throws {
        var body = try fixture(); var snapshot = body["question_sync"] as! [String: Any]
        var questions = snapshot["questions"] as! [[String: Any]]
        questions[0]["form_version"] = "historical-form"
        snapshot["questions"] = questions; body["question_sync"] = snapshot
        let peer = try SyncPeer(snapshot: body, writable: true); let session = try session(peer)
        let view = try await session.refresh()
        XCTAssertEqual(view.questionSyncPhase, .ready)
        XCTAssertEqual(view.questions[0].formVersion, "historical-form")
        do { _ = try await session.answer(process: view.processes[0], questions: [view.questions[0]], values: ["q0": .string("no-0")]); XCTFail("Historical binding must not answer the current form") } catch { }
        let posts = await peer.posts; XCTAssertTrue(posts.isEmpty)
    }
    func testUpdatingAndPendingRetainCompleteSnapshotAndDisableAnswers() async throws {
        let peer = try SyncPeer(snapshot: fixture(), writable: true)
        let session = try session(peer)
        let before = try await session.refresh()
        XCTAssertEqual(before.questionSyncPhase, .ready)
        for (index, state) in ["updating", "pending"].enumerated() {
            await peer.setState(state, revision: 16 + index, poolRevision: 5)
            let current = try await session.refresh()
            XCTAssertFalse(current.questionSyncAllowsAnswers)
            XCTAssertEqual(current.questionSyncMessage, "Waiting for sync…")
            XCTAssertEqual(current.questions, before.questions)
            XCTAssertEqual(current.processes, before.processes)
            XCTAssertEqual(current.sharedQuestions, before.sharedQuestions)
            XCTAssertEqual(current.questionSyncPoolRevision, 4)
            do { _ = try await session.answer(process: before.processes[0], questions: [before.questions[0]], values: ["q0": .string("no-0")]); XCTFail("Sync must block writes") } catch { }
        }
        let posts = await peer.posts
        XCTAssertTrue(posts.isEmpty)
        await peer.setState("ready", revision: 18, poolRevision: 5)
        let ready = try await session.refresh()
        XCTAssertTrue(ready.questionSyncAllowsAnswers)
        XCTAssertEqual(ready.questionSyncPoolRevision, 5)
    }
    func testRevisionHighWaterSurvivesSessionRecreation() async throws {
        let peer = try SyncPeer(snapshot: fixture()); let cache = SyncCache()
        let first = try session(peer, cache)
        let before = try await first.refresh()
        await peer.setState("updating", revision: 20, poolRevision: 4)
        _ = try await first.refresh()
        let restored = try session(peer, cache)
        let cached = await restored.view()
        XCTAssertFalse(cached.questionSyncAllowsAnswers)
        await peer.setState("ready", revision: 19, poolRevision: 4)
        let rejected = try await restored.refresh()
        XCTAssertEqual(rejected.questionSyncPhase, .failed)
        XCTAssertEqual(rejected.questionSyncRevision, 20)
        XCTAssertEqual(rejected.questions, before.questions)
        await peer.setState("ready", revision: 21, poolRevision: 5)
        let recovered = try await restored.refresh()
        XCTAssertEqual(recovered.questionSyncPhase, .ready)
    }
    func testCapturedPoolRevisionIsNotRetargetedToNewSnapshot() async throws {
        let peer = try SyncPeer(snapshot: fixture(), writable: true); let session = try session(peer)
        let before = try await session.refresh()
        await peer.setState("ready", revision: 17, poolRevision: 5)
        _ = try await session.refresh()
        do { _ = try await session.answer(process: before.processes[0], questions: [before.questions[0]], values: ["q0": .string("no-0")]); XCTFail("Captured revision is stale") } catch { }
        let posts = await peer.posts; XCTAssertTrue(posts.isEmpty)
    }
    func testDefinitiveStaleAnswerRefreshesWithoutKeepingRejectedCommand() async throws {
        let peer = try SyncPeer(snapshot: fixture(), writable: true); let cache = SyncCache(); let session = try session(peer, cache)
        let before = try await session.refresh()
        await peer.setPostMode("stale")
        do { _ = try await session.answer(process: before.processes[0], questions: [before.questions[0]], values: ["q0": .string("no-0")]); XCTFail("Expected stale rejection") }
        catch let error as WorkflowHTTPError { XCTAssertTrue(error.isDefinitiveRejection) }
        let view = await session.view()
        XCTAssertNil(view.pending); XCTAssertEqual(view.questionSyncPoolRevision, 5)
        let posts = await peer.posts
        let sent = try JSONDecoder().decode(WorkflowAnswerRequest.self, from: XCTUnwrap(posts.first))
        XCTAssertEqual(sent.expectedPoolRevision, 4)
        XCTAssertEqual(sent.answers[0].value, .string("no-0"))
    }
    func testUncertainRetryKeepsExactBodyAndOriginalPoolRevision() async throws {
        let peer = try SyncPeer(snapshot: fixture(), writable: true); let cache = SyncCache(); let session = try session(peer, cache)
        let before = try await session.refresh(); await peer.setPostMode("lost")
        do { _ = try await session.answer(process: before.processes[0], questions: [before.questions[0]], values: ["q0": .string("no-0")]); XCTFail("Expected lost response") } catch { }
        let pending = try XCTUnwrap(cache.load()?.pending)
        await peer.setState("ready", revision: 19, poolRevision: 5)
        let restored = try self.session(peer, cache)
        let completed = try await restored.retryPending()
        let posts = await peer.posts
        XCTAssertEqual(posts, [pending.body, pending.body]); XCTAssertNil(completed.pending)
        XCTAssertEqual(try JSONDecoder().decode(WorkflowAnswerRequest.self, from: pending.body).expectedPoolRevision, 4)
    }
    func testMalformedReadyAndPartialUpdatingNeverReplaceCachedSnapshot() async throws {
        let peer = try SyncPeer(snapshot: fixture()); let session = try session(peer)
        let before = try await session.refresh()
        for problem in ["missing", "partial", "wrong-binding", "negative"] {
            await peer.setMalformed(problem)
            let result = try await session.refresh()
            XCTAssertEqual(result.questionSyncPhase, .failed)
            XCTAssertEqual(result.questions, before.questions)
            XCTAssertEqual(result.sharedQuestions, before.sharedQuestions)
        }
    }
    func testCapableReadsDoNotMixLegacyFormEndpoints() async throws {
        let peer = try SyncPeer(snapshot: fixture()); _ = try await session(peer).refresh()
        let paths = await peer.paths
        XCTAssertTrue(paths.contains("/api/v1/question-sync"))
        XCTAssertFalse(paths.contains("/api/v1/questions")); XCTAssertFalse(paths.contains("/api/v1/processes"))
    }
    func testUnknownCommitStaleReasonStillRetainsPending() {
        XCTAssertFalse(WorkflowHTTPError(status: 409, code: "conflict", reason: "question_sync_stale", commitStatus: "unknown", message: "fixture").isDefinitiveRejection)
    }
}

private final class SyncCache: WorkflowPersistence, @unchecked Sendable {
    private var saved: SavedWorkflowSession?
    private let lock = NSLock()
    func load() throws -> SavedWorkflowSession? { lock.lock(); defer { lock.unlock() }; return saved }
    func save(_ value: SavedWorkflowSession) throws { lock.lock(); defer { lock.unlock() }; saved = value }
}

private actor SyncPeer: ReviewTransport {
    var snapshot: [String: Any]
    var reads: [String: Data] = [:]
    var mode = "ok"
    var malformed: String?
    private(set) var posts: [Data] = []
    private(set) var paths: [String] = []
    init(snapshot: [String: Any], writable: Bool = false) throws {
        self.snapshot = snapshot
        if writable {
            var value = self.snapshot["question_sync"] as! [String: Any]
            var processes = value["processes"] as! [[String: Any]]
            processes[0]["task_status"] = "waiting_for_user"; processes[0]["block_reason"] = "questions_pending"
            value["processes"] = processes; self.snapshot["question_sync"] = value
        }
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("job-review/fixtures/v1-http.json"))) as! [String: Any]
        for item in fixture["sequence"] as! [[String: Any]] {
            let request = item["request"] as! [String: Any]
            guard request["method"] as? String == "GET" else { continue }
            let path = request["path"] as! String
            guard reads[path] == nil else { continue }
            let response = item["response"] as! [String: Any]
            var body = response["body"] as! [String: Any]
            if var caps = body["capabilities"] as? [String: Any] { caps["question_sync"] = true; body["capabilities"] = caps }
            reads[path] = try JSONSerialization.data(withJSONObject: body)
        }
    }
    func setState(_ state: String, revision: Int, poolRevision: Int) {
        var value = snapshot["question_sync"] as! [String: Any]
        value["sync"] = ["state": state, "revision": revision, "pool_revision": poolRevision]
        var questions = value["questions"] as! [[String: Any]]
        for i in questions.indices { questions[i]["pool_revision"] = poolRevision }
        value["questions"] = questions; snapshot["question_sync"] = value; malformed = nil
    }
    func setPostMode(_ value: String) { mode = value }
    func setMalformed(_ value: String) { malformed = value }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path; paths.append(path)
        if request.httpMethod == "POST" {
            let body = request.httpBody!; posts.append(body)
            let command = try JSONSerialization.jsonObject(with: body) as! [String: Any]
            if mode == "stale" {
                mode = "ok"; setState("ready", revision: 18, poolRevision: 5)
                return HTTPResult(status: 409, body: try JSONSerialization.data(withJSONObject: ["schema_version":1,"error":["code":"conflict","reason":"question_sync_stale","commit_status":"not_committed","message":"Fixture stale"]]))
            }
            if mode == "lost" { mode = "ok"; throw URLError(.timedOut) }
            return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version":1,"accepted":true,"request_id":command["request_id"]!,"process_ids":["process-0"],"task_ids":["task-0"],"token":"fictional-sync-token"]))
        }
        if path == "/api/v1/question-sync" {
            var body = snapshot; var value = body["question_sync"] as! [String: Any]
            let state = (value["sync"] as! [String: Any])["state"] as! String
            if state != "ready" { for key in ["shared_questions","questions","processes"] { value[key] = NSNull() } }
            if malformed == "missing" { value["shared_questions"] = nil }
            if malformed == "partial" { value["sync"] = ["state":"updating","revision":16,"pool_revision":4] }
            if malformed == "wrong-binding" {
                var shared = value["shared_questions"] as! [[String: Any]]; shared[0]["binding_ids"] = ["unknown"]
                value["shared_questions"] = shared
            }
            if malformed == "negative" { value["sync"] = ["state":"ready","revision":-1,"pool_revision":4] }
            body["question_sync"] = value
            return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: body))
        }
        return HTTPResult(status: reads[path] == nil ? 404 : 200, body: reads[path] ?? Data())
    }
}
