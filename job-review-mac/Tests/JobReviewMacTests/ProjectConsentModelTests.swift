import XCTest
@testable import JobReviewMac
import MacDashboardCore

final class ProjectConsentModelTests: XCTestCase {
    @MainActor func testFreshUsePresentsOnlyAfterSuccessfulServerRead() async {
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        XCTAssertFalse(model.isPresented)
        let service = MemoryConsentService()
        await model.load(client: service, origin: "https://fixture.example")
        XCTAssertTrue(model.isPresented)
        XCTAssertEqual(model.status, "Agreement required")
        let requests = await service.requests
        XCTAssertTrue(requests.isEmpty)
    }

    @MainActor func testAgreeThenRestartUsesServerStateAndDoesNotAskAgain() async {
        let service = MemoryConsentService()
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: service, origin: "https://fixture.example")
        await model.agree()
        XCTAssertEqual(model.consent?.granted, true)
        XCTAssertFalse(model.isPresented)
        let restarted = ProjectConsentModel(intentStore: MemoryIntentStore())
        await restarted.load(client: service, origin: "https://fixture.example")
        XCTAssertEqual(restarted.consent?.granted, true)
        XCTAssertFalse(restarted.isPresented)
        let requests = await service.requests
        XCTAssertEqual(requests.count, 1)
    }

    @MainActor func testDeclineDoesNotSaveOrRepromptDuringPollAndCanReviewLater() async {
        let service = MemoryConsentService()
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: service, origin: "https://fixture.example")
        model.dismiss()
        XCTAssertFalse(model.isPresented)
        await model.load(client: service, origin: "https://fixture.example")
        XCTAssertFalse(model.isPresented)
        XCTAssertEqual(model.consent?.granted, false)
        let requests = await service.requests
        XCTAssertTrue(requests.isEmpty)
        model.review()
        XCTAssertTrue(model.isPresented)
        let restarted = ProjectConsentModel(intentStore: MemoryIntentStore())
        await restarted.load(client: service, origin: "https://fixture.example")
        XCTAssertTrue(restarted.isPresented)
    }

    @MainActor func testFailedSaveKeepsModalAndRetriesIdenticalRequestID() async {
        let service = MemoryConsentService(failSave: true)
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: service, origin: "https://fixture.example")
        await model.agree()
        XCTAssertFalse(model.isSaving)
        XCTAssertTrue(model.isPresented)
        XCTAssertEqual(model.consent?.granted, false)
        XCTAssertNotNil(model.errorMessage)
        await service.allowSave()
        await model.agree()
        let requests = await service.requests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0], requests[1])
        XCTAssertEqual(model.consent?.granted, true)
        XCTAssertFalse(model.isPresented)
    }

    @MainActor func testLostSaveResponseCanBeConfirmedByNextRead() async {
        let service = MemoryConsentService(failSave: true, saveBeforeFailure: true)
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: service, origin: "https://fixture.example")
        await model.agree()
        XCTAssertTrue(model.isPresented)
        await model.load(client: service, origin: "https://fixture.example")
        XCTAssertFalse(model.isPresented)
        XCTAssertEqual(model.consent?.granted, true)
        let requests = await service.requests
        XCTAssertEqual(requests.count, 1)
    }

    @MainActor func testExistingGrantAndChangedOriginDoNotShareLocalAgreement() async {
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: MemoryConsentService(granted: true), origin: "https://first.example")
        XCTAssertFalse(model.isPresented)
        XCTAssertEqual(model.status, "Agreed")
        await model.load(client: MemoryConsentService(), origin: "https://second.example")
        XCTAssertEqual(model.consent?.granted, false)
        XCTAssertTrue(model.isPresented)
    }

    @MainActor func testFailedInitialReadNeverShowsAgreeOrClaimsPermission() async {
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: MemoryConsentService(failRead: true), origin: "https://fixture.example")
        XCTAssertNil(model.consent)
        XCTAssertFalse(model.isPresented)
        XCTAssertEqual(model.status, "Could not verify permissions")
    }

    @MainActor func testPendingAgreeSurvivesRelaunchAndRetriesWithoutAnotherClick() async {
        let store = MemoryIntentStore()
        let service = MemoryConsentService(failSave: true)
        let first = ProjectConsentModel(intentStore: store)
        await first.load(client: service, origin: "https://fixture.example")
        await first.agree()
        XCTAssertNotNil(store.intent)
        await service.allowSave()
        let restarted = ProjectConsentModel(intentStore: store)
        await restarted.load(client: service, origin: "https://fixture.example")
        XCTAssertEqual(restarted.consent?.granted, true)
        XCTAssertFalse(restarted.isPresented)
        XCTAssertNil(store.intent)
        let requests = await service.requests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0], requests[1])
    }

    @MainActor func testPendingIntentNeverCrossesOrigin() async {
        let store = MemoryIntentStore()
        store.intent = ConsentIntent(origin: "https://other.example", policyVersion: 1, requestID: UUID().uuidString)
        let service = MemoryConsentService()
        let model = ProjectConsentModel(intentStore: store)
        await model.load(client: service, origin: "https://fixture.example")
        let requests = await service.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertEqual(model.consent?.granted, false)
        XCTAssertTrue(model.isPresented)
    }

    @MainActor func testIntentWriteFailureNeverSendsAgreement() async {
        let service = MemoryConsentService()
        let model = ProjectConsentModel(intentStore: FailingIntentStore())
        await model.load(client: service, origin: "https://fixture.example")
        await model.agree()
        let requests = await service.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertEqual(model.consent?.granted, false)
        XCTAssertTrue(model.isPresented)
        XCTAssertNotNil(model.errorMessage)
    }

    @MainActor func testAgreeWaitsForRefreshToFinishAndUsesVerifiedClient() async {
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        await model.load(client: MemoryConsentService(), origin: "https://fixture.example")
        let delayed = DelayedReadConsentService()
        let refresh = Task { await model.load(client: delayed, origin: "https://fixture.example") }
        await delayed.waitUntilReading()
        XCTAssertTrue(model.isLoading)
        await model.agree()
        let before = await delayed.saves
        XCTAssertEqual(before, 0)
        await delayed.finishRead()
        await refresh.value
        XCTAssertFalse(model.isLoading)
        await model.agree()
        XCTAssertEqual(model.consent?.granted, true)
        XCTAssertFalse(model.isPresented)
    }

    @MainActor func testOriginChangeDuringSaveRejectsOldAcknowledgement() async {
        let model = ProjectConsentModel(intentStore: MemoryIntentStore())
        let delayed = DelayedSaveConsentService()
        await model.load(client: delayed, origin: "https://first.example")
        let saving = Task { await model.agree() }
        await delayed.waitUntilSaving()
        await model.load(client: MemoryConsentService(), origin: "https://second.example")
        await delayed.finishSave()
        await saving.value
        XCTAssertEqual(model.consent?.granted, false)
        XCTAssertTrue(model.isPresented)
        XCTAssertFalse(model.isSaving)
    }

    func testPendingIntentFileContainsOnlyFixedIntentAndUsesPrivatePermissions() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ConsentIntentFile(url: directory.appendingPathComponent("pending.json"))
        let intent = ConsentIntent(origin: "https://fixture.example", policyVersion: 1, requestID: UUID().uuidString)
        try store.save(intent)
        XCTAssertEqual(try store.load(), intent)
        let attributes = try FileManager.default.attributesOfItem(atPath: store.url.path)
        XCTAssertEqual(attributes[.posixPermissions] as? Int, 0o600)
        let keys = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as? [String: Any]).keys
        XCTAssertEqual(Set(keys), ["origin", "policyVersion", "requestID"])
        try store.clear()
        XCTAssertNil(try store.load())
    }
}

private actor MemoryConsentService: ProjectConsentServing {
    var granted: Bool
    var failSave: Bool
    let failRead: Bool
    let saveBeforeFailure: Bool
    var requests: [String] = []
    init(granted: Bool = false, failSave: Bool = false, failRead: Bool = false, saveBeforeFailure: Bool = false) {
        self.granted = granted; self.failSave = failSave; self.failRead = failRead; self.saveBeforeFailure = saveBeforeFailure
    }
    func fetch() throws -> ProjectConsent {
        if failRead { throw DashboardError.network }
        return ProjectConsent(granted: granted, grantedAt: granted ? "2026-10-18T12:00:00Z" : nil)
    }
    func agree(requestID: String) throws -> ProjectConsent {
        requests.append(requestID)
        if saveBeforeFailure { granted = true }
        if failSave { throw DashboardError.timeout }
        granted = true
        return try fetch()
    }
    func allowSave() { failSave = false }
}

private final class MemoryIntentStore: ConsentIntentStoring {
    var intent: ConsentIntent?
    func load() -> ConsentIntent? { intent }
    func save(_ value: ConsentIntent) { intent = value }
    func clear() { intent = nil }
}

private struct FailingIntentStore: ConsentIntentStoring {
    func load() -> ConsentIntent? { nil }
    func save(_ intent: ConsentIntent) throws { throw DashboardError.configuration }
    func clear() {}
}

private actor DelayedReadConsentService: ProjectConsentServing {
    var continuation: CheckedContinuation<ProjectConsent, Never>?
    var saves = 0
    func fetch() async -> ProjectConsent { await withCheckedContinuation { continuation = $0 } }
    func agree(requestID: String) -> ProjectConsent { saves += 1; return ProjectConsent(granted: true) }
    func waitUntilReading() async { while continuation == nil { await Task.yield() } }
    func finishRead() { continuation?.resume(returning: ProjectConsent(granted: false)); continuation = nil }
}

private actor DelayedSaveConsentService: ProjectConsentServing {
    var continuation: CheckedContinuation<ProjectConsent, Never>?
    func fetch() -> ProjectConsent { ProjectConsent(granted: false) }
    func agree(requestID: String) async -> ProjectConsent { await withCheckedContinuation { continuation = $0 } }
    func waitUntilSaving() async { while continuation == nil { await Task.yield() } }
    func finishSave() { continuation?.resume(returning: ProjectConsent(granted: true)); continuation = nil }
}
