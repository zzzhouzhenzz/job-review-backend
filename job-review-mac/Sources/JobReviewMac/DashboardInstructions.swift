import Foundation
import ReviewCore

extension DashboardModel {
    var instructionOrigin: String { settings?.origin ?? (demo ? "fictional-demo" : "injected-workspace") }
    var supportsInstructions: Bool { demo || workflow?.capabilities?.unblockInstructions == true }

    func openInstructions(_ task: WorkflowTask) {
        let selection = AgentInstructionSelection(origin: instructionOrigin, taskID: task.id,
                                                   title: taskTitle(task), subtitle: taskSubtitle(task))
        agentInstructions.select(selection)
        instructionSelection = selection
    }

    func canSendInstructions(_ selection: AgentInstructionSelection) -> Bool {
        selection.origin == instructionOrigin && agentInstructions.selection == selection &&
        supportsInstructions && canWrite([]) && !agentInstructions.isLoading && !agentInstructions.needsRefresh &&
        agentInstructions.snapshot?.target.taskID == selection.taskID &&
        agentInstructions.snapshot?.target.supportsSubmission == true && agentInstructions.validText
    }

    func loadInstructions(_ selection: AgentInstructionSelection) async {
        guard selection.origin == instructionOrigin, agentInstructions.selection == selection else { return }
        guard !isSaving, !isRefreshing else {
            agentInstructions.error = "Wait for the current update to finish, then refresh instructions."; return
        }
        guard supportsInstructions else {
            agentInstructions.error = "This backend does not support instructions yet. Refresh after the backend is updated."; return
        }
        isRefreshing = true; agentInstructions.isLoading = true; agentInstructions.error = nil
        defer { isRefreshing = false; agentInstructions.isLoading = false }
        do {
            if demo {
                if agentInstructions.snapshot == nil {
                    agentInstructions.snapshot = try DemoSnapshot.instructions(taskID: selection.taskID)
                }
            } else {
                guard let service = workflowService else { throw ReviewError.offline }
                let snapshot = try await service.instructions(taskID: selection.taskID)
                guard selection.origin == instructionOrigin, agentInstructions.selection == selection,
                      snapshot.target.taskID == selection.taskID else { throw ReviewError.invalid("The instruction history belongs to a different task.") }
                agentInstructions.snapshot = snapshot
            }
            agentInstructions.needsRefresh = false
        } catch {
            agentInstructions.error = error.localizedDescription.safeDiagnostic
            agentInstructions.needsRefresh = true
        }
    }

    func sendInstructions(_ selection: AgentInstructionSelection) async {
        now = Date()
        guard canSendInstructions(selection), let target = agentInstructions.snapshot?.target else {
            agentInstructions.error = "Refresh the task and review its status before sending instructions."; return
        }
        let text = agentInstructions.text
        isSaving = true; agentInstructions.error = nil; agentInstructions.message = nil
        commandError = nil; commandMessage = nil
        do {
            if demo {
                let (snapshot, receipt) = try DemoSnapshot.savedInstructions(snapshot: agentInstructions.snapshot!, text: text)
                agentInstructions.snapshot = snapshot
                _ = agentInstructions.confirm(receipt, sentText: text, for: selection)
            } else {
                guard let service = workflowService else { throw ReviewError.offline }
                let previousReceipt = workflow?.lastAcknowledgement?.requestID
                let result = try await service.sendInstructions(target: target, text: text)
                receiveWorkflow(result)
                guard let receipt = result.lastAcknowledgement, receipt.requestID != previousReceipt,
                      agentInstructions.confirm(receipt, sentText: text, for: selection) else {
                    throw ReviewError.uncertain("The save was not confirmed. Your draft was kept.")
                }
            }
            commandMessage = agentInstructions.message
        } catch {
            if !demo, let service = workflowService { receiveWorkflow(await service.view(), successfulRead: false) }
            agentInstructions.error = instructionError(error)
            // A reviewed target must be loaded again after any failed save.
            agentInstructions.needsRefresh = true
        }
        isSaving = false; now = Date(); onChange?()
        if agentInstructions.error == nil && !demo { await loadInstructions(selection) }
    }

    func instructionError(_ error: Error) -> String {
        if let conflict = error as? WorkflowHTTPError, conflict.status == 409 {
            return "The task changed or is busy. Your draft was kept. Refresh instructions and review the current status before sending again."
        }
        if pendingSave != nil { return "The save could not be confirmed. Your draft was kept. Check the saved request before sending new instructions." }
        return error.localizedDescription.safeDiagnostic
    }
}
