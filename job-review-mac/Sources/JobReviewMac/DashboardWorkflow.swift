import Foundation
import MacDashboardCore
import ReviewCore

extension DashboardModel {
    var reviewFeed: WorkflowFeed? { demo ? snapshot?.feed : workflow?.feed }
    var pendingSave: PendingSave? { workflow?.pending }
    var selectedCard: WorkflowCard? { reviewFeed?.cards.first { $0.id == selectedCardID } }
    var hasWorkspaceData: Bool { workflow != nil || snapshot != nil }

    func validateConnectionChange(to next: ConnectionSettings) throws {
        if let settings, settings.origin != next.origin, pendingSave != nil {
            throw ReviewError.invalid("Confirm the pending request in the current workspace before changing the server address.")
        }
    }

    func canWrite(_ endpoints: [String]) -> Bool {
        guard !isSaving, !isRefreshing, !localServer.isBusy, pendingSave == nil else { return false }
        if demo { return phase == .connected }
        guard let workflow, workflow.online, workflow.pending == nil,
              workflow.capabilities != nil, let workflowReadAt,
              now.timeIntervalSince(workflowReadAt) <= 45 else { return false }
        return endpoints.allSatisfy { workflow.readErrors[$0] == nil }
    }
    func canDecide(_ card: WorkflowCard) -> Bool {
        canWrite(["feed"]) && reviewFeed?.cards.contains(card) == true
    }
    func canAnswer(_ process: WorkflowProcess) -> Bool {
        let capabilities = workflow?.capabilities ?? (demo ? snapshot?.feed.capabilities : nil)
        return canWrite(["tasks", "processes", "questions"]) && (workflow?.questionSyncAllowsAnswers ?? true) && acceptsQuestionAnswers(process) && processes.contains(process) &&
            capabilities?.questions == true && capabilities?.questionScopes.contains("application") == true
    }
    func canCancel(_ task: WorkflowTask) -> Bool {
        canWrite(["tasks", "processes"]) && tasks.contains(task) && task.canCancel &&
            !processes.contains { $0.taskID == task.id && !["not_started", "preparing"].contains($0.submissionStatus) }
    }

    /// All presentation data comes from the same durable session used for commands.
    func receiveWorkflow(_ value: WorkflowView, successfulRead: Bool = true) {
        workflow = value
        synchronizeQuestionEditors()
        if successfulRead && value.online { workflowReadAt = Date() }
        if let feed = value.feed, let health = value.health {
            snapshot = DashboardSnapshot(feed: feed, health: health, tasks: value.tasks,
                                         processes: value.processes, questions: value.questions,
                                         fetchedAt: value.savedAt ?? Date())
        }
        if !value.online {
            errorMessage = "The workspace is offline. Saved data is available for review."
        } else if value.capabilities == nil {
            errorMessage = "This service does not support the current review workflow."
        } else if !value.readErrors.isEmpty {
            errorMessage = "Some workspace updates failed: " + value.readErrors.keys.sorted().joined(separator: ", ") + "."
        } else { errorMessage = nil }
        recovery = errorMessage == nil ? nil : "Refresh to get current data before making a new decision."
        if value.feed?.cards.contains(where: { $0.id == selectedCardID }) != true {
            selectedCardID = value.feed?.cards.first?.id
        }
        now = Date(); onChange?()
    }

    func decide(card: WorkflowCard, action: String, selected: Set<String>, note: String = "") async {
        now = Date()
        guard canDecide(card) else { commandError = "Refresh this card before making a decision."; return }
        guard ["apply", "find_more", "exclude"].contains(action) else {
            commandError = "This decision is not supported."; return
        }
        do { _ = try WorkflowDecisionRequest(card: card, action: action, selected: selected, note: note) }
        catch { commandError = error.localizedDescription.safeDiagnostic; return }
        if action == "apply", reviewFeed?.capabilities.explicitApply != true {
            commandError = "This workspace does not support Apply."; return
        }
        let message = action == "apply" ? "Application authorized for \(selected.count) job\(selected.count == 1 ? "" : "s") at \(card.company.name). Follow progress on the Dashboard." :
            action == "find_more" ? "More jobs requested for \(card.company.name)." : "\(card.company.name) excluded."
        if demo {
            guard let snapshot else { return }
            var feed = snapshot.feed
            feed.cards.removeAll { $0.id == card.id }
            feed.counts.readyCards = feed.cards.count
            feed.counts.readyJobs = feed.cards.reduce(0) { $0 + $1.jobs.count }
            self.snapshot = DashboardSnapshot(feed: feed, health: snapshot.health, tasks: snapshot.tasks,
                                             processes: snapshot.processes, questions: snapshot.questions, fetchedAt: Date())
            selectedCardID = feed.cards.first?.id
            commandMessage = "Demo only · " + message; commandError = nil
            return
        }
        await performCommand(message: message) { service in
            try await service.decide(card: card, action: action, selected: selected, excludeJobKeys: [], note: note)
        }
    }

    func answer(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue], correctionIDs: Set<String> = []) async {
        now = Date()
        let currentCorrection = !correctionIDs.isEmpty && correctionIDs.isSubset(of: Set(values.keys)) && questions.allSatisfy { original in
            self.questions.contains { $0.hasSameAnswerBinding(as: original) }
        }
        if !currentCorrection, reconcileAcceptedAnswers(process: process, questions: questions, values: values) { return }
        if hasDeferredSharedAnswers(questions: questions, values: values) {
            commandMessage = nil
            commandError = "A shared answer is already accepted. This form is waiting to sync. Your draft was kept; no duplicate answer was sent."
            return
        }
        guard canAnswer(process), questions.allSatisfy({ original in self.questions.contains { $0.hasSameAnswerBinding(as: original) } }) else {
            commandError = "The application form changed or is unavailable. Refresh and review its current questions."; return
        }
        guard !questions.contains(where: { values[$0.id] != nil && $0.hasAcceptedAnswer && !(currentCorrection && correctionIDs.contains($0.id)) }) else {
            commandError = "An answer is already accepted. Refresh to review the current server answers."; return
        }
        do { _ = try WorkflowAnswerRequest(process: process, questions: questions, values: values) }
        catch { commandError = error.localizedDescription.safeDiagnostic; return }
        if demo {
            guard let snapshot else { return }
            var updated = snapshot.questions
            for index in updated.indices where values[updated[index].id] != nil {
                updated[index].answer = values[updated[index].id]; updated[index].revision += 1
                updated[index].answeredAt = ISO8601DateFormatter().string(from: Date())
            }
            self.snapshot = DashboardSnapshot(feed: snapshot.feed, health: snapshot.health, tasks: snapshot.tasks,
                                             processes: snapshot.processes, questions: updated, fetchedAt: Date())
            synchronizeQuestionEditors()
            commandMessage = "Demo only · Answers saved."; commandError = nil
            return
        }
        await performCommand(message: "Answers saved. The application resumes when all required answers are complete.") { service in
            try await service.answer(process: process, questions: questions, values: values)
        }
        if commandError == nil, workflow?.lastAcknowledgement?.alreadyAnswered == true {
            commandMessage = "These answers were already accepted by the server. The workspace is updated."
        }
        if commandError != nil, pendingSave == nil {
            // A definitive rejection can be reconciled with fresh reads. Never replay the write.
            await refresh()
            _ = reconcileAcceptedAnswers(process: process, questions: questions, values: values)
        }
    }

    func cancel(_ task: WorkflowTask) async {
        now = Date()
        guard canCancel(task) else { commandError = "Only current queued work with no completed or uncertain submission can be cancelled."; return }
        if demo { commandMessage = "Demo only · Cancellation preview. No work was changed."; return }
        await performCommand(message: "Queued work cancelled.") { try await $0.cancel(task: task) }
    }

    func retryPending() async {
        guard !isSaving, !isRefreshing, pendingSave != nil else { return }
        let pending = pendingSave
        await performCommand(message: "The saved request was confirmed. Workspace updated.") { try await $0.retryPending() }
        if pending?.path == "api/v1/unblock-instructions" {
            guard commandError == nil, let pending,
                  let sent = try? JSONDecoder().decode(WorkflowInstructionRequest.self, from: pending.body),
                  let receipt = workflow?.lastAcknowledgement, receipt.requestID == sent.requestID else {
                agentInstructions.error = commandError ?? "The save was not confirmed. Your draft was kept."
                agentInstructions.needsRefresh = true
                return
            }
            commandMessage = AgentInstructionsModel.confirmation(mode: receipt.mode, wakeStatus: receipt.wakeStatus)
            if let selection = agentInstructions.selection, selection.origin == instructionOrigin, selection.taskID == sent.taskID {
                _ = agentInstructions.confirm(receipt, sentText: sent.text, for: selection)
                await loadInstructions(selection)
            }
        }
    }

    private func performCommand(message: String, operation: (any MacWorkflowServing) async throws -> WorkflowView) async {
        guard let workflowService, !isSaving, !isRefreshing else { return }
        isSaving = true; commandError = nil; commandMessage = nil
        defer { isSaving = false; now = Date(); onChange?() }
        do {
            receiveWorkflow(try await operation(workflowService))
            commandError = nil; commandMessage = message
        } catch {
            receiveWorkflow(await workflowService.view(), successfulRead: false)
            commandError = error.localizedDescription.safeDiagnostic
        }
    }
}
