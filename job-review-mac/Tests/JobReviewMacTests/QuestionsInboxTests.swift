import XCTest
import MacDashboardCore
import ReviewCore
@testable import JobReviewMac

@MainActor final class QuestionsInboxTests: XCTestCase {
    private func model() throws -> DashboardModel {
        let model = DashboardModel(demo: true)
        model.snapshot = try DemoSnapshot.make(scenario: "questions-inbox")
        return model
    }
    private func replace(_ model: DashboardModel, tasks: [WorkflowTask]? = nil, processes: [WorkflowProcess]? = nil, questions: [WorkflowQuestion]? = nil) throws {
        let old = try XCTUnwrap(model.snapshot)
        model.snapshot = DashboardSnapshot(feed: old.feed, health: old.health, tasks: tasks ?? old.tasks,
            processes: processes ?? old.processes, questions: questions ?? old.questions, fetchedAt: old.fetchedAt)
        model.synchronizeQuestionEditors()
    }
    func testSharedRowsCountOnceAndSuggestionsArePendingUntilAccepted() throws {
        let model = try model()
        XCTAssertEqual(model.sharedQuestionRows.count, 4)
        XCTAssertEqual(model.pendingQuestionCount, 2)
        XCTAssertEqual(model.savedQuestionCount, 1)
        let shared = try XCTUnwrap(model.sharedQuestionRows.first { $0.question.poolQuestionID == "shared-location" })
        XCTAssertEqual(shared.processes.count, 2)
        XCTAssertEqual(shared.state, .pending)
        XCTAssertNotNil(shared.question.answer)
        XCTAssertFalse(shared.question.hasAcceptedAnswer)
        XCTAssertEqual(model.questionIssueTasks.map(\.id), ["demo-task-5"])
    }
    func testSharedQuestionPrefersEditableBindingAndDoesNotUseQuestionTextAsIdentity() throws {
        let model = try model()
        var processes = model.processes
        processes[1].blockReason = "tool_approval_required"
        try replace(model, processes: processes)
        let shared = try XCTUnwrap(model.sharedQuestionRows.first { $0.question.poolQuestionID == "shared-location" })
        XCTAssertEqual(shared.process.id, "demo-process-1")
        var questions = model.questions
        var distinct = shared.question; distinct.id = "same-text-different-consent"; distinct.poolQuestionID = "different-consent"
        questions.append(distinct)
        var scope = shared.question; scope.id = "same-pool-different-scope"; scope.scope = "employer-consent"
        questions.append(scope)
        try replace(model, questions: questions)
        XCTAssertEqual(model.sharedQuestionRows.count, 6)
    }
    func testOldAndMissingFormsAreNotOfferedAsCurrentQuestions() throws {
        let model = try model()
        var questions = model.questions
        var old = questions[0]; old.id = "old-form-question"; old.formVersion = "old-form"; old.poolQuestionID = nil
        var missing = old; missing.id = "missing-process-question"; missing.processID = "missing"
        questions += [old, missing]
        try replace(model, questions: questions)
        XCTAssertEqual(model.sharedQuestionRows.count, 4)
    }
    func testSubmittedUncertainAndQuarantinedFormsCannotAcceptAnswers() throws {
        for state in ["submitted", "submission_unknown", "browser_work_uncertain"] {
            let model = try model()
            var tasks = model.tasks, processes = model.processes
            for i in processes.indices where ["demo-process-1", "demo-process-2"].contains(processes[i].id) {
                if state == "browser_work_uncertain" {
                    let index = try XCTUnwrap(tasks.firstIndex { $0.id == processes[i].taskID })
                    tasks[index].resourceQuarantined = true
                } else { processes[i].submissionStatus = state }
            }
            try replace(model, tasks: tasks, processes: processes)
            XCTAssertEqual(model.pendingQuestionCount, 0, state)
            for process in processes { XCTAssertFalse(model.canAnswer(process), state) }
        }
    }
    func testQuestionNavigationReusesEditorAndPreservesExactDraftAndReview() throws {
        let model = try model()
        let row = try XCTUnwrap(model.visibleQuestionRows.first)
        let questions = model.questions.filter { $0.processID == row.process.id && $0.formVersion == row.process.formVersion }
        let editor = model.questionEditor(process: row.process, questions: questions)
        editor.draft.setText("  Exact\nunsent draft  ", for: row.question)
        editor.review = ApplicationAnswerReview(process: row.process, questions: questions, values: editor.draft.values)
        model.showTask(row.process.taskID); model.showQuestions(); model.openQuestion(row)
        XCTAssertEqual(model.page, .questions)
        XCTAssertEqual(model.selectedTaskID, row.process.taskID)
        XCTAssertEqual(model.focusedQuestionID, row.question.id)
        XCTAssertTrue(model.questionEditor(process: row.process, questions: questions) === editor)
        XCTAssertEqual(editor.draft.values[row.question.id], .string("  Exact\nunsent draft  "))
        XCTAssertNotNil(editor.review)
    }
    func testRemoteAcceptedSharedAnswerMovesOnceToSavedAndPreservesUnrelatedDraft() throws {
        let model = try model()
        let process = try XCTUnwrap(model.processes.first { $0.id == "demo-process-2" })
        let local = model.questions.filter { $0.processID == process.id }
        let editor = model.questionEditor(process: process, questions: local)
        let location = try XCTUnwrap(local.first { $0.poolQuestionID == "shared-location" })
        let other = try XCTUnwrap(local.first { $0.type == "multi_choice" })
        editor.draft.setText("San Francisco", for: location)
        try editor.draft.toggleChoice("platform", for: other)
        var questions = model.questions
        for i in questions.indices where questions[i].poolQuestionID == "shared-location" {
            questions[i].answer = .string("San Francisco"); questions[i].answeredAt = "2026-10-25T20:00:00Z"; questions[i].revision += 1
        }
        try replace(model, questions: questions)
        XCTAssertEqual(model.pendingQuestionCount, 1); XCTAssertEqual(model.savedQuestionCount, 2)
        XCTAssertNil(editor.draft.values[location.id]); XCTAssertEqual(editor.draft.values[other.id], .array([.string("platform")]))
        model.questionsFilter = .saved
        XCTAssertEqual(model.visibleQuestionRows.filter { $0.question.poolQuestionID == "shared-location" }.count, 1)
    }
    func testSearchAndFiltersDoNotChangeAuthoritativePendingCount() throws {
        let model = try model()
        model.questionSearch = "No such company"
        XCTAssertTrue(model.visibleQuestionRows.isEmpty); XCTAssertEqual(model.pendingQuestionCount, 2)
        model.questionSearch = "Canopy"
        XCTAssertEqual(model.visibleQuestionRows.count, 1)
        model.questionsFilter = .saved; model.questionSearch = ""
        XCTAssertEqual(model.visibleQuestionRows.count, 1); XCTAssertEqual(model.pendingQuestionCount, 2)
    }
    func testAcceptedSharedBindingWinsOverEditableUnansweredBindingWithoutClaimingEveryFormSaved() throws {
        let model = DashboardModel(demo: true)
        model.snapshot = try DemoSnapshot.make(scenario: "questions-inbox-mixed")
        let row = try XCTUnwrap(model.sharedQuestionRows.first { $0.question.poolQuestionID == "shared-location" })
        XCTAssertEqual(row.state, .saved); XCTAssertTrue(row.question.hasAcceptedAnswer)
        XCTAssertEqual(row.question.id, "demo-shared-location")
        XCTAssertEqual(row.awaitingSyncCount, 1)
        XCTAssertEqual(model.pendingQuestionCount, 1); XCTAssertEqual(model.savedQuestionCount, 2)
        XCTAssertFalse(model.visibleQuestionRows.contains { $0.id == row.id })
        let unresolved = try XCTUnwrap(model.questions.first { $0.id == "demo-question" })
        XCTAssertFalse(unresolved.hasAcceptedAnswer)
        XCTAssertEqual(model.acceptedSharedQuestion(for: unresolved)?.id, row.question.id)
    }
    func testMixedSharedBindingCannotResubmitStaleDraftAndPreservesOtherDrafts() async throws {
        let model = DashboardModel(demo: true)
        model.snapshot = try DemoSnapshot.make(scenario: "questions-inbox-mixed")
        let process = try XCTUnwrap(model.processes.first { $0.id == "demo-process-2" })
        let local = model.questions.filter { $0.processID == process.id }
        let pending = try XCTUnwrap(local.first { $0.id == "demo-question" })
        let other = try XCTUnwrap(local.first { $0.type == "multi_choice" })
        let editor = model.questionEditor(process: process, questions: local)
        editor.draft.setText("My unsent choice", for: pending)
        try editor.draft.toggleChoice("platform", for: other)
        let before = model.questions
        await model.answer(process: process, questions: local, values: editor.draft.values)
        XCTAssertEqual(model.questions, before, "No local demo answer write, fan-out, or revision change")
        XCTAssertTrue(model.commandError?.contains("no duplicate answer was sent") == true)
        XCTAssertEqual(editor.draft.values[pending.id], .string("My unsent choice"))
        XCTAssertEqual(editor.draft.values[other.id], .array([.string("platform")]))
    }
    func testAcceptedForeignScopeAndLegacyTextDoNotSuppressASeparatePrompt() throws {
        let model = try model()
        var questions = model.questions
        let original = questions[0]
        var accepted = original; accepted.id = "foreign-accepted"; accepted.scope = "employer-consent"
        accepted.answeredAt = "2026-10-25T20:00:00Z"
        questions.append(accepted)
        var legacy = original; legacy.id = "legacy-unlinked"; legacy.poolQuestionID = nil
        questions.append(legacy)
        try replace(model, questions: questions)
        XCTAssertNil(model.acceptedSharedQuestion(for: original))
        XCTAssertNil(model.acceptedSharedQuestion(for: legacy))
        XCTAssertEqual(model.pendingQuestionCount, 3)
        let order = model.sharedQuestionRows.map(\.id)
        try replace(model, questions: questions.reversed())
        XCTAssertEqual(model.sharedQuestionRows.map(\.id), order)
    }
}
