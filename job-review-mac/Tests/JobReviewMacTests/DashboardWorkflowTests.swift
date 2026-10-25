import Foundation
import MacDashboardCore
import ReviewCore
import XCTest
@testable import JobReviewMac

@MainActor final class DashboardWorkflowTests: XCTestCase {
    private let origin = "https://workflow.example.test"

    private func fixture() async throws -> WorkflowView {
        let demo = try DemoSnapshot.make(scenario: "active")
        let saved = SavedWorkflowSession(origin: origin, feed: demo.feed, processes: demo.processes,
                                         questions: demo.questions, tasks: demo.tasks,
                                         capabilities: demo.feed.capabilities, health: demo.health,
                                         savedAt: Date())
        let session = try WorkflowSession(api: WorkflowAPI(address: ServerAddress(origin),
                                                          transport: WorkflowTestNoNetwork()),
                                          persistence: WorkflowTestSavedState(saved: saved))
        var view = await session.view()
        view.online = true
        return view
    }

    private func model(_ view: WorkflowView) -> (DashboardModel, WorkflowModelService) {
        let service = WorkflowModelService(view)
        let model = DashboardModel(workflowService: service)
        model.receiveWorkflow(view)
        return (model, service)
    }

    func testApplyPassesOnlyExplicitSelectionAndOriginalCardToService() async throws {
        var view = try await fixture()
        var card = try XCTUnwrap(view.feed?.cards.first)
        var extra = try XCTUnwrap(card.jobs.first)
        extra.key = "fictional-second-job"
        extra.jobID = "FICTIONAL-SECOND"
        card.jobs.append(extra)
        view.feed?.cards[0] = card
        let (model, service) = model(view)
        let selected: Set<String> = [extra.key]

        await model.decide(card: card, action: "apply", selected: selected, note: "Chosen role only")

        let calls = await service.decisions
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.card, card)
        XCTAssertEqual(calls.first?.action, "apply")
        XCTAssertEqual(calls.first?.selected, selected)
        XCTAssertEqual(calls.first?.excluded, [])
        XCTAssertEqual(calls.first?.note, "Chosen role only")
        XCTAssertNil(model.commandError)
        XCTAssertTrue(model.commandMessage?.contains("1 job") == true)
    }

    func testEmptyOrForeignApplySelectionNeverReachesService() async throws {
        let view = try await fixture()
        let card = try XCTUnwrap(view.feed?.cards.first)
        let (model, service) = model(view)

        await model.decide(card: card, action: "apply", selected: [])
        XCTAssertNotNil(model.commandError)
        await model.decide(card: card, action: "apply", selected: ["not-on-this-card"])

        let calls = await service.decisions
        XCTAssertTrue(calls.isEmpty)
        XCTAssertNotNil(model.commandError)
    }

    func testCompanyDecisionsDoNotInferSelectedJobs() async throws {
        for action in ["find_more", "exclude"] {
            let view = try await fixture()
            let card = try XCTUnwrap(view.feed?.cards.first)
            let (model, service) = model(view)

            await model.decide(card: card, action: action, selected: [])

            let calls = await service.decisions
            XCTAssertEqual(calls.count, 1)
            XCTAssertEqual(calls.first?.action, action)
            XCTAssertEqual(calls.first?.selected, [])
            XCTAssertEqual(calls.first?.excluded, [])
        }
    }

    func testUpdatedOrRemovedCardFromAnotherDeviceRejectsCapturedDecision() async throws {
        let original = try await fixture()
        let captured = try XCTUnwrap(original.feed?.cards.first)
        for removed in [false, true] {
            var updated = original
            if removed { updated.feed?.cards.removeFirst() }
            else { updated.feed?.cards[0].revision = "new-iphone-revision" }
            let (model, service) = model(original)
            model.receiveWorkflow(updated)

            XCTAssertFalse(model.canDecide(captured))
            await model.decide(card: captured, action: "apply", selected: [captured.jobs[0].key])

            let calls = await service.decisions
            XCTAssertTrue(calls.isEmpty)
            XCTAssertNotNil(model.commandError)
        }
    }

    func testDecisionResultReplacesFeedAndBoardWithAuthoritativeState() async throws {
        let original = try await fixture()
        let card = try XCTUnwrap(original.feed?.cards.first)
        var result = original
        result.feed?.cards.removeFirst()
        result.feed?.revision = "server-after-decision"
        result.tasks[0].status = "succeeded"
        result.tasks[3].id = "server-created-application-task"
        result.savedAt = Date()
        let (model, service) = model(original)
        model.selectedCardID = card.id
        await service.setSuccessResult(result)

        await model.decide(card: card, action: "apply", selected: [card.jobs[0].key])

        XCTAssertEqual(model.reviewFeed, result.feed)
        XCTAssertEqual(model.tasks, result.tasks)
        XCTAssertEqual(model.snapshot?.tasks, result.tasks)
        XCTAssertEqual(model.processes, result.processes)
        XCTAssertEqual(model.questions, result.questions)
        XCTAssertEqual(model.selectedCardID, result.feed?.cards.first?.id)
        XCTAssertNil(model.pendingSave)
        XCTAssertNil(model.commandError)
    }

    func testInFlightDecisionBlocksSecondCommandAndRefresh() async throws {
        let view = try await fixture()
        let card = try XCTUnwrap(view.feed?.cards.first)
        let (model, service) = model(view)
        await service.holdNextDecision()
        let first = Task { await model.decide(card: card, action: "apply", selected: [card.jobs[0].key]) }
        await service.waitForDecisionStart()
        XCTAssertTrue(model.isSaving)

        await model.decide(card: card, action: "exclude", selected: [])
        await model.refresh()
        let decisions = await service.decisions
        let refreshes = await service.refreshCount
        XCTAssertEqual(decisions.count, 1)
        XCTAssertEqual(refreshes, 0)

        await service.releaseDecision()
        await first.value
        XCTAssertFalse(model.isSaving)
    }

    func testUncertainFailureKeepsPendingAndRetryUsesOriginalServiceRequest() async throws {
        let original = try await fixture()
        let card = try XCTUnwrap(original.feed?.cards.first)
        let payload = try WorkflowDecisionRequest(card: card, action: "apply", selected: [card.jobs[0].key],
                                                  requestID: "fictional-stable-request-id")
        let pending = try PendingSave(origin: origin, path: "api/v1/decisions", payload: payload,
                                      summary: "Fictional application decision")
        var failed = original
        failed.pending = pending
        failed.online = false
        var confirmed = original
        confirmed.feed?.cards.removeFirst()
        confirmed.pending = nil
        let (model, service) = model(original)
        await service.failDecision(with: failed)
        await service.setSuccessResult(confirmed)

        await model.decide(card: card, action: "apply", selected: [card.jobs[0].key])

        XCTAssertEqual(model.pendingSave, pending)
        XCTAssertEqual(model.pendingSave?.body, pending.body)
        XCTAssertFalse(model.canDecide(card))
        XCTAssertNotNil(model.commandError)
        XCTAssertNil(model.commandMessage)
        await model.decide(card: card, action: "exclude", selected: [])
        await model.retryPending()

        let decisions = await service.decisions
        let retries = await service.retryCount
        let retried = await service.retriedPending
        XCTAssertEqual(decisions.count, 1)
        XCTAssertEqual(retries, 1)
        XCTAssertEqual(retried, pending)
        XCTAssertNil(model.pendingSave)
        XCTAssertEqual(model.reviewFeed, confirmed.feed)
        XCTAssertNil(model.commandError)
        XCTAssertFalse(model.isSaving)
    }

    func testEndpointErrorsDisableOnlyDependentActions() async throws {
        let base = try await fixture()
        let card = try XCTUnwrap(base.feed?.cards.first)
        let process = try XCTUnwrap(base.processes.first { $0.canAnswer })
        let task = try XCTUnwrap(base.tasks.first { $0.canCancel && $0.kind == "apply" })
        let cases: [(String, Bool, Bool, Bool)] = [
            ("feed", false, true, true),
            ("questions", true, false, true),
            ("processes", true, false, false),
            ("tasks", true, false, false),
            ("health", true, true, true)
        ]
        for (endpoint, decision, answer, cancel) in cases {
            var view = base
            view.readErrors[endpoint] = "Fictional endpoint failure"
            let (model, _) = model(view)
            XCTAssertEqual(model.canDecide(card), decision, endpoint)
            XCTAssertEqual(model.canAnswer(process), answer, endpoint)
            XCTAssertEqual(model.canCancel(task), cancel, endpoint)
            XCTAssertNotNil(model.errorMessage)
        }
    }

    func testOfflineStaleMissingCapabilitiesAndPendingStatesDisableWrites() async throws {
        let original = try await fixture()
        let card = try XCTUnwrap(original.feed?.cards.first)
        let process = try XCTUnwrap(original.processes.first { $0.canAnswer })
        let task = try XCTUnwrap(original.tasks.first { $0.canCancel })
        for reason in ["offline", "stale", "capabilities", "pending", "refreshing"] {
            var view = original
            if reason == "offline" { view.online = false }
            if reason == "capabilities" { view.capabilities = nil }
            if reason == "pending" {
                view.pending = try PendingSave(origin: origin, path: "api/v1/decisions", payload: ["request_id": "pending-id"], summary: "Fictional pending decision")
            }
            let (model, _) = model(view)
            if reason == "stale" { model.workflowReadAt = Date().addingTimeInterval(-60) }
            if reason == "refreshing" { model.isRefreshing = true }
            XCTAssertFalse(model.canDecide(card), reason)
            XCTAssertFalse(model.canAnswer(process), reason)
            XCTAssertFalse(model.canCancel(task), reason)
        }
    }

    func testCurrentAnswersPassExactProcessQuestionsAndValues() async throws {
        let view = try await fixture()
        let process = try XCTUnwrap(view.processes.first { $0.canAnswer })
        let questions = view.questions.filter { $0.processID == process.id }
        let question = try XCTUnwrap(questions.first)
        let values: [String: JSONValue] = [question.id: .string("San Francisco")]
        let (model, service) = model(view)

        await model.answer(process: process, questions: questions, values: values)

        let answers = await service.answers
        XCTAssertEqual(answers.count, 1)
        XCTAssertEqual(answers.first?.process, process)
        XCTAssertEqual(answers.first?.questions, questions)
        XCTAssertEqual(answers.first?.values, values)
        XCTAssertNil(model.commandError)
    }

    func testChangedFormProcessOrQuestionRejectsCapturedAnswers() async throws {
        let original = try await fixture()
        let captured = try XCTUnwrap(original.processes.first { $0.canAnswer })
        let questions = original.questions.filter { $0.processID == captured.id }
        let question = try XCTUnwrap(questions.first)
        let index = try XCTUnwrap(original.processes.firstIndex { $0.id == captured.id })
        for changed in ["form", "process", "question"] {
            var updated = original
            if changed == "form" { updated.processes[index].formVersion = "new-iphone-form" }
            if changed == "process" { updated.processes[index].revision += 1 }
            if changed == "question" { updated.questions[0].revision += 1 }
            let (model, service) = model(original)
            model.receiveWorkflow(updated)

            await model.answer(process: captured, questions: questions, values: [question.id: .string("San Francisco")])

            let answers = await service.answers
            XCTAssertTrue(answers.isEmpty, changed)
            XCTAssertNotNil(model.commandError, changed)
        }
    }

    func testCancelUsesCurrentQueuedTaskAndRejectsCompletedOrUncertainSubmissions() async throws {
        let original = try await fixture()
        let task = try XCTUnwrap(original.tasks.first { $0.canCancel && $0.kind == "apply" })
        let processIndex = try XCTUnwrap(original.processes.firstIndex { $0.taskID == task.id })
        let (allowed, allowedService) = model(original)
        await allowed.cancel(task)
        let cancellations = await allowedService.cancellations
        XCTAssertEqual(cancellations, [task])
        XCTAssertNil(allowed.commandError)

        for status in ["submitted", "uncertain", "future-submission-status"] {
            var updated = original
            updated.processes[processIndex].submissionStatus = status
            let (model, service) = model(updated)
            XCTAssertFalse(model.canCancel(task), status)
            await model.cancel(task)
            let cancellations = await service.cancellations
            XCTAssertTrue(cancellations.isEmpty, status)
            XCTAssertNotNil(model.commandError, status)
        }
    }

    func testTaskChangedSinceReviewCannotBeCancelled() async throws {
        let original = try await fixture()
        let captured = try XCTUnwrap(original.tasks.first { $0.canCancel })
        let index = try XCTUnwrap(original.tasks.firstIndex { $0.id == captured.id })
        var updated = original
        updated.tasks[index].status = "running"
        let (model, service) = model(original)
        model.receiveWorkflow(updated)

        XCTAssertFalse(model.canCancel(captured))
        await model.cancel(captured)

        let cancellations = await service.cancellations
        XCTAssertTrue(cancellations.isEmpty)
        XCTAssertNotNil(model.commandError)
    }

    func testRefreshUsesInjectedServiceAndUpdatesCurrentSelection() async throws {
        let original = try await fixture()
        var next = original
        next.feed?.cards.removeFirst()
        next.tasks[0].status = "succeeded"
        let (model, service) = model(original)
        model.selectedCardID = original.feed?.cards.first?.id
        await service.setSuccessResult(next)

        await model.refresh()

        let refreshes = await service.refreshCount
        XCTAssertEqual(refreshes, 1)
        XCTAssertEqual(model.reviewFeed, next.feed)
        XCTAssertEqual(model.tasks, next.tasks)
        XCTAssertEqual(model.selectedCardID, next.feed?.cards.first?.id)
        XCTAssertNil(model.settings, "An injected service must not read the user's connection configuration")
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.isRefreshing)
    }

    func testPendingRequestRejectsOriginChangeWithoutDiscardingCurrentState() async throws {
        var view = try await fixture()
        let pending = try PendingSave(origin: origin, path: "api/v1/decisions",
                                      payload: ["request_id": "fictional-pending-origin-change"],
                                      summary: "Fictional pending decision")
        view.pending = pending
        let (model, _) = model(view)
        let current = try ConnectionSettings(origin: origin, credentialFile: "/fictional/current-credential")
        model.settings = current
        let other = try ConnectionSettings(origin: "https://different-workspace.example.test",
                                            credentialFile: "/fictional/other-credential")

        XCTAssertThrowsError(try model.validateConnectionChange(to: other)) { error in
            XCTAssertTrue(error.localizedDescription.contains("pending request"))
        }

        XCTAssertEqual(model.settings, current)
        XCTAssertEqual(model.pendingSave, pending)
        XCTAssertEqual(model.pendingSave?.body, pending.body)
        XCTAssertEqual(model.reviewFeed, view.feed)
        XCTAssertEqual(model.tasks, view.tasks)
    }

    func testPendingRequestAllowsCredentialPathRotationOnSameOrigin() async throws {
        var view = try await fixture()
        let pending = try PendingSave(origin: origin, path: "api/v1/decisions",
                                      payload: ["request_id": "fictional-pending-rotation"],
                                      summary: "Fictional pending decision")
        view.pending = pending
        let (model, _) = model(view)
        model.settings = try ConnectionSettings(origin: origin, credentialFile: "/fictional/original-credential")
        let rotated = try ConnectionSettings(origin: origin + ":443/", credentialFile: "/fictional/rotated-credential")

        XCTAssertNoThrow(try model.validateConnectionChange(to: rotated))

        XCTAssertEqual(rotated.origin, origin)
        XCTAssertNotEqual(rotated.credentialFile, model.settings?.credentialFile)
        XCTAssertEqual(model.pendingSave, pending)
        XCTAssertEqual(model.workflow, view)
    }

    func testOriginChangeIsAllowedWhenNoRequestIsPending() async throws {
        let view = try await fixture()
        let (model, _) = model(view)
        model.settings = try ConnectionSettings(origin: origin, credentialFile: "/fictional/current-credential")
        let other = try ConnectionSettings(origin: "https://different-workspace.example.test",
                                            credentialFile: "/fictional/other-credential")

        XCTAssertNil(model.pendingSave)
        XCTAssertNoThrow(try model.validateConnectionChange(to: other))
    }

    func testMissingHealthKeepsWorkspaceVisibleAndUnrelatedCommandsAvailable() async throws {
        var view = try await fixture()
        view.health = nil
        let card = try XCTUnwrap(view.feed?.cards.first)
        let process = try XCTUnwrap(view.processes.first { $0.canAnswer })
        let task = try XCTUnwrap(view.tasks.first { $0.canCancel && $0.kind == "apply" })
        let (model, _) = model(view)

        XCTAssertTrue(model.hasWorkspaceData)
        XCTAssertNil(model.snapshot, "The workflow itself must keep the workspace visible when no legacy snapshot can be made")
        XCTAssertEqual(model.tasks, view.tasks)
        XCTAssertEqual(model.processes, view.processes)
        XCTAssertEqual(model.questions, view.questions)
        XCTAssertEqual(model.reviewFeed, view.feed)
        XCTAssertEqual(model.phase, .connected)
        XCTAssertTrue(model.canDecide(card))
        XCTAssertTrue(model.canAnswer(process))
        XCTAssertTrue(model.canCancel(task))

        view.readErrors["health"] = "Fictional health endpoint failure"
        model.receiveWorkflow(view)

        XCTAssertTrue(model.hasWorkspaceData)
        XCTAssertEqual(model.phase, .stale)
        XCTAssertTrue(model.canDecide(card), "Health failure must not block a current feed decision")
        XCTAssertTrue(model.canAnswer(process), "Health failure must not block current application questions")
        XCTAssertTrue(model.canCancel(task))
        XCTAssertTrue(model.errorMessage?.contains("health") == true)
    }

    func testUnavailableFeedDoesNotHideOtherWorkspaceStateOrUseOldCards() async throws {
        let original = try await fixture()
        let oldCard = try XCTUnwrap(original.feed?.cards.first)
        let process = try XCTUnwrap(original.processes.first { $0.canAnswer })
        let (model, _) = model(original)
        XCTAssertNotNil(model.snapshot?.feed)
        var unavailable = original
        unavailable.feed = nil
        unavailable.health = nil
        unavailable.readErrors["feed"] = "Fictional feed endpoint failure"

        model.receiveWorkflow(unavailable)

        XCTAssertTrue(model.hasWorkspaceData)
        XCTAssertNil(model.reviewFeed, "Old snapshot cards must not masquerade as the current feed")
        XCTAssertNil(model.selectedCard)
        XCTAssertEqual(model.tasks, unavailable.tasks)
        XCTAssertEqual(model.processes, unavailable.processes)
        XCTAssertEqual(model.questions, unavailable.questions)
        XCTAssertEqual(model.phase, .stale)
        XCTAssertFalse(model.canDecide(oldCard))
        XCTAssertTrue(model.canAnswer(process))
    }
}

private struct WorkflowTestSavedState: WorkflowPersistence {
    let saved: SavedWorkflowSession
    func load() throws -> SavedWorkflowSession? { saved }
    func save(_ value: SavedWorkflowSession) throws { throw WorkflowModelTestError.unexpectedPersistence }
}

private struct WorkflowTestNoNetwork: ReviewTransport {
    func send(_ request: URLRequest) async throws -> HTTPResult { throw WorkflowModelTestError.unexpectedNetwork }
}

private enum WorkflowModelTestError: Error, LocalizedError {
    case uncertain, unexpectedNetwork, unexpectedPersistence
    var errorDescription: String? { "Fictional workflow error: \(self)" }
}

private actor WorkflowModelService: MacWorkflowServing {
    struct Decision: Equatable, Sendable {
        let card: WorkflowCard
        let action: String
        let selected: Set<String>
        let excluded: Set<String>
        let note: String
    }
    struct Answer: Equatable, Sendable {
        let process: WorkflowProcess
        let questions: [WorkflowQuestion]
        let values: [String: JSONValue]
    }
    private var state: WorkflowView
    private var success: WorkflowView
    private var decisionFailure: WorkflowView?
    private var hold = false
    private var decisionStarted = false
    private var decisionGate: CheckedContinuation<Void, Never>?
    private var startWaiter: CheckedContinuation<Void, Never>?
    private(set) var decisions: [Decision] = []
    private(set) var answers: [Answer] = []
    private(set) var cancellations: [WorkflowTask] = []
    private(set) var refreshCount = 0
    private(set) var retryCount = 0
    private(set) var retriedPending: PendingSave?

    init(_ state: WorkflowView) { self.state = state; success = state }
    func setSuccessResult(_ view: WorkflowView) { success = view }
    func failDecision(with view: WorkflowView) { decisionFailure = view }
    func holdNextDecision() { hold = true }
    func waitForDecisionStart() async {
        if decisionStarted { return }
        await withCheckedContinuation { startWaiter = $0 }
    }
    func releaseDecision() { decisionGate?.resume(); decisionGate = nil; hold = false }
    func view() -> WorkflowView { state }
    func refresh() -> WorkflowView { refreshCount += 1; state = success; return state }
    func decide(card: WorkflowCard, action: String, selected: Set<String>, excludeJobKeys: Set<String>, note: String) async throws -> WorkflowView {
        decisions.append(Decision(card: card, action: action, selected: selected, excluded: excludeJobKeys, note: note))
        decisionStarted = true
        startWaiter?.resume(); startWaiter = nil
        if hold { await withCheckedContinuation { decisionGate = $0 } }
        if let decisionFailure { state = decisionFailure; throw WorkflowModelTestError.uncertain }
        state = success
        return state
    }
    func answer(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue]) -> WorkflowView {
        answers.append(Answer(process: process, questions: questions, values: values)); state = success; return state
    }
    func cancel(task: WorkflowTask) -> WorkflowView { cancellations.append(task); state = success; return state }
    func retryPending() -> WorkflowView { retryCount += 1; retriedPending = state.pending; state = success; return state }
    func instructions(taskID: String) throws -> WorkflowInstructionSnapshot { throw WorkflowModelTestError.unexpectedNetwork }
    func sendInstructions(target: WorkflowInstructionTarget, text: String) throws -> WorkflowView { throw WorkflowModelTestError.unexpectedNetwork }
}
