import XCTest
import MacDashboardCore
import ReviewCore
@testable import JobReviewMac

@MainActor final class ApplicationAnswerCopyTests: XCTestCase {
    private func fixture() throws -> (DashboardModel, WorkflowProcess, [WorkflowQuestion]) {
        let snapshot = try DemoSnapshot.make(scenario: "questions-accepted-blocked")
        let model = DashboardModel(demo: true); model.snapshot = snapshot; model.now = snapshot.fetchedAt
        let process = try XCTUnwrap(snapshot.processes.first { $0.taskID == "demo-task-2" })
        return (model, process, snapshot.questions.filter { $0.processID == process.id })
    }

    func testAcceptedFormNamesExistingBlockerWithoutSuggestingRefresh() throws {
        let (model, process, questions) = try fixture()
        let message = try XCTUnwrap(model.acceptedAnswersBlockerMessage(process: process, questions: questions))
        XCTAssertEqual(message, "All answers for this form are accepted. Remaining issue: Tool Approval Required\nSee Task details for the next step.")
        XCTAssertFalse(message.localizedCaseInsensitiveContains("refresh"))
        XCTAssertFalse(model.canAnswer(process), "Copy must not enable another answer submission")
        let editor = model.questionEditor(process: process, questions: questions)
        editor.draft.setText("Keep this correction draft", for: questions[0])
        _ = model.acceptedAnswersBlockerMessage(process: process, questions: questions)
        XCTAssertEqual(editor.draft.values[questions[0].id], .string("Keep this correction draft"))
    }

    func testPendingSuggestedEmptyAndForeignFormsDoNotClaimAllAccepted() throws {
        let (model, process, accepted) = try fixture()
        var suggested = accepted; suggested[0].answeredAt = nil
        var unanswered = accepted; unanswered[0].answer = nil
        var nullAnswer = accepted; nullAnswer[0].answer = .null
        var foreign = accepted; foreign[0].formVersion = "another-form"
        var foreignProcess = accepted; foreignProcess[0].processID = "another-process"
        for questions in [[], suggested, unanswered, nullAnswer, foreign, foreignProcess, accepted + unanswered] {
            XCTAssertNil(model.acceptedAnswersBlockerMessage(process: process, questions: questions))
        }
    }

    func testQuestionBlockerKeepsExistingAnswerGateAndNoBlockerCopy() throws {
        let (model, original, questions) = try fixture()
        var process = original; process.blockReason = "questions_pending"
        let snapshot = try XCTUnwrap(model.snapshot)
        model.snapshot = DashboardSnapshot(feed: snapshot.feed, health: snapshot.health, tasks: snapshot.tasks,
            processes: [process], questions: questions, fetchedAt: snapshot.fetchedAt)
        XCTAssertNil(model.acceptedAnswersBlockerMessage(process: process, questions: questions))
        XCTAssertTrue(model.canAnswer(process), "The existing questions_pending gate must remain unchanged")
    }

    func testProcessBlockerTakesPriorityAndTaskErrorIsFallback() throws {
        let (model, original, questions) = try fixture()
        var process = original; process.blockReason = "posting_changed"
        XCTAssertTrue(try XCTUnwrap(model.acceptedAnswersBlockerMessage(process: process, questions: questions)).contains("Posting changed"))
        process.blockReason = nil
        XCTAssertTrue(try XCTUnwrap(model.acceptedAnswersBlockerMessage(process: process, questions: questions)).contains("Tool Approval Required"))
        process.blockReason = "Review Bearer \(String(repeating: "A", count: 43))"
        let message = try XCTUnwrap(model.acceptedAnswersBlockerMessage(process: process, questions: questions))
        XCTAssertTrue(message.contains("[redacted]")); XCTAssertFalse(message.contains(String(repeating: "A", count: 43)))
    }

    func testHistoricalIssueDoesNotDescribeRunningOrFinishedWorkAsBlocked() throws {
        let (model, original, questions) = try fixture()
        for status in ["running", "succeeded", "cancelled"] {
            var process = original; process.taskStatus = status
            XCTAssertNil(model.acceptedAnswersBlockerMessage(process: process, questions: questions))
        }
    }
}
