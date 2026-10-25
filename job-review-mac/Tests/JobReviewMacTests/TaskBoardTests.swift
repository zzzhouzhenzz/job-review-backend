import MacDashboardCore
import ReviewCore
import XCTest
@testable import JobReviewMac

final class TaskBoardTests: XCTestCase {
    private func task(status: String, quarantined: Bool = false) throws -> WorkflowTask {
        var task = try XCTUnwrap(DemoSnapshot.make(scenario: "active").tasks.first)
        task.id = "fictional-\(status)-\(quarantined)"
        task.status = status
        task.resourceQuarantined = quarantined
        return task
    }

    func testEveryTaskAppearsInExactlyOneColumn() throws {
        let expected: [(String, TaskBoardColumn)] = [
            ("queued", .queued), ("retry_wait", .queued), ("running", .running),
            ("waiting_for_user", .attention), ("failed", .attention),
            ("succeeded", .finished), ("cancelled", .finished), ("future_state", .other)
        ]
        var tasks = [WorkflowTask]()
        for (status, column) in expected {
            let value = try task(status: status)
            XCTAssertEqual(TaskBoardColumn.column(for: value), column, status)
            tasks.append(value)
            tasks.append(try task(status: status, quarantined: true))
        }
        let columns = TaskBoardColumn.allCases.map { column in
            tasks.filter { TaskBoardColumn.column(for: $0) == column }
        }
        let displayedIDs = columns.flatMap { $0.map(\.id) }
        XCTAssertEqual(displayedIDs.count, tasks.count)
        XCTAssertEqual(Set(displayedIDs).count, tasks.count)
        XCTAssertEqual(Set(displayedIDs), Set(tasks.map(\.id)))
    }

    func testUnknownStatesRemainVisibleInOther() throws {
        for status in ["future_state", "paused", "", "Succeeded"] {
            XCTAssertEqual(TaskBoardColumn.column(for: try task(status: status)), .other)
        }
    }

    func testQuarantineTakesPriorityOverRunningAndTerminalStates() throws {
        for status in ["running", "cancelled", "succeeded", "queued", "retry_wait", "future_state"] {
            XCTAssertEqual(TaskBoardColumn.column(for: try task(status: status, quarantined: true)), .attention, status)
        }
    }

    func testRetryHistoryDoesNotTurnScheduledWorkIntoFailure() throws {
        var retry = try task(status: "retry_wait")
        retry.failureCount = 2
        retry.error = "Fictional temporary connection failure"
        XCTAssertEqual(TaskBoardColumn.column(for: retry), .queued)

        var failed = try task(status: "failed")
        failed.failureCount = 0
        failed.error = nil
        XCTAssertEqual(TaskBoardColumn.column(for: failed), .attention)

        var cancelled = try task(status: "cancelled")
        cancelled.failureCount = cancelled.maxAttempts
        cancelled.error = "Fictional earlier failure"
        XCTAssertEqual(TaskBoardColumn.column(for: cancelled), .finished)
    }

    @MainActor func testFinishedTaskDoesNotInferApplicationSubmission() throws {
        let base = try DemoSnapshot.make(scenario: "active")
        let process = try XCTUnwrap(base.processes.first)
        var tasks = base.tasks
        let index = try XCTUnwrap(tasks.firstIndex { $0.id == process.taskID })
        tasks[index].status = "succeeded"
        tasks[index].resourceQuarantined = false
        let model = DashboardModel(demo: true)
        model.snapshot = DashboardSnapshot(feed: base.feed, health: base.health, tasks: tasks,
                                           processes: base.processes, questions: base.questions, fetchedAt: base.fetchedAt)

        XCTAssertEqual(TaskBoardColumn.column(for: tasks[index]), .finished)
        XCTAssertEqual(model.process(for: tasks[index])?.submissionStatus, "preparing")
        XCTAssertNil(model.process(for: tasks[index])?.receipt)
        XCTAssertEqual(model.process(for: tasks[index]), process)
    }

    func testColumnTitlesOrderAndExistingFilterMapping() {
        XCTAssertEqual(TaskBoardColumn.allCases, [.queued, .running, .attention, .finished, .other])
        XCTAssertEqual(TaskBoardColumn.allCases.map(\.title), ["Queued", "Running", "Needs attention", "Finished", "Other"])
        XCTAssertEqual(TaskBoardColumn.allCases.map(\.filterName), ["Queued", "Active", "Attention", "Finished", "All"])
        XCTAssertEqual(Set(TaskBoardColumn.allCases.map(\.id)).count, TaskBoardColumn.allCases.count)
    }
}
