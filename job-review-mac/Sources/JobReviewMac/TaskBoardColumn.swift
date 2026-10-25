import ReviewCore

/// Each task belongs to one column. Task completion does not imply an employer submission.
enum TaskBoardColumn: String, CaseIterable, Identifiable {
    case queued, running, attention, finished, other

    var id: String { rawValue }

    var title: String {
        switch self {
        case .queued: return "Queued"
        case .running: return "Running"
        case .attention: return "Needs attention"
        case .finished: return "Finished"
        case .other: return "Other"
        }
    }

    /// Existing navigation filters have no dedicated Other option.
    var filterName: String {
        switch self {
        case .queued: return "Queued"
        case .running: return "Active"
        case .attention: return "Attention"
        case .finished: return "Finished"
        case .other: return "All"
        }
    }

    static func column(for task: WorkflowTask) -> Self {
        if task.resourceQuarantined { return .attention }
        switch task.status {
        case "waiting_for_user", "failed": return .attention
        case "queued", "retry_wait": return .queued
        case "running": return .running
        case "succeeded", "cancelled": return .finished
        default: return .other
        }
    }
}
