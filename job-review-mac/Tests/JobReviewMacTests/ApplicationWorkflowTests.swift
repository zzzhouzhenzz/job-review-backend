import Foundation
import MacDashboardCore
import ReviewCore
import XCTest
@testable import JobReviewMac

@MainActor final class ApplicationWorkflowTests: XCTestCase {
    func testThreeLanesKeepPausedDistinctFromAvailableAndSimultaneousActiveStages() throws {
        let value = try ApplicationWorkflowDemo.make()
        let model = ApplicationWorkflowModel(); model.receive(.observed(value))
        XCTAssertEqual(model.phase, .observed)
        XCTAssertEqual(value.workers.filter { $0.state == "running" }.count, 2)
        XCTAssertEqual(value.workers[1].state, "paused")
        XCTAssertTrue(ApplicationWorkerStep.pool.contains(value.workers[2].stage))
        XCTAssertTrue(ApplicationWorkerStep.apply.contains(value.workers[0].stage))
        let idle = try ApplicationWorkflowDemo.make(scenario: "workflow-idle")
        XCTAssertTrue(idle.workers.allSatisfy { $0.state == "available" && $0.stage == "idle" })
    }
    func testStageLockAndHandlerChangeTogetherFromOneObservation() throws {
        let model = ApplicationWorkflowModel()
        model.receive(.observed(try ApplicationWorkflowDemo.make()))
        model.selectedWorkerID = "apply-3"
        model.receive(.observed(try ApplicationWorkflowDemo.make(scenario: "workflow-publishing")))
        XCTAssertEqual(model.selectedWorker?.stage, "waiting_for_lock")
        XCTAssertEqual(model.observation?.questionLock.ownerKind, "answer_handler")
        XCTAssertEqual(model.observation?.questionLock.waitingWorkerIds, ["apply-1", "apply-3"])
        XCTAssertEqual(model.observation?.answerHandler.stage, "publishing_answers")
    }
    func testFreshRequestCannotMakeOldOrFutureServerObservationLookLive() throws {
        let model = ApplicationWorkflowModel(), now = Date()
        model.receive(.observed(try ApplicationWorkflowDemo.make(date: now.addingTimeInterval(-60))), at: now)
        XCTAssertEqual(model.phase, .stale)
        model.receive(.observed(try ApplicationWorkflowDemo.make(date: now.addingTimeInterval(60))), at: now)
        XCTAssertEqual(model.phase, .stale)
        model.receive(.observed(try ApplicationWorkflowDemo.make(date: now)), at: now)
        model.now = now.addingTimeInterval(16); XCTAssertEqual(model.phase, .stale)
    }
    func testDisconnectKeepsLastObservationButStopsLiveClaim() async throws {
        let service = WorkflowObservationService(.observed(try ApplicationWorkflowDemo.make()))
        let model = ApplicationWorkflowModel(service: service)
        await model.refresh(); let original = model.observation
        await service.fail(); await model.refresh()
        XCTAssertEqual(model.observation, original); XCTAssertEqual(model.phase, .stale)
        XCTAssertNotNil(model.error)
    }
    func testUnsupportedClearsPriorObservationAndFailedDiscoveryIsUnknown() async throws {
        let service = WorkflowObservationService(.observed(try ApplicationWorkflowDemo.make()))
        let model = ApplicationWorkflowModel(service: service)
        await model.refresh(); await service.set(.unsupported); await model.refresh()
        XCTAssertEqual(model.phase, .unsupported); XCTAssertNil(model.observation)
        await service.fail(); await model.refresh()
        XCTAssertEqual(model.phase, .disconnected)
    }
    func testFictionalMonitoringMakesNoRequestsOrAutomaticStageMotion() async throws {
        let service = WorkflowObservationService(.unsupported)
        let model = ApplicationWorkflowModel(service: service, fictional: true)
        model.loadDemo("workflow"); let original = model.observation
        await model.monitor(); await model.refresh()
        XCTAssertEqual(model.observation, original)
        let calls = await service.calls; XCTAssertEqual(calls, 0)
    }
    func testUnknownStageHighlightsNoKnownStep() throws {
        let worker = try ApplicationWorkflowDemo.make(scenario: "workflow-unknown").workers[0]
        XCTAssertFalse(worker.knownState); XCTAssertFalse(worker.knownStage)
        XCTAssertFalse(ApplicationWorkerStep.allCases.contains { $0.contains(worker.stage) })
    }
    func testBrowserWaitIsNotAnApplyingStageAndQueueRemainsOrdered() throws {
        let value = try ApplicationWorkflowDemo.make(scenario: "workflow-browser-queue")
        let waiting = value.workers[1]
        XCTAssertTrue(waiting.knownStage); XCTAssertEqual(waiting.state, "running")
        XCTAssertTrue(ApplicationWorkerStep.browser.contains(waiting.stage))
        XCTAssertFalse(ApplicationWorkerStep.apply.contains(waiting.stage))
        XCTAssertEqual(value.browserAccess?.owner?.workerId, "apply-1")
        XCTAssertEqual(value.browserAccess?.queue.map(\.workerId), ["apply-2", "apply-3"])
        let blocked = try ApplicationWorkflowDemo.make(scenario: "workflow-browser-blocked")
        XCTAssertEqual(blocked.browserAccess?.state, "blocked")
        XCTAssertNil(blocked.browserAccess?.owner)
        XCTAssertEqual(blocked.browserAccess?.blockedBy.first?.taskId, "sample-task-2")
        XCTAssertNil(try ApplicationWorkflowDemo.make().browserAccess)
    }
}
private actor WorkflowObservationService: ApplicationWorkflowServing {
    var value: ApplicationWorkflowRead
    var failing = false
    var calls = 0
    init(_ value: ApplicationWorkflowRead) { self.value = value }
    func set(_ value: ApplicationWorkflowRead) { self.value = value }
    func fail() { failing = true }
    func fetch() async throws -> ApplicationWorkflowRead {
        calls += 1
        if failing { throw DashboardError.network }
        return value
    }
}
