import XCTest
import ReviewCore
@testable import JobReviewMac

@MainActor final class CanonicalQuestionsTests: XCTestCase {
    private func model(_ state: String = "ready") async throws -> DashboardModel {
        let model = DashboardModel(demo: true)
        try await model.loadQuestionSyncDemo("canonical-questions-\(state)")
        return model
    }
    private func editable(_ model: DashboardModel) throws -> (WorkflowProcess, [WorkflowQuestion], WorkflowQuestion) {
        let q = try XCTUnwrap(model.questions.first { question in
            question.type == "text" && !question.hasAcceptedAnswer &&
                model.processes.first(where: { $0.id == question.processID }).map(model.canAnswer) == true
        })
        let process = try XCTUnwrap(model.processes.first { $0.id == q.processID })
        return (process, model.questions.filter { $0.processID == process.id }, q)
    }
    func testCanonicalSavedIncludesUnboundAnswersAndNoLegacyArbitration() async throws {
        let model = try await model()
        XCTAssertTrue(model.sharedQuestionRows.isEmpty)
        XCTAssertEqual(model.savedQuestionCount, 2)
        let unbound = try XCTUnwrap(model.canonicalQuestions.first { $0.id == "saved-unbound" })
        XCTAssertTrue(unbound.hasAcceptedAnswer); XCTAssertTrue(model.canonicalBindings(unbound).isEmpty)
        XCTAssertEqual(unbound.answerDisplayText, "Alex")
        for q in model.questions { XCTAssertNil(model.acceptedSharedQuestion(for: q)) }
    }
    func testInitialSyncDoesNotClaimKnownZeroCounts() async throws {
        let model = try await model("loading")
        XCTAssertFalse(model.hasCanonicalQuestionSnapshot)
        XCTAssertEqual(model.workflow?.questionSyncMessage, "Waiting for sync…")
        XCTAssertFalse(model.workflow?.questionSyncAllowsAnswers ?? true)
    }
    func testUpdatingPendingAndFailureKeepDraftAndBusinessState() async throws {
        let model = try await model()
        let (process, form, q) = try editable(model)
        let editor = model.questionEditor(process: process, questions: form)
        editor.draft.setText("  My exact\nunsent draft  ", for: q)
        let tasks = model.tasks; let count = model.savedQuestionCount
        for state in ["updating", "pending", "failed"] {
            var view = try XCTUnwrap(model.workflow)
            view.questionSync?.state = state == "failed" ? "ready" : state
            if state == "failed" { view.readErrors["question_sync"] = "fixture failed" }
            model.receiveWorkflow(view)
            XCTAssertFalse(model.canAnswer(process))
            XCTAssertEqual(editor.draft.values[q.id], .string("  My exact\nunsent draft  "))
            XCTAssertEqual(model.tasks, tasks); XCTAssertEqual(model.savedQuestionCount, count)
        }
    }
    func testNewPoolRevisionInvalidatesConfirmationAndKeepsDraft() async throws {
        let model = try await model(); let (process, form, q) = try editable(model)
        let editor = model.questionEditor(process: process, questions: form)
        editor.draft.setText("Keep this draft", for: q)
        editor.review = ApplicationAnswerReview(process: process, questions: form, values: editor.draft.values)
        var next = try XCTUnwrap(model.workflow)
        next.questionSync?.poolRevision = 5; next.questionSync?.revision = 17; next.questionSyncPoolRevision = 5
        for i in next.questions.indices { next.questions[i].snapshotPoolRevision = 5; next.questions[i].poolRevision = 5 }
        model.receiveWorkflow(next)
        XCTAssertNil(editor.review)
        XCTAssertEqual(editor.draft.values[q.id], .string("Keep this draft"))
    }
    func testDifferentServerAnswerRetainsExactConflictingDraft() async throws {
        let model = try await model(); let (process, form, q) = try editable(model)
        let editor = model.questionEditor(process: process, questions: form)
        editor.draft.setText("  Preserve exactly  ", for: q)
        var next = try XCTUnwrap(model.workflow); let index = try XCTUnwrap(next.questions.firstIndex { $0.id == q.id })
        next.questions[index].answer = .string("Remote answer"); next.questions[index].answeredAt = "2026-10-25T12:00:00Z"
        next.questions[index].revision += 1
        model.receiveWorkflow(next)
        XCTAssertEqual(editor.retainedDrafts[q.id]?.value, .string("  Preserve exactly  "))
        XCTAssertNil(editor.draft.values[q.id]); XCTAssertNil(editor.review)
    }
    func testChangedQuestionRetainsDraftWithoutBindingItToNewField() async throws {
        let model = try await model(); let (process, form, q) = try editable(model)
        let editor = model.questionEditor(process: process, questions: form)
        editor.draft.setText("Original field draft", for: q)
        var next = try XCTUnwrap(model.workflow); let index = try XCTUnwrap(next.questions.firstIndex { $0.id == q.id })
        next.questions[index].text = "A different field"; next.questions[index].revision += 1
        model.receiveWorkflow(next)
        XCTAssertEqual(editor.retainedDrafts[q.id]?.question.text, q.text)
        XCTAssertEqual(editor.retainedDrafts[q.id]?.value, .string("Original field draft"))
        XCTAssertNil(editor.draft.values[q.id])
    }
    func testCanonicalBindingNavigationUsesExplicitServerBinding() async throws {
        let model = try await model(); let (_, _, q) = try editable(model)
        model.openCanonicalBinding(q)
        XCTAssertEqual(model.focusedQuestionID, q.id)
        XCTAssertEqual(model.selectedTaskID, model.processes.first { $0.id == q.processID }?.taskID)
        XCTAssertEqual(model.page, .questions)
    }
}
