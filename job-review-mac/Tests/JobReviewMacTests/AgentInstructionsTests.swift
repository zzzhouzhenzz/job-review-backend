import Foundation
import MacDashboardCore
import ReviewCore
import XCTest
@testable import JobReviewMac

@MainActor final class AgentInstructionsTests: XCTestCase {
    private let origin = "https://instructions.example.test"

    private func fixture() async throws -> (DashboardModel, InstructionTestService, AgentInstructionSelection) {
        let demo = try DemoSnapshot.make(scenario: "active")
        var capabilities = demo.feed.capabilities
        capabilities.unblockInstructions = true
        let saved = SavedWorkflowSession(origin: origin, feed: demo.feed, processes: demo.processes,
            questions: demo.questions, tasks: demo.tasks, capabilities: capabilities, health: demo.health, savedAt: Date())
        let session = try WorkflowSession(api: WorkflowAPI(address: ServerAddress(origin), transport: InstructionNoNetwork()),
                                          persistence: InstructionTestPersistence(saved: saved))
        var view = await session.view(); view.online = true
        let snapshot = try DemoSnapshot.instructions(taskID: "demo-task-2", mode: "questions_pending")
        let service = InstructionTestService(state: view, snapshot: snapshot)
        let model = DashboardModel(workflowService: service)
        model.settings = try ConnectionSettings(origin: origin, credentialFile: "/fictional/credential")
        model.receiveWorkflow(view)
        model.openInstructions(try XCTUnwrap(model.tasks.first { $0.id == "demo-task-2" }))
        return (model, service, try XCTUnwrap(model.instructionSelection))
    }

    func testDraftsStayWithTheirTaskAndOriginAndResetOnConnectionChange() {
        let editor = AgentInstructionsModel()
        let first = AgentInstructionSelection(origin: origin, taskID: "one", title: "One", subtitle: "")
        let second = AgentInstructionSelection(origin: origin, taskID: "two", title: "Two", subtitle: "")
        let foreign = AgentInstructionSelection(origin: "https://other.example.test", taskID: "one", title: "One", subtitle: "")
        editor.select(first); editor.text = "First task guidance"
        editor.select(second); XCTAssertEqual(editor.text, ""); editor.text = "Second task guidance"
        editor.select(foreign); XCTAssertEqual(editor.text, "")
        editor.select(first); XCTAssertEqual(editor.text, "First task guidance")
        editor.reset(); editor.select(first); XCTAssertEqual(editor.text, "")
    }

    func testDraftValidationMatchesBackendUnicodeScalarLimit() {
        let editor = AgentInstructionsModel()
        editor.select(AgentInstructionSelection(origin: origin, taskID: "one", title: "One", subtitle: ""))
        editor.text = " \n\t"; XCTAssertFalse(editor.validText)
        editor.text = String(repeating: "e\u{301}", count: 2000)
        XCTAssertEqual(editor.count, 4000); XCTAssertTrue(editor.validText)
        editor.text += "a"; XCTAssertFalse(editor.validText)
    }

    func testConfirmedReceiptClearsOnlyExactSentDraftForMatchingTask() throws {
        let editor = AgentInstructionsModel()
        let selection = AgentInstructionSelection(origin: origin, taskID: "demo-task-2", title: "One", subtitle: "")
        editor.select(selection); editor.text = "Check the existing tab"
        let snapshot = try DemoSnapshot.instructions(taskID: selection.taskID)
        let (_, receipt) = try DemoSnapshot.savedInstructions(snapshot: snapshot, text: editor.text)
        var wrong = receipt; wrong.taskIDs = ["foreign-task"]
        XCTAssertFalse(editor.confirm(wrong, sentText: editor.text, for: selection))
        XCTAssertEqual(editor.text, "Check the existing tab")
        editor.text = "Newer draft"
        XCTAssertTrue(editor.confirm(receipt, sentText: "Check the existing tab", for: selection))
        XCTAssertEqual(editor.text, "Newer draft")
        XCTAssertTrue(editor.confirm(receipt, sentText: "Newer draft", for: selection))
        XCTAssertEqual(editor.text, "")
    }

    func testCapabilityAbsentPreventsInstructionReadAndSend() async throws {
        let (model, service, selection) = try await fixture()
        model.workflow?.capabilities?.unblockInstructions = nil
        model.agentInstructions.text = "Check the tab"
        await model.loadInstructions(selection)
        await model.sendInstructions(selection)
        let reads = await service.reads, sends = await service.sends
        XCTAssertEqual(reads, 0); XCTAssertTrue(sends.isEmpty)
        XCTAssertFalse(model.canSendInstructions(selection))
        XCTAssertEqual(model.agentInstructions.text, "Check the tab")
    }

    func testLoadKeepsHistoryOnFailureButDisablesNewSend() async throws {
        let (model, service, selection) = try await fixture()
        await model.loadInstructions(selection)
        let original = model.agentInstructions.snapshot
        model.agentInstructions.text = "Please check"
        XCTAssertTrue(model.canSendInstructions(selection))
        await service.failRead()
        await model.loadInstructions(selection)
        XCTAssertEqual(model.agentInstructions.snapshot, original)
        XCTAssertTrue(model.agentInstructions.needsRefresh)
        XCTAssertFalse(model.canSendInstructions(selection))
        XCTAssertNotNil(model.agentInstructions.error)
        XCTAssertFalse(model.isRefreshing)
    }

    func testFreshInstructionTargetDoesNotDependOnUnrelatedTaskListReads() async throws {
        let (model, _, selection) = try await fixture()
        model.workflow?.readErrors["tasks"] = "Fictional list failure"
        model.workflow?.readErrors["processes"] = "Fictional list failure"
        await model.loadInstructions(selection)
        model.agentInstructions.text = "Check the existing tab"
        XCTAssertTrue(model.canSendInstructions(selection), "The instruction endpoint supplies its own authoritative target")
    }

    func testSendPassesReviewedTargetAndExactTextAndUsesReceiptEffect() async throws {
        let (model, service, selection) = try await fixture()
        await model.loadInstructions(selection)
        let target = try XCTUnwrap(model.agentInstructions.snapshot?.target)
        let questions = model.questions
        model.agentInstructions.text = "  Check the existing tab.\nKeep the form open.  "
        let sent = model.agentInstructions.text
        await model.sendInstructions(selection)
        let calls = await service.sends
        XCTAssertEqual(calls.count, 1); XCTAssertEqual(calls.first?.target, target); XCTAssertEqual(calls.first?.text, sent)
        XCTAssertEqual(model.agentInstructions.text, "")
        XCTAssertEqual(model.questions, questions, "Operational instructions must not become application answers")
        XCTAssertEqual(model.agentInstructions.snapshot?.instructions.last?.text, sent)
        XCTAssertTrue(model.agentInstructions.message?.contains("Required application answers") == true)
        XCTAssertFalse(model.isSaving)
    }

    func testConflictKeepsDraftAndRequiresExplicitRefreshBeforeAnotherSend() async throws {
        let (model, service, selection) = try await fixture()
        await model.loadInstructions(selection)
        model.agentInstructions.text = "Check again"
        await service.failSend()
        await model.sendInstructions(selection)
        XCTAssertEqual(model.agentInstructions.text, "Check again")
        XCTAssertTrue(model.agentInstructions.needsRefresh)
        XCTAssertNotNil(model.agentInstructions.error)
        XCTAssertNotNil(model.agentInstructions.snapshot, "Keep readable history after a failed save")
        await model.sendInstructions(selection)
        let sends = await service.sends
        XCTAssertEqual(sends.count, 1)
        await model.loadInstructions(selection)
        XCTAssertTrue(model.canSendInstructions(selection))
    }

    func testUnavailableUnknownAndForeignTargetsCannotBeSent() async throws {
        for reason in ["busy", "unknown-mode", "wrong-task", "wrong-origin", "offline", "pending"] {
            let (model, service, selection) = try await fixture()
            await model.loadInstructions(selection)
            model.agentInstructions.text = "Please check"
            switch reason {
            case "busy": model.agentInstructions.snapshot?.target.canSubmit = false
            case "unknown-mode": model.agentInstructions.snapshot?.target.mode = "future-mode"
            case "wrong-task": model.agentInstructions.snapshot?.target.taskID = "foreign"
            case "wrong-origin": model.settings = try ConnectionSettings(origin: "https://other.example.test", credentialFile: "/fictional/other")
            case "offline": model.workflow?.online = false
            default: model.workflow?.pending = try PendingSave(origin: origin, path: "api/v1/decisions", payload: ["request_id": "fictional-id"], summary: "Fictional pending decision")
            }
            XCTAssertFalse(model.canSendInstructions(selection), reason)
            await model.sendInstructions(selection)
            let sends = await service.sends
            XCTAssertTrue(sends.isEmpty, reason)
        }
    }

    func testPendingInstructionRecoveryClearsMatchingDraftOnlyAfterReceipt() async throws {
        let (model, service, selection) = try await fixture()
        await model.loadInstructions(selection)
        let snapshot = try XCTUnwrap(model.agentInstructions.snapshot)
        model.agentInstructions.text = "Check the existing tab"
        let request = try WorkflowInstructionRequest(target: snapshot.target, text: model.agentInstructions.text)
        let pending = try PendingSave(origin: origin, path: "api/v1/unblock-instructions", payload: request, summary: "Fictional guidance")
        model.workflow?.pending = pending
        try await service.prepareRetry(pending, request: request)
        await model.retryPending()
        let replayed = await service.replayed
        XCTAssertEqual(replayed, pending)
        XCTAssertNil(model.pendingSave)
        XCTAssertEqual(model.agentInstructions.text, "")
        XCTAssertTrue(model.commandMessage?.contains("Required application answers") == true)
    }

    func testConfirmationNeverClaimsSavedOnlyResumesWork() {
        for mode in ["questions_pending", "reconciliation_only", "manual_review"] {
            let text = AgentInstructionsModel.confirmation(mode: mode, wakeStatus: "saved_only")
            XCTAssertFalse(text.contains("resumed")); XCTAssertFalse(text.contains("recheck is scheduled"))
        }
        XCTAssertTrue(AgentInstructionsModel.confirmation(mode: "browser_recheck", wakeStatus: "recheck_scheduled").contains("has not yet resumed"))
        XCTAssertTrue(AgentInstructionsModel.confirmation(mode: "preparation_retry", wakeStatus: "retry_scheduled").contains("not yet confirmed clear"))
    }
}

private struct InstructionTestPersistence: WorkflowPersistence {
    let saved: SavedWorkflowSession
    func load() -> SavedWorkflowSession? { saved }
    func save(_ value: SavedWorkflowSession) throws { throw ReviewError.invalid("Unexpected test persistence") }
}
private struct InstructionNoNetwork: ReviewTransport {
    func send(_ request: URLRequest) throws -> HTTPResult { throw ReviewError.invalid("No network allowed") }
}
private actor InstructionTestService: MacWorkflowServing {
    struct Send: Equatable { let target: WorkflowInstructionTarget; let text: String }
    var state: WorkflowView
    var snapshot: WorkflowInstructionSnapshot
    var readFails = false
    var sendFails = false
    var retryReceipt: WorkflowAcknowledgement?
    private(set) var reads = 0
    private(set) var sends: [Send] = []
    private(set) var replayed: PendingSave?
    init(state: WorkflowView, snapshot: WorkflowInstructionSnapshot) { self.state = state; self.snapshot = snapshot }
    func failRead() { readFails = true }
    func failSend() { sendFails = true }
    func view() -> WorkflowView { state }
    func refresh() -> WorkflowView { state }
    func instructions(taskID: String) throws -> WorkflowInstructionSnapshot {
        reads += 1
        if readFails { throw ReviewError.offline }
        return snapshot
    }
    func sendInstructions(target: WorkflowInstructionTarget, text: String) throws -> WorkflowView {
        sends.append(Send(target: target, text: text))
        if sendFails { throw ReviewError.conflict("The task changed. Refresh and review the current blocker.") }
        let result = try DemoSnapshot.savedInstructions(snapshot: snapshot, text: text)
        snapshot = result.0; state.lastAcknowledgement = result.1
        return state
    }
    func prepareRetry(_ pending: PendingSave, request: WorkflowInstructionRequest) throws {
        state.pending = pending
        let result = try DemoSnapshot.savedInstructions(snapshot: snapshot, text: request.text)
        snapshot = result.0; retryReceipt = result.1; retryReceipt?.requestID = request.requestID
    }
    func retryPending() -> WorkflowView {
        replayed = state.pending; state.pending = nil; state.lastAcknowledgement = retryReceipt
        return state
    }
    func decide(card: WorkflowCard, action: String, selected: Set<String>, excludeJobKeys: Set<String>, note: String) throws -> WorkflowView { throw ReviewError.invalid("Unexpected command") }
    func answer(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue]) throws -> WorkflowView { throw ReviewError.invalid("Unexpected answer") }
    func cancel(task: WorkflowTask) throws -> WorkflowView { throw ReviewError.invalid("Unexpected cancellation") }
}
