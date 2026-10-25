import Foundation
import ReviewCore

final class MemoryPersistence: SessionPersistence, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SavedSession?
    var failWrites = false
    func load() throws -> SavedSession? { lock.lock(); defer { lock.unlock() }; return value }
    func save(_ value: SavedSession) throws {
        lock.lock(); defer { lock.unlock() }
        if failWrites { throw ReviewError.storage("Disk full") }
        self.value = value
    }
}

actor FaultTransport: ReviewTransport {
    let demo: DemoTransport
    var loseNextPost = false
    var rejectNextPost = false
    var nextStatus: Int?
    var failGET = false
    var posts: [URLRequest] = []
    init(state: ReviewState) { demo = DemoTransport(state: state) }
    func losePost() { loseNextPost = true }
    func conflictPost() { rejectNextPost = true }
    func rejectStatus(_ status: Int) { nextStatus = status }
    func goOffline() { failGET = true }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        if request.httpMethod == "POST" {
            posts.append(request)
            if let status = nextStatus {
                nextStatus = nil
                return HTTPResult(status: status, body: Data("{\"error\":\"Credentials or service need attention.\"}".utf8))
            }
            if rejectNextPost {
                rejectNextPost = false
                return HTTPResult(status: 409, body: Data("{\"error\":\"The card changed.\"}".utf8))
            }
            let result = try await demo.send(request)
            if loseNextPost { loseNextPost = false; throw URLError(.networkConnectionLost) }
            return result
        }
        if failGET { throw URLError(.notConnectedToInternet) }
        return try await demo.send(request)
    }
}

struct ReviewChecks {
    func makeSession(storage: MemoryPersistence = MemoryPersistence(), transport: FaultTransport? = nil) throws -> (ReviewSession, MemoryPersistence, FaultTransport) {
        let transport = try transport ?? FaultTransport(state: DemoData.state())
        let api = ReviewAPI(address: try ServerAddress(DemoData.origin), transport: transport, accessToken: "test-only-access-token")
        return (try ReviewSession(api: api, persistence: storage), storage, transport)
    }
    func testDemoContainsNoRealJobsAndRetainsFullDetails() throws {
        let state = try DemoData.state()
        try check(state.companies.count == 3)
        try check(state.jobs.count == 9)
        try check(state.flashcards.first?.jobs.count == 3)
        try check(state.jobs.allSatisfy { $0.officialURL?.host == "example.com" })
        try check(!(state.jobs[0].short("gap").isEmpty))
        try check(try JSONDecoder().decode(ReviewState.self, from: JSONEncoder().encode(state)) == state)
    }
    func testCardPayloadUsesWholeCohortAndOnlyCheckedJobs() throws {
        let card = try require(DemoData.state().flashcards.first)
        let selected = card.jobs[1].key
        let request = try CardRequest(card: card, action: .approve, selected: [selected], requestID: "fixed")
        try check(request.jobs.count == 3)
        try check(request.selected == [selected])
        let data = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as! [String: Any]
        try check(data["request_id"] as? String == "fixed")
        try check(data["company_id"] != nil)
        try check(data["requestID"] == nil)
        try checkThrows { try CardRequest(card: card, action: .approve, selected: []) }
        try checkThrows { try CardRequest(card: card, action: .approve, selected: ["foreign-job"]) }
        try checkThrows { try CardRequest(card: card, action: .research, selected: [selected]) }
    }
    func testSwipeRequiresDistanceAndHorizontalIntent() throws {
        try check(SwipeDecision.action(horizontal: 20, vertical: 0, cardWidth: 360) == nil)
        try check(SwipeDecision.action(horizontal: 120, vertical: 110, cardWidth: 360) == nil)
        try check(SwipeDecision.action(horizontal: 0, vertical: 400, cardWidth: 360) == nil)
        try check(SwipeDecision.action(horizontal: 120, vertical: 5, cardWidth: 360) == .approve)
        try check(SwipeDecision.action(horizontal: -120, vertical: 5, cardWidth: 360) == .exclude)
    }
    func testSavedSelectionDoesNotSelectNewJobsOrChangeWithOrder() throws {
        let jobs = try DemoData.state().jobs
        var selection = JobSelection()
        try check(selection.keys.isEmpty)
        try check(selection.reconcile(with: Array(jobs.prefix(4))) == 0)
        try check(selection.keys.isEmpty)

        selection.toggle(jobs[1])
        selection.toggle(jobs[3])
        var unrelated = jobs[0]
        unrelated.revision += 1
        let refreshed = [jobs[4], jobs[3], unrelated, jobs[2], jobs[1]]
        try check(selection.reconcile(with: refreshed) == 0)
        try check(selection.keys == Set([jobs[1].key, jobs[3].key]))

        selection.toggle(jobs[3])
        try check(selection.keys == Set([jobs[1].key]))
        try check(selection.reconcile(with: refreshed) == 0)
        try check(selection.keys == Set([jobs[1].key]))
    }
    func testSavedSelectionRemovesChangedAndMissingJobsWithoutRestoringThem() throws {
        let jobs = try DemoData.state().jobs
        var selection = JobSelection()
        for job in jobs.prefix(3) { selection.toggle(job) }
        var changed = jobs[1]
        changed.revision += 1

        try check(selection.reconcile(with: [changed, jobs[2], jobs[3]]) == 2)
        try check(selection.keys == Set([jobs[2].key]))
        // Neither a returning job nor its previous revision restores a removed pick.
        try check(selection.reconcile(with: jobs) == 0)
        try check(selection.keys == Set([jobs[2].key]))
        try check(selection.reconcile(with: [jobs[0], changed, jobs[2], jobs[3]]) == 0)
        try check(selection.keys == Set([jobs[2].key]))
        try check(selection.reconcile(with: []) == 1)
        try check(selection.keys.isEmpty)
        try check(selection.reconcile(with: jobs) == 0)
        try check(selection.keys.isEmpty)
    }
    func testSavedSelectionRetoggleCapturesNewRevisionAndClearResetsPicks() throws {
        let original = try require(DemoData.state().jobs.first)
        var revised = original
        revised.revision += 1
        var selection = JobSelection()
        selection.toggle(original)
        try check(selection.reconcile(with: [revised]) == 1)
        try check(selection.keys.isEmpty)

        selection.toggle(revised)
        try check(selection.reconcile(with: [revised]) == 0)
        try check(selection.keys == Set([revised.key]))
        try check(selection.reconcile(with: [original]) == 1)
        try check(selection.keys.isEmpty)

        selection.toggle(revised)
        selection.clear()
        try check(selection.keys.isEmpty)
        try check(selection.reconcile(with: [revised]) == 0)
        try check(selection.keys.isEmpty)
        selection.toggle(revised)
        try check(selection.keys == Set([revised.key]))
    }
    func testOriginsRejectInsecureRemoteAndEmbeddedSecrets() throws {
        _ = try ServerAddress("https://review.example.com")
        _ = try ServerAddress("http://127.0.0.1:8766/")
        try checkThrows { try ServerAddress("http://192.168.1.2:8766") }
        try checkThrows { try ServerAddress("https://user:password@example.com") }
        try checkThrows { try ServerAddress("https://example.com?token=secret") }
        try checkThrows { try ServerAddress("https://example.com/api") }
        try checkThrows { try ServerAddress("file:///etc/passwd") }
        try check(Job.safeURL("javascript:alert(1)") == nil)
    }
    func testApprovalDoesNotQueueOrApproveUncheckedJobs() async throws {
        let (session, _, _) = try makeSession()
        try await session.refresh()
        let before = await session.view()
        let card = try require(before.state?.flashcards.first)
        try await session.decide(card: card, action: .approve, selected: [card.jobs[1].key])
        let after = await session.view()
        try check(after.state?.readyJobs.map(\.key) == [card.jobs[1].key])
        try check(after.state?.jobs.first(where: { $0.key == card.jobs[0].key })?.decision == "pending")
        try check(after.state?.queue.count == 0)
        try check(after.state?.flashcards.count == 2)
        try check(after.pending == nil)
    }
    func testLostResponseSurvivesRestartAndRetriesIdenticalBytes() async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        await transport.losePost()
        do { try await session.decide(card: card, action: .research, selected: []); record("Should be uncertain") }
        catch { try check(error.localizedDescription.contains("unknown")) }
        let unknown = await session.view()
        try check(unknown.pending != nil)
        try check(unknown.state?.flashcards.count == 3) // No optimistic advance.
        try await session.refresh()
        let refreshed = await session.view()
        try check(refreshed.state?.flashcards.count == 3) // Keep this card until its exact save is confirmed.
        try check(refreshed.pending == unknown.pending)
        do { try await session.decide(card: card, action: .exclude, selected: []); record("Should lock") }
        catch { try check(error as? ReviewError == .pending) }
        let (restarted, _, _) = try makeSession(storage: storage, transport: transport)
        let restoredView = await restarted.view()
        try check(restoredView.pending != nil)
        try await restarted.retryPending()
        let recovered = await restarted.view()
        try check(recovered.pending == nil)
        try check(recovered.state?.queue.count == 1)
        try check(recovered.state?.cardReviews.count == 1)
        let posts = await transport.posts
        try check(posts.count == 2)
        try check(posts[0].httpBody == posts[1].httpBody)
    }
    func testQueueRejectsChangedConfirmationBeforeSending() async throws {
        let (session, _, transport) = try makeSession()
        try await session.refresh()
        let initial = await session.view()
        let card = try require(initial.state?.flashcards.first)
        let keys = Set(card.jobs.prefix(2).map(\.key))
        try await session.decide(card: card, action: .approve, selected: keys)
        let approved = await session.view()
        let jobs = try require(approved.state?.readyJobs)
        let expected = jobs.map(JobVersion.init)
        var stale = expected
        stale[0].revision -= 1
        let sentBefore = await transport.posts.count
        do {
            try await session.queue(keys: keys, expectedVersions: stale)
            record("Changed job versions must not be queued")
        } catch {
            guard case .conflict = error as? ReviewError else { record("Expected a confirmation conflict"); return }
        }
        let rejected = await session.view()
        let sentAfter = await transport.posts.count
        try check(sentBefore == sentAfter)
        try check(rejected.pending == nil)
        try check(rejected.state?.readyJobs == jobs)
        // Display order can differ from server order without changing the selection.
        try await session.queue(keys: keys, expectedVersions: expected.reversed())
        let queued = await session.view()
        try check(Set(queued.state?.queue.compactMap(\.jobKey) ?? []) == keys)
        let posts = await transport.posts
        let payload = try JSONDecoder().decode(QueueRequest.self, from: require(posts.last?.httpBody))
        try check(payload.jobs.sorted { $0.key < $1.key } == expected.sorted { $0.key < $1.key })
    }
    func testQueueIsExplicitAndPartialAndDuplicateSafe() async throws {
        let (session, _, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        try await session.decide(card: card, action: .approve, selected: Set(card.jobs.map(\.key)))
        await transport.losePost()
        do { try await session.queue(keys: [card.jobs[2].key]); record("Should be uncertain") } catch {}
        try await session.retryPending()
        let queuedView = await session.view()
        let state = try require(queuedView.state)
        try check(state.queue.count == 1)
        try check(state.queue[0].jobKey == card.jobs[2].key)
        try check(state.queue[0].status == "queued")
        try check(state.readyJobs.count == 2)
        do { try await session.queue(keys: [card.jobs[2].key]); record("Duplicate must fail") } catch {}
    }
    func testConflictLoadsCurrentStateWithoutReplayingChoice() async throws {
        let (session, _, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        await transport.conflictPost()
        do { try await session.decide(card: card, action: .exclude, selected: []); record("Expected conflict") }
        catch { try check(error as? ReviewError == .conflict("The card changed.")) }
        let view = await session.view()
        try check(view.pending == nil)
        try check(view.online)
        try check(view.state?.flashcards.count == 3)
        let postCount = await transport.posts.count
        try check(postCount == 1)
    }
    func testOfflineSnapshotCannotWriteAndHasNoSessionToken() async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        try check(try storage.load()?.state?.token == nil)
        await transport.goOffline()
        do { try await session.refresh(); record("Expected offline") } catch {}
        let view = await session.view()
        try check(!(view.online))
        try check(view.state != nil)
        let card = try require(view.state?.flashcards.first)
        do { try await session.decide(card: card, action: .exclude, selected: []); record("Offline write") }
        catch { try check(error as? ReviewError == .offline) }
        let postCount = await transport.posts.count
        try check(postCount == 0)
    }
    func testDiskFailurePreventsMutation() async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        storage.failWrites = true
        do { try await session.decide(card: card, action: .exclude, selected: []); record("Expected disk error") } catch {}
        let postCount = await transport.posts.count
        try check(postCount == 0)
        let failedView = await session.view()
        try check(failedView.pending == nil)
    }
    func testRequestsUseOriginAndTokensButStorageDoesNot() async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        try await session.decide(card: card, action: .research, selected: [])
        let sent = await transport.posts
        let post = try require(sent.first)
        try check(post.value(forHTTPHeaderField: "Origin") == DemoData.origin)
        try check(post.value(forHTTPHeaderField: "X-Review-Token") == "demo-session")
        try check(post.value(forHTTPHeaderField: "Authorization") == "Bearer test-only-access-token")
        let persisted = String(data: try JSONEncoder().encode(storage.load()), encoding: .utf8)!
        try check(!(persisted.contains("demo-session")))
        try check(!(persisted.contains("test-only-access-token")))
    }
    func testFileStoragePartitionsOriginsAndReportsCorruption() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let one = try FilePersistence(directory: directory, origin: DemoData.origin)
        let two = try FilePersistence(directory: directory, origin: "https://another.example")
        try one.save(SavedSession(origin: DemoData.origin, state: DemoData.state()))
        try check(try one.load()?.state?.token == nil)
        try check(try two.load() == nil)
        try Data("broken".utf8).write(to: one.file)
        try checkThrows { try one.load() }
        try check(FileManager.default.fileExists(atPath: one.file.path))
    }
    func testAuthenticationFailurePreservesOriginalSaveAndAllowsNewCredential() async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        await transport.losePost()
        do { try await session.decide(card: card, action: .research, selected: []); record("Expected lost response") } catch {}
        let original = try require(storage.load()?.pending)
        await transport.rejectStatus(403)
        do { try await session.retryPending(); record("Expected token error") } catch {}
        try check(try storage.load()?.pending == original)
        let newAPI = ReviewAPI(address: try ServerAddress(DemoData.origin), transport: transport, accessToken: "replacement-token")
        let reconnected = try ReviewSession(api: newAPI, persistence: storage)
        try await reconnected.retryPending()
        let recovered = await reconnected.view()
        try check(recovered.pending == nil)
        try check(recovered.state?.queue.count == 1)
        let requests = await transport.posts
        try check(requests.count == 3)
        try check(requests.allSatisfy { $0.httpBody == original.body })
        try check(requests.last?.value(forHTTPHeaderField: "Authorization") == "Bearer replacement-token")
    }
    func transientFailureKeepsPending(status: Int) async throws {
        let (session, storage, transport) = try makeSession()
        try await session.refresh()
        let snapshot = await session.view()
        let card = try require(snapshot.state?.flashcards.first)
        await transport.rejectStatus(status)
        do { try await session.decide(card: card, action: .exclude, selected: []); record("Expected failure") } catch {}
        try check(try storage.load()?.pending != nil)
    }
    func testSavedRequestCannotMoveToAnotherServer() throws {
        let storage = MemoryPersistence()
        try storage.save(SavedSession(origin: "https://other.example"))
        try checkThrows { try makeSession(storage: storage) }
    }

    func testIsolatedPythonServerContract() async throws {
        guard let address = ProcessInfo.processInfo.environment["JOB_REVIEW_TEST_SERVER_URL"] else { return }
        let api = ReviewAPI(address: try ServerAddress(address), transport: URLSessionTransport())
        let before = try await api.fetch()
        try check(before.jobs.count == 9)
        try check(Set(before.companies.map(\.id)) == Set(["northstar", "canopy", "fieldwork"]))
        try check(before.jobs.allSatisfy { $0.officialURL?.host == "example.com" && $0.jobID.hasPrefix("demo-") })
        let token = try require(before.token)
        let card = try require(before.flashcards.first)
        let request = try CardRequest(card: card, action: .approve, selected: [card.jobs[1].key])
        let pending = try PendingSave(origin: api.address.origin, path: "api/card", payload: request, summary: "Integration test")
        let saved = try await api.send(pending, sessionToken: token)
        let replay = try await api.send(pending, sessionToken: token)
        try check(saved.cardReviews.count == 1)
        try check(replay.cardReviews.count == 1)
        try check(replay.readyJobs.map(\.key) == [card.jobs[1].key])
        let staleRequest = try CardRequest(card: card, action: .exclude, selected: [])
        let stale = try PendingSave(origin: api.address.origin, path: "api/card", payload: staleRequest, summary: "Stale card")
        do { _ = try await api.send(stale, sessionToken: token); record("Expected conflict") }
        catch { guard case .conflict = error as? ReviewError else { record("Wrong conflict type"); return } }
        let session = try ReviewSession(api: api, persistence: MemoryPersistence())
        try await session.refresh()
        try await session.queue(keys: [card.jobs[1].key])
        let queued = await session.view()
        try check(queued.state?.queue.count == 1)
        try check(queued.state?.queue.first?.snapshot["title"]?.text == card.jobs[1].title)
        try check(queued.state?.queue.first?.status == "queued")
    }
}


struct CheckFailure: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

final class CheckLog: @unchecked Sendable {
    static let shared = CheckLog()
    private let lock = NSLock()
    private var failures: [String] = []
    func append(_ message: String) { lock.lock(); defer { lock.unlock() }; failures.append(message) }
    var messages: [String] { lock.lock(); defer { lock.unlock() }; return failures }
}

func record(_ message: String, file: StaticString = #filePath, line: UInt = #line) {
    CheckLog.shared.append("\(file):\(line): \(message)")
}
func check(_ condition: @autoclosure () throws -> Bool, file: StaticString = #filePath, line: UInt = #line) throws {
    if try !condition() { throw CheckFailure("\(file):\(line): Check failed") }
}
func require<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) throws -> T {
    guard let value else { throw CheckFailure("\(file):\(line): Required value is missing") }
    return value
}
func checkThrows<T>(_ operation: () throws -> T, file: StaticString = #filePath, line: UInt = #line) throws {
    do { _ = try operation() } catch { return }
    throw CheckFailure("\(file):\(line): Expected an error")
}

@main
struct ReviewCoreChecks {
    static func main() async {
        let checks = ReviewChecks()
        var cases: [(String, () async throws -> Void)] = [
            ("testSwipeRequiresDistanceAndHorizontalIntent", { try checks.testSwipeRequiresDistanceAndHorizontalIntent() }),
            ("testSavedSelectionDoesNotSelectNewJobsOrChangeWithOrder", { try checks.testSavedSelectionDoesNotSelectNewJobsOrChangeWithOrder() }),
            ("testSavedSelectionRemovesChangedAndMissingJobsWithoutRestoringThem", { try checks.testSavedSelectionRemovesChangedAndMissingJobsWithoutRestoringThem() }),
            ("testSavedSelectionRetoggleCapturesNewRevisionAndClearResetsPicks", { try checks.testSavedSelectionRetoggleCapturesNewRevisionAndClearResetsPicks() }),
            ("testDemoContainsNoRealJobsAndRetainsFullDetails", { try checks.testDemoContainsNoRealJobsAndRetainsFullDetails() }),
            ("testCardPayloadUsesWholeCohortAndOnlyCheckedJobs", { try checks.testCardPayloadUsesWholeCohortAndOnlyCheckedJobs() }),
            ("testOriginsRejectInsecureRemoteAndEmbeddedSecrets", { try checks.testOriginsRejectInsecureRemoteAndEmbeddedSecrets() }),
            ("testApprovalDoesNotQueueOrApproveUncheckedJobs", { try await checks.testApprovalDoesNotQueueOrApproveUncheckedJobs() }),
            ("testLostResponseSurvivesRestartAndRetriesIdenticalBytes", { try await checks.testLostResponseSurvivesRestartAndRetriesIdenticalBytes() }),
            ("testQueueIsExplicitAndPartialAndDuplicateSafe", { try await checks.testQueueIsExplicitAndPartialAndDuplicateSafe() }),
            ("testQueueRejectsChangedConfirmationBeforeSending", { try await checks.testQueueRejectsChangedConfirmationBeforeSending() }),
            ("testConflictLoadsCurrentStateWithoutReplayingChoice", { try await checks.testConflictLoadsCurrentStateWithoutReplayingChoice() }),
            ("testOfflineSnapshotCannotWriteAndHasNoSessionToken", { try await checks.testOfflineSnapshotCannotWriteAndHasNoSessionToken() }),
            ("testDiskFailurePreventsMutation", { try await checks.testDiskFailurePreventsMutation() }),
            ("testRequestsUseOriginAndTokensButStorageDoesNot", { try await checks.testRequestsUseOriginAndTokensButStorageDoesNot() }),
            ("testFileStoragePartitionsOriginsAndReportsCorruption", { try checks.testFileStoragePartitionsOriginsAndReportsCorruption() }),
            ("testAuthenticationFailurePreservesOriginalSaveAndAllowsNewCredential", { try await checks.testAuthenticationFailurePreservesOriginalSaveAndAllowsNewCredential() }),
            ("testSavedRequestCannotMoveToAnotherServer", { try checks.testSavedRequestCannotMoveToAnotherServer() }),
            ("testIsolatedPythonServerContract", { try await checks.testIsolatedPythonServerContract() }),
            ("transientFailureKeepsPending_401", { try await checks.transientFailureKeepsPending(status: 401) }),
            ("transientFailureKeepsPending_408", { try await checks.transientFailureKeepsPending(status: 408) }),
            ("transientFailureKeepsPending_429", { try await checks.transientFailureKeepsPending(status: 429) }),
            ("transientFailureKeepsPending_502", { try await checks.transientFailureKeepsPending(status: 502) })
        ]
        if ProcessInfo.processInfo.environment["JOB_REVIEW_TEST_SERVER_URL"] == nil {
            cases.removeAll { $0.0 == "testIsolatedPythonServerContract" }
            print("SKIP testIsolatedPythonServerContract: run scripts/verify.py for the isolated HTTP check.")
        }
        cases += WorkflowChecks().cases()
        if ProcessInfo.processInfo.environment["JOB_REVIEW_WORKFLOW_TEST_SERVER_URL"] == nil {
            cases.removeAll { $0.0 == "testWorkflowIsolatedPythonServerContract" }
            print("SKIP testWorkflowIsolatedPythonServerContract: run scripts/verify.py for the isolated v1 HTTP check.")
        }
        var passed = 0
        for (name, operation) in cases {
            let before = CheckLog.shared.messages.count
            do { try await operation() } catch { record("\(name): \(error)") }
            if CheckLog.shared.messages.count == before { print("PASS \(name)"); passed += 1 }
            else { print("FAIL \(name)") }
        }
        for failure in CheckLog.shared.messages { print(failure) }
        print("\(passed)/\(cases.count) checks passed.")
        if passed != cases.count { exit(1) }
    }
}
