import Foundation
import Combine
import ReviewCore

struct AgentInstructionSelection: Identifiable, Equatable {
    let origin: String
    let taskID: String
    let title: String
    let subtitle: String
    var id: String { origin + "\n" + taskID }
}

/// Drafts stay bound to one task and origin. Only a confirmed receipt clears sent text.
@MainActor final class AgentInstructionsModel: ObservableObject {
    @Published private(set) var selection: AgentInstructionSelection?
    @Published var snapshot: WorkflowInstructionSnapshot?
    @Published var isLoading = false
    @Published var error: String?
    @Published var message: String?
    @Published var needsRefresh = true
    @Published private var drafts: [String: String] = [:]

    var text: String {
        get { selection.flatMap { drafts[$0.id] } ?? "" }
        set { if let selection { drafts[selection.id] = newValue } }
    }
    var count: Int { text.unicodeScalars.count }
    var validText: Bool { WorkflowInstructionRequest.validText(text) }

    func select(_ value: AgentInstructionSelection) {
        selection = value; snapshot = nil; error = nil; message = nil; needsRefresh = true
    }
    func reset() {
        selection = nil; snapshot = nil; drafts = [:]; error = nil; message = nil; needsRefresh = true
    }
    func confirm(_ receipt: WorkflowAcknowledgement, sentText: String, for value: AgentInstructionSelection) -> Bool {
        guard receipt.accepted, receipt.instructionID != nil, receipt.taskIDs == [value.taskID] else { return false }
        if drafts[value.id] == sentText { drafts[value.id] = nil }
        if selection == value {
            error = nil
            message = Self.confirmation(mode: receipt.mode, wakeStatus: receipt.wakeStatus)
        }
        return true
    }

    static func effect(_ mode: String) -> String {
        switch mode {
        case "preparation_retry": return "Saving retries preparation for this already approved application. The agent must still pass the existing readiness and approval checks."
        case "browser_recheck": return "Saving asks the agent to check the browser prerequisites. The task stays blocked until that check succeeds."
        case "questions_pending": return "Guidance is saved for the agent. Complete required answers in Application questions to continue."
        case "reconciliation_only": return "A submission may have occurred. Guidance is saved for review of that attempt. Saving does not retry the submission."
        case "manual_review": return "Guidance is saved for review. Saving does not restart this task."
        default: return "This task cannot accept new instructions. You can still read its saved history."
        }
    }
    static func confirmation(mode: String?, wakeStatus: String?) -> String {
        if wakeStatus == "retry_scheduled" {
            return "Instructions saved. A preparation retry is scheduled for the approved application. The blocker is not yet confirmed clear."
        }
        if wakeStatus == "recheck_scheduled" {
            return "Instructions saved. A browser prerequisite check is scheduled. The task has not yet resumed."
        }
        if wakeStatus == "saved_only" {
            switch mode {
            case "questions_pending": return "Instructions saved. Required application answers still need to be completed."
            case "reconciliation_only": return "Instructions saved for review of the uncertain attempt. No submission retry was scheduled."
            default: return "Instructions saved for review. The task was not restarted."
            }
        }
        return "Instructions saved. Refresh the task to see its current status."
    }
    static func unavailable(_ reason: String?) -> String {
        switch reason {
        case "task_busy": return "A task or check is running. Refresh after it stops."
        case "task_not_blocked": return "This task is not blocked. Saved instructions remain available below."
        case "task_closed": return "This task is closed. Saved instructions remain available below."
        case "authorization_changed": return "The application approval changed. Review the task before continuing."
        case "stale_revision": return "The task changed. Refresh and review its current status."
        default: return "This task cannot accept new instructions. Refresh to check its status."
        }
    }
}
