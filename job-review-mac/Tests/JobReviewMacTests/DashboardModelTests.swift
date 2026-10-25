import XCTest
@testable import JobReviewMac
import MacDashboardCore
import ReviewCore

final class DashboardModelTests: XCTestCase {
    func testWorkerConfigurationLabelDistinguishesPauseFromMissingConfiguration() throws {
        let paused = try DemoSnapshot.make(scenario: "workers-paused").feed.capabilities
        XCTAssertFalse(paused.workerEnabled)
        XCTAssertEqual(paused.workerConfigurationTitle, "Paused")
        let missing = try DemoSnapshot.make(scenario: "workers-not-configured").feed.capabilities
        XCTAssertFalse(missing.workerEnabled)
        XCTAssertEqual(missing.workerConfigurationTitle, "Not configured")
        let enabled = try DemoSnapshot.make(scenario: "idle").feed.capabilities
        XCTAssertTrue(enabled.workerEnabled)
        XCTAssertEqual(enabled.workerConfigurationTitle, "Enabled")
    }

    @MainActor func testFreshnessDoesNotUseBrowserReadiness() throws {
        let model = DashboardModel(demo: true)
        model.snapshot = try DemoSnapshot.make(scenario: "idle")
        model.now = model.snapshot!.fetchedAt
        XCTAssertEqual(model.phase, .connected)
        XCTAssertFalse(model.snapshot!.health.ready)
        XCTAssertEqual(model.snapshot!.health.status, "idle")
        XCTAssertTrue(model.running.isEmpty)
        model.now = model.snapshot!.fetchedAt.addingTimeInterval(46)
        XCTAssertEqual(model.phase, .stale)
    }
    @MainActor func testFailedRefreshRetainsSnapshotAsStale() throws {
        let model = DashboardModel()
        XCTAssertEqual(model.phase, .loading)
        model.errorMessage = "Unavailable"
        XCTAssertEqual(model.phase, .disconnected)
        model.snapshot = try DemoSnapshot.make()
        XCTAssertEqual(model.phase, .stale)
        XCTAssertEqual(model.menuTitle, "JR Stale")
        model.errorMessage = nil
        XCTAssertEqual(model.phase, .connected)
    }
    @MainActor func testTaskGroupingAndNavigation() throws {
        let model = DashboardModel(demo: true)
        model.snapshot = try DemoSnapshot.make()
        XCTAssertEqual(model.running.count, 2)
        XCTAssertEqual(model.queued.count, 2)
        XCTAssertEqual(model.attention.count, 2)
        XCTAssertEqual(model.unansweredQuestions.count, 1)
        model.search = "does not match"
        model.taskFilter = "Active"
        model.showTask("demo-task-2")
        XCTAssertEqual(model.page, .tasks)
        XCTAssertEqual(model.taskFilter, "All")
        XCTAssertEqual(model.search, "")
        XCTAssertEqual(model.selectedTaskID, "demo-task-2")
        model.taskFilter = "Attention"
        XCTAssertEqual(model.filteredTasks.count, 2)
        model.search = "Northstar"
        XCTAssertEqual(model.filteredTasks.count, 1)
    }
    @MainActor func testUnansweredQuestionsMatchCurrentFormAndWaitState() throws {
        let base = try DemoSnapshot.make()
        var question = base.questions[0]
        question.formVersion = "superseded-form"
        let model = DashboardModel(demo: true)
        model.snapshot = DashboardSnapshot(feed: base.feed, health: base.health, tasks: base.tasks, processes: base.processes, questions: [question], fetchedAt: Date())
        XCTAssertTrue(model.unansweredQuestions.isEmpty)
        question.formVersion = "demo-form"
        question.answer = .string("Saved answer")
        model.snapshot = DashboardSnapshot(feed: base.feed, health: base.health, tasks: base.tasks, processes: base.processes, questions: [question], fetchedAt: Date())
        XCTAssertTrue(model.unansweredQuestions.isEmpty)
    }
    func testPublicDiagnosticsRedactTokenShapes() {
        let token = String(repeating: "z", count: 43)
        let message = "Bearer \(token) failed"
        XCTAssertFalse(message.safeDiagnostic.contains(token))
        XCTAssertTrue(message.safeDiagnostic.contains("[redacted]"))
        XCTAssertEqual("questions_pending".safeDiagnostic, "questions_pending")
        XCTAssertEqual(String(repeating: "a ", count: 4000).safeDiagnostic.count, 4000)
    }
    func testTimestampParsingAndUnknownStatus() {
        XCTAssertNotNil(timestamp("2026-10-17T18:00:00.123456Z"))
        XCTAssertNotNil(timestamp("2026-10-17T18:00:00Z"))
        XCTAssertNil(timestamp("invalid"))
        XCTAssertEqual("future_state".readableStatus, "Future State")
    }
}
