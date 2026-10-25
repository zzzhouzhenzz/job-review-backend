import Foundation
import MacDashboardCore
import ReviewCore
import XCTest
@testable import JobReviewMac

@MainActor final class CrossDeviceAnswerTests: XCTestCase {
    private func fixture() throws -> (WorkflowProcess, [WorkflowQuestion]) {
        let demo = try DemoSnapshot.make(scenario: "active")
        let process = try XCTUnwrap(demo.processes.first { $0.canAnswer })
        var first = try XCTUnwrap(demo.questions.first { $0.processID == process.id })
        first.type = "text"; first.choices = []; first.answer = nil; first.answeredAt = nil
        var second = first; second.id = "fictional-second-question"; second.text = "Second question"
        return (process, [first, second])
    }
    func testRemotePartialAnswerKeepsUnrelatedDraftAndInvalidatesReview() throws {
        var (process, questions) = try fixture()
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.draft.setText("First", for: questions[0]); editor.draft.setText("Unsent second", for: questions[1])
        editor.review = ApplicationAnswerReview(process: process, questions: questions, values: editor.draft.values)
        questions[0].answer = .string("First"); questions[0].answeredAt = "2026-10-23T23:00:00Z"; questions[0].revision += 1; process.revision += 1
        editor.synchronize(process: process, questions: questions)
        XCTAssertNil(editor.draft.values[questions[0].id]); XCTAssertEqual(editor.draft.values[questions[1].id], .string("Unsent second"))
        XCTAssertNil(editor.review); XCTAssertTrue(questions[0].hasAcceptedAnswer)
        XCTAssertTrue(editor.notice?.contains("Other drafts were kept") == true)
    }
    func testConflictingRemoteAnswerShowsServerAnswerAndDoesNotKeepStaleIntent() throws {
        var (process, questions) = try fixture()
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.draft.setText("My draft", for: questions[0]); editor.draft.setText("Keep", for: questions[1])
        questions[0].answer = .string("Accepted elsewhere"); questions[0].answeredAt = "2026-10-23T23:00:00Z"; questions[0].revision += 1; process.revision += 1
        editor.synchronize(process: process, questions: questions)
        XCTAssertNil(editor.draft.values[questions[0].id]); XCTAssertEqual(editor.draft.values.count, 1)
        XCTAssertTrue(editor.notice?.contains("different answer") == true)
    }
    func testProcessRevisionAloneDoesNotEraseDraftButInvalidatesReview() throws {
        var (process, questions) = try fixture()
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.draft.setText("Keep", for: questions[0])
        editor.review = ApplicationAnswerReview(process: process, questions: questions, values: editor.draft.values)
        process.revision += 1; editor.synchronize(process: process, questions: questions)
        XCTAssertEqual(editor.draft.values[questions[0].id], .string("Keep")); XCTAssertNil(editor.review)
    }
    func testChangedQuestionDropsOnlyItsDraftAndSuggestionIsNotAccepted() throws {
        var (process, questions) = try fixture()
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.draft.setText("Old answer", for: questions[0]); editor.draft.setText("Keep", for: questions[1])
        questions[0].text = "Different question"; questions[0].revision += 1
        questions[1].answer = .string("Suggested answer")
        editor.synchronize(process: process, questions: questions)
        XCTAssertNil(editor.draft.values[questions[0].id]); XCTAssertEqual(editor.draft.values[questions[1].id], .string("Keep"))
        XCTAssertFalse(questions[1].hasAcceptedAnswer)
        process.revision += 1
    }
    func testNavigationAndDiagnosticsKeepSameEditorAndExactUnsentDraft() throws {
        let (process, questions) = try fixture(), model = DashboardModel(demo: true)
        let editor = model.questionEditor(process: process, questions: questions)
        editor.draft.setText("  Exact\nunsent text  ", for: questions[0])
        model.page = .applicationWorkflow; model.applicationWorkflow.loadDemo("workflow-publishing")
        model.page = .tasks
        XCTAssertTrue(model.questionEditor(process: process, questions: questions) === editor)
        XCTAssertEqual(editor.draft.values[questions[0].id], .string("  Exact\nunsent text  "))
    }
    func testGlobalPoolRevisionDoesNotDiscardDraftOrReviewedBinding() throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].poolQuestionID = "shared-a"; questions[0].poolRevision = 1
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.draft.setText("Keep", for: questions[0])
        editor.review = ApplicationAnswerReview(process: process, questions: questions, values: editor.draft.values)
        questions[0].poolRevision = 200
        editor.synchronize(process: process, questions: questions)
        XCTAssertEqual(editor.draft.values[questions[0].id], .string("Keep"))
        XCTAssertNotNil(editor.review); XCTAssertEqual(editor.review?.questions[0].poolRevision, 200)
    }
    func testSamePoolBindingsGroupOnceWithoutCrossingScopes() throws {
        let (_, original) = try fixture()
        var questions = original; questions[0].poolQuestionID = "shared-a"; questions[1].poolQuestionID = "shared-a"
        XCTAssertEqual(ApplicationQuestionsView.distinctQuestions(questions).count, 1)
        questions[1].scope = "employer-consent"
        XCTAssertEqual(ApplicationQuestionsView.distinctQuestions(questions).count, 2)
        questions[1].poolQuestionID = nil
        XCTAssertEqual(ApplicationQuestionsView.distinctQuestions(questions).count, 2)
    }
    func testEqualReadCannotClearUncertainPendingCommand() async throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].answer = .string("Same"); questions[0].answeredAt = "2026-10-23T23:00:00Z"
        let model = try await liveModel(process: process, questions: questions)
        model.workflow?.pending = try PendingSave(origin: "https://example.test", path: "api/v1/answers", payload: ["request_id": "unknown-outcome"], summary: "Uncertain fictional answer")
        XCTAssertFalse(model.reconcileAcceptedAnswers(process: process, questions: original, values: [original[0].id: .string("Same")]))
        XCTAssertNotNil(model.pendingSave)
    }
    func testExplicitCorrectionSurvivesPoolMetadataRefreshButNotRemoteAnswerChange() throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].answer = .string("Saved"); questions[0].answeredAt = "2026-10-23T23:00:00Z"
        let editor = ApplicationQuestionEditor(); editor.synchronize(process: process, questions: questions)
        editor.correctionIDs.insert(questions[0].id); editor.draft.setText("Reviewed correction", for: questions[0])
        questions[0].poolRevision = 200; editor.synchronize(process: process, questions: questions)
        XCTAssertEqual(editor.draft.values[questions[0].id], .string("Reviewed correction"))
        questions[0].answer = .string("Changed elsewhere"); questions[0].revision += 1
        editor.synchronize(process: process, questions: questions)
        XCTAssertNil(editor.draft.values[questions[0].id]); XCTAssertFalse(editor.correctionIDs.contains(questions[0].id))
    }
    func testMultiChoiceOrderDoesNotMakeAnIdenticalAcceptedSelectionConflict() throws {
        var question = try XCTUnwrap(DemoSnapshot.make(scenario: "questions").questions.first { $0.type == "multi_choice" })
        let ids = question.choices.map(\.id)
        question.answer = .array(ids.map(JSONValue.string)); question.answeredAt = "2026-10-23T23:00:00Z"
        XCTAssertTrue(question.matchesAccepted(.array(ids.reversed().map(JSONValue.string))))
    }
    func testDefinitiveConcurrentConflictRefreshesAndNeverReplays() async throws {
        for same in [true, false] {
            let (process, original) = try fixture()
            let initial = try await liveModel(process: process, questions: original)
            var accepted = original; accepted[0].answer = .string(same ? "My answer" : "Other answer")
            accepted[0].answeredAt = "2026-10-23T23:00:00Z"; accepted[0].revision += 1
            let result = try await liveModel(process: process, questions: accepted)
            let service = ConcurrentAnswerService(initial.workflow!, accepted: result.workflow!)
            let model = DashboardModel(workflowService: service); model.receiveWorkflow(initial.workflow!)
            await model.answer(process: process, questions: original, values: [original[0].id: .string("My answer")])
            let writes = await service.writes, reads = await service.reads
            XCTAssertEqual(writes, 1); XCTAssertEqual(reads, 1)
            XCTAssertEqual(model.questions[0].answer, accepted[0].answer)
            if same { XCTAssertNil(model.commandError); XCTAssertTrue(model.commandMessage?.contains("already accepted") == true) }
            else { XCTAssertTrue(model.commandError?.contains("Nothing was overwritten") == true) }
        }
    }
    private func liveModel(process: WorkflowProcess, questions: [WorkflowQuestion]) async throws -> DashboardModel {
        let demo = try DemoSnapshot.make(scenario: "active")
        let saved = SavedWorkflowSession(origin: "https://example.test", feed: demo.feed, processes: [process], questions: questions, tasks: demo.tasks, capabilities: demo.feed.capabilities, health: demo.health, savedAt: Date())
        let session = try WorkflowSession(api: WorkflowAPI(address: ServerAddress("https://example.test"), transport: NoCrossDeviceNetwork()), persistence: CrossDevicePersistence(saved))
        var view = await session.view(); view.online = true
        let model = DashboardModel(); model.receiveWorkflow(view)
        return model
    }
    func testIdenticalAlreadyAcceptedAnswersReconcileWithoutAnyWrite() async throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].answer = .string("Same"); questions[0].answeredAt = "2026-10-23T23:00:00Z"; questions[0].revision += 1
        let model = try await liveModel(process: process, questions: questions)
        await model.answer(process: process, questions: original, values: [original[0].id: .string("Same")])
        XCTAssertNil(model.commandError); XCTAssertTrue(model.commandMessage?.contains("already accepted") == true)
    }
    func testConflictingAlreadyAcceptedAnswerDoesNotOverwrite() async throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].answer = .string("Server"); questions[0].answeredAt = "2026-10-23T23:00:00Z"; questions[0].revision += 1
        let model = try await liveModel(process: process, questions: questions)
        await model.answer(process: process, questions: original, values: [original[0].id: .string("Draft")])
        XCTAssertTrue(model.commandError?.contains("Nothing was overwritten") == true)
        XCTAssertEqual(model.questions[0].answer, .string("Server")); XCTAssertNil(model.commandMessage)
    }
    func testStaleOrFailedQuestionReadCannotConfirmEqualAnswer() async throws {
        let (process, original) = try fixture()
        var questions = original; questions[0].answer = .string("Same"); questions[0].answeredAt = "2026-10-23T23:00:00Z"
        let model = try await liveModel(process: process, questions: questions)
        model.workflow?.readErrors["questions"] = "Unavailable"
        XCTAssertFalse(model.reconcileAcceptedAnswers(process: process, questions: original, values: [original[0].id: .string("Same")]))
        model.workflow?.readErrors = [:]; model.workflowReadAt = Date().addingTimeInterval(-60)
        XCTAssertFalse(model.reconcileAcceptedAnswers(process: process, questions: original, values: [original[0].id: .string("Same")]))
    }
}
private struct NoCrossDeviceNetwork: ReviewTransport {
    func send(_ request: URLRequest) async throws -> HTTPResult { XCTFail("No network calls permitted"); throw URLError(.cannotConnectToHost) }
}
private final class CrossDevicePersistence: WorkflowPersistence, @unchecked Sendable {
    var value: SavedWorkflowSession
    init(_ value: SavedWorkflowSession) { self.value = value }
    func load() throws -> SavedWorkflowSession? { value }
    func save(_ value: SavedWorkflowSession) throws { self.value = value }
    func clear() throws {}
}

private actor ConcurrentAnswerService: MacWorkflowServing {
    var state: WorkflowView
    let accepted: WorkflowView
    var writes = 0
    var reads = 0
    init(_ state: WorkflowView, accepted: WorkflowView) { self.state = state; self.accepted = accepted }
    func view() -> WorkflowView { state }
    func refresh() -> WorkflowView { reads += 1; state = accepted; return state }
    func answer(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue]) throws -> WorkflowView {
        writes += 1; state = accepted
        throw ReviewError.conflict("Fictional stale answer")
    }
    func decide(card: WorkflowCard, action: String, selected: Set<String>, excludeJobKeys: Set<String>, note: String) throws -> WorkflowView { XCTFail("Unexpected decision"); throw ReviewError.offline }
    func cancel(task: WorkflowTask) throws -> WorkflowView { XCTFail("Unexpected cancel"); throw ReviewError.offline }
    func retryPending() throws -> WorkflowView { XCTFail("Must not replay"); throw ReviewError.offline }
    func instructions(taskID: String) throws -> WorkflowInstructionSnapshot { throw ReviewError.offline }
    func sendInstructions(target: WorkflowInstructionTarget, text: String) throws -> WorkflowView { throw ReviewError.offline }
}
