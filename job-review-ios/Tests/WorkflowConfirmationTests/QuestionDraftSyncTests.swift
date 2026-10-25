import XCTest
@testable import ReviewCore
@testable import JobReviewPreview

final class QuestionDraftSyncTests: XCTestCase {
    private func question(_ id: String, revision: Int = 1, answer: JSONValue? = nil) -> WorkflowQuestion {
        WorkflowQuestion(id: id, processID: "fictional-process", formVersion: "form-1",
            revision: revision, text: "Question \(id)", type: "text", choices: [],
            required: true, scope: "application", reason: "", answer: answer,
            answeredAt: answer == nil ? nil : "2026-10-23T21:00:00Z",
            createdAt: "2026-10-23T20:00:00Z", updatedAt: "2026-10-23T21:00:00Z")
    }

    private func workflow(syncState: String? = "ready", supported: Bool = true,
                          poolRevision: Int = 7, readError: Bool = false) -> WorkflowView {
        var capabilities = WorkflowCapabilities(apiVersion: 1, workerEnabled: true, workerStatus: "configured",
            explicitApply: true, questions: true, feedDelivery: "poll", questionScopes: ["application"], remoteAccess: true)
        capabilities.questionSync = supported
        var view = WorkflowView(feed: nil, processes: [], questions: [], tasks: [], capabilities: capabilities,
            health: nil, pending: nil, lastAcknowledgement: nil, savedAt: nil, online: true,
            readErrors: readError ? ["question_sync": "Fixture read failed"] : [:])
        view.questionSync = syncState.map { WorkflowQuestionSync(state: $0, revision: 12, poolRevision: poolRevision) }
        view.questionSyncReadAt = syncState == nil ? nil : Date()
        view.questionSyncPoolRevision = syncState == nil ? nil : poolRevision
        return view
    }

    private func process(_ id: String, taskStatus: String = "waiting_for_user") -> WorkflowProcess {
        WorkflowProcess(id: id, jobKey: "fictional:job", taskID: "task-" + id, decisionID: "decision",
            snapshot: ["company_name": .string("Fictional employer"), "title": .string("Fictional role")],
            submissionStatus: "preparing", recruitingStatus: "awaiting_response", taskStatus: taskStatus,
            revision: 1, receipt: nil, blockReason: "questions_pending", formVersion: "form-1",
            createdAt: "2026-10-25T12:00:00Z", updatedAt: "2026-10-25T12:00:00Z", events: [])
    }

    private func shared(bindingIDs: [String]) -> WorkflowSharedQuestion {
        WorkflowSharedQuestion(id: "canonical", text: "Saved personal answer", type: "text", choices: [],
            required: true, scope: WorkflowSharedQuestionScope(kind: "personal", key: ""),
            status: "answered", answer: .string("Confirmed"), answerRevision: 1,
            answeredAt: "2026-10-25T12:00:00Z", bindingIDs: bindingIDs)
    }

    func testRemoteAnswerRemovesOnlyResolvedDraft() {
        let before = [question("a"), question("b")]
        let after = [question("a", revision: 2, answer: .string("Saved on Mac")), question("b")]
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Old draft"), "b": .string("Keep me")],
                                                  previous: before, current: after)
        XCTAssertEqual(result.values, ["b": .string("Keep me")])
        XCTAssertEqual(result.remotelyAnsweredIDs, ["a"])
        XCTAssertTrue(result.invalidatedIDs.isEmpty)
    }

    func testRemoteAnswerWithoutLocalDraftStillReportsSynchronization() {
        let result = QuestionDraftSync.reconcile(values: [:], previous: [question("a")],
            current: [question("a", revision: 2, answer: .string("Saved on Mac"))])
        XCTAssertEqual(result.remotelyAnsweredIDs, ["a"])
        XCTAssertTrue(result.values.isEmpty)
    }

    func testQuestionRevisionChangeInvalidatesOnlyThatBinding() {
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Stale"), "b": .string("Keep")],
            previous: [question("a"), question("b")], current: [question("a", revision: 2), question("b")])
        XCTAssertEqual(result.values, ["b": .string("Keep")])
        XCTAssertEqual(result.invalidatedIDs, ["a"])
    }

    func testGlobalPoolRevisionDoesNotEraseUnrelatedDrafts() {
        var before = question("a"); before.poolQuestionID = "shared-a"; before.poolRevision = 4
        var after = before; after.poolRevision = 5
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Keep")], previous: [before], current: [after])
        XCTAssertEqual(result.values, ["a": .string("Keep")])
        XCTAssertTrue(result.invalidatedIDs.isEmpty)
    }

    func testUnchangedExplicitCorrectionDraftIsPreserved() {
        let saved = question("a", revision: 2, answer: .string("Original"))
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Correction")], previous: [saved], current: [saved])
        XCTAssertEqual(result.values, ["a": .string("Correction")])
    }

    func testRemoteCorrectionReplacesStaleLocalCorrection() {
        let result = QuestionDraftSync.reconcile(values: ["a": .string("My stale correction")],
            previous: [question("a", revision: 2, answer: .string("Original"))],
            current: [question("a", revision: 3, answer: .string("Remote correction"))])
        XCTAssertTrue(result.values.isEmpty)
        XCTAssertEqual(result.remotelyAnsweredIDs, ["a"])
    }

    func testRemovedQuestionCannotBeSubmittedFromAnOldDraft() {
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Old form")], previous: [question("a")], current: [])
        XCTAssertTrue(result.values.isEmpty)
        XCTAssertEqual(result.invalidatedIDs, ["a"])
    }

    func testChangedFormCannotReuseAStaleDraft() {
        let before = question("a")
        var after = before; after.formVersion = "form-2"
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Old form")], previous: [before], current: [after])
        XCTAssertTrue(result.values.isEmpty)
        XCTAssertEqual(result.invalidatedIDs, ["a"])
    }

    func testLegacyAndPoolQuestionDecodingRemainCompatible() throws {
        let original = question("a")
        let legacyData = try JSONEncoder().encode(original)
        let legacy = try JSONDecoder().decode(WorkflowQuestion.self, from: legacyData)
        XCTAssertNil(legacy.poolQuestionID)
        XCTAssertNil(legacy.poolRevision)
        var new = original; new.poolQuestionID = "shared-a"; new.poolRevision = 7
        let restored = try JSONDecoder().decode(WorkflowQuestion.self, from: JSONEncoder().encode(new))
        XCTAssertEqual(restored.poolQuestionID, "shared-a")
        XCTAssertEqual(restored.poolRevision, 7)
        XCTAssertEqual(restored.id, legacy.id)
    }

    func testSharedQuestionIsPresentedOnceUsingAnEditableApplicationBinding() {
        var first = question("a"); first.processID = "paused-process"; first.poolQuestionID = "shared"
        var second = question("b"); second.processID = "editable-process"; second.poolQuestionID = "shared"
        let result = QuestionDraftSync.displayQuestions([first, second], answerableProcessIDs: ["editable-process"])
        XCTAssertEqual(result.map(\.id), ["b"])
    }

    func testSimilarWordingNeverMergesDifferentScopesOrLegacyQuestions() {
        var first = question("a"); first.text = "I agree"; first.poolQuestionID = "agreement-a"
        var second = question("b"); second.text = first.text; second.poolQuestionID = "agreement-b"
        var legacy = question("c"); legacy.text = first.text
        XCTAssertEqual(QuestionDraftSync.displayQuestions([first, second, legacy],
            answerableProcessIDs: []).map(\.id), ["a", "b", "c"])
    }

    func testSuggestedAnswerWithoutAcceptanceDoesNotClaimRemoteSave() {
        let before = question("a")
        var suggestion = question("a", answer: .string("Suggestion")); suggestion.answeredAt = nil
        XCTAssertFalse(suggestion.hasAcceptedAnswer)
        let update = QuestionDraftSync.reconcile(values: [:], previous: [before], current: [suggestion])
        XCTAssertTrue(update.remotelyAnsweredIDs.isEmpty)
        var accepted = suggestion; accepted.answeredAt = "2026-10-23T21:00:00Z"; accepted.revision = 2
        XCTAssertTrue(accepted.hasAcceptedAnswer)
        XCTAssertFalse(suggestion.hasSameAnswerBinding(as: accepted))
    }

    func testUpdatingOrFailedSyncPreservesExactDraftEvenIfReplacementDataIsEmpty() {
        let drafts: [String: JSONValue] = ["a": .string("  Keep my exact draft\n"), "b": .string("Another draft")]
        for view in [workflow(syncState: nil), workflow(syncState: "updating"), workflow(syncState: "pending"), workflow(readError: true)] {
            XCTAssertFalse(view.questionSyncAllowsAnswers)
            let result = QuestionDraftSync.reconcile(values: drafts, previous: [question("a"), question("b")],
                current: [], snapshotReady: view.questionSyncAllowsAnswers)
            XCTAssertEqual(result.values, drafts)
            XCTAssertTrue(result.remotelyAnsweredIDs.isEmpty)
            XCTAssertTrue(result.invalidatedIDs.isEmpty)
        }
    }

    func testReadySyncAppliesRemoteAnswerAndKeepsUnrelatedDraft() {
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Local"), "b": .string("Keep")],
            previous: [question("a"), question("b")],
            current: [question("a", revision: 2, answer: .string("Saved on Mac")), question("b")],
            snapshotReady: workflow().questionSyncAllowsAnswers)
        XCTAssertEqual(result.values, ["b": .string("Keep")])
        XCTAssertEqual(result.remotelyAnsweredIDs, ["a"])
    }

    func testConfirmationRequiresCapturedPoolRevisionButNewRevisionKeepsDraft() {
        var captured = question("a"); captured.snapshotPoolRevision = 7
        XCTAssertTrue(QuestionDraftSync.confirmationHasCurrentSnapshot([captured], workflow: workflow()))
        XCTAssertFalse(QuestionDraftSync.confirmationHasCurrentSnapshot([captured], workflow: workflow(poolRevision: 8)))
        XCTAssertFalse(QuestionDraftSync.confirmationHasCurrentSnapshot([captured], workflow: workflow(syncState: "updating")))
        XCTAssertFalse(QuestionDraftSync.confirmationHasCurrentSnapshot([question("a")], workflow: workflow()))
        var refreshed = captured; refreshed.snapshotPoolRevision = 8
        let result = QuestionDraftSync.reconcile(values: ["a": .string("Keep after stale review")],
            previous: [captured], current: [refreshed], snapshotReady: true)
        XCTAssertEqual(result.values, ["a": .string("Keep after stale review")])
        XCTAssertTrue(result.invalidatedIDs.isEmpty)
    }

    func testCapableSyncRetriesQuicklyEvenWithAnEmptyQuestionCache() {
        for view in [workflow(syncState: nil), workflow(syncState: "updating"), workflow(syncState: "pending"), workflow(readError: true)] {
            XCTAssertTrue(view.questions.isEmpty)
            XCTAssertTrue(QuestionDraftSync.shouldPollQuickly(view))
        }
        XCTAssertFalse(QuestionDraftSync.shouldPollQuickly(workflow()))
        var readyWithQuestion = workflow(); readyWithQuestion.questions = [question("a")]
        XCTAssertTrue(QuestionDraftSync.shouldPollQuickly(readyWithQuestion))
    }

    func testLegacyServerKeepsItsExistingReadAndWriteRules() {
        let legacy = workflow(syncState: nil, supported: false)
        XCTAssertTrue(legacy.questionSyncAllowsAnswers)
        XCTAssertTrue(QuestionDraftSync.confirmationHasCurrentSnapshot([question("a")], workflow: legacy))
        XCTAssertFalse(QuestionDraftSync.shouldPollQuickly(legacy))
    }

    func testCanonicalEditorRouteUsesOnlyExactBindingsAndServerOrder() {
        var first = question("a"); first.poolQuestionID = "canonical"; first.processID = "paused"
        var second = question("b"); second.poolQuestionID = "canonical"; second.processID = "editable"
        var unrelated = question("c"); unrelated.poolQuestionID = "other-canonical"; unrelated.processID = "editable"
        var view = workflow(); view.questions = [first, second, unrelated]
        view.processes = [process("paused", taskStatus: "queued"), process("editable")]
        let canonical = shared(bindingIDs: ["c", "a", "b"])
        XCTAssertEqual(QuestionDraftSync.editorBinding(for: canonical, workflow: view)?.id, "b")
        view.processes[0].taskStatus = "waiting_for_user"
        XCTAssertEqual(QuestionDraftSync.editorBinding(for: canonical, workflow: view)?.id, "a")
        XCTAssertEqual(canonical.answerDisplayText, "Confirmed")
        XCTAssertTrue(canonical.hasAcceptedAnswer)
    }

    func testUnboundCanonicalSavedAnswerNeedsNoLocalFormToRemainVisible() {
        let canonical = shared(bindingIDs: [])
        var view = workflow(); view.sharedQuestions = [canonical]
        XCTAssertNil(QuestionDraftSync.editorBinding(for: canonical, workflow: view))
        XCTAssertEqual(view.sharedQuestions.count, 1)
        XCTAssertEqual(view.sharedQuestions[0].answerDisplayText, "Confirmed")
        XCTAssertTrue(view.sharedQuestions[0].hasAcceptedAnswer)
    }

    func testLocalBindingDoesNotDetermineCanonicalSavedState() {
        var local = question("a"); local.poolQuestionID = "canonical"
        var view = workflow(); view.questions = [local]; view.processes = [process(local.processID)]
        let canonical = shared(bindingIDs: ["a"])
        XCTAssertFalse(QuestionDraftSync.editorBinding(for: canonical, workflow: view)!.hasAcceptedAnswer)
        XCTAssertTrue(canonical.hasAcceptedAnswer)
        XCTAssertEqual(canonical.answerDisplayText, "Confirmed")
    }
}
