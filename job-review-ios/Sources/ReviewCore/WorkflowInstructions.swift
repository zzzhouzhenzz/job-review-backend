import Foundation

/// A task-scoped snapshot. The server decides whether guidance can be saved and what it can do.
public struct WorkflowInstructionTarget: Codable, Equatable, Sendable {
    public var taskID: String
    public var taskUpdatedAt: String
    public var taskStatus: String
    public var processID: String?
    public var processRevision: Int?
    public var blockReason: String?
    public var canSubmit: Bool
    public var mode: String
    public var unavailableReason: String?
    public static let supportedModes: Set<String> = ["browser_recheck", "preparation_retry", "questions_pending", "reconciliation_only", "manual_review"]
    public var supportsSubmission: Bool { canSubmit && Self.supportedModes.contains(mode) }
    enum CodingKeys: String, CodingKey {
        case mode
        case taskID = "task_id", taskUpdatedAt = "task_updated_at", taskStatus = "task_status"
        case processID = "process_id", processRevision = "process_revision", blockReason = "block_reason"
        case canSubmit = "can_submit", unavailableReason = "unavailable_reason"
    }
}

public struct WorkflowInstruction: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var requestID: String
    public var taskID: String
    public var processID: String?
    public var processRevision: Int?
    public var taskUpdatedAt: String
    public var text: String
    public var mode: String
    public var wakeStatus: String
    public var createdAt: String
    public var status: String
    enum CodingKeys: String, CodingKey {
        case id, text, mode, status
        case requestID = "request_id", taskID = "task_id", processID = "process_id", processRevision = "process_revision"
        case taskUpdatedAt = "task_updated_at", wakeStatus = "wake_status", createdAt = "created_at"
    }
}

public struct WorkflowInstructionSnapshot: Codable, Equatable, Sendable {
    public var target: WorkflowInstructionTarget
    public var instructions: [WorkflowInstruction]
}

public struct WorkflowInstructionRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var taskID: String
    public var taskUpdatedAt: String
    public var processID: String?
    public var processRevision: Int?
    public var text: String
    public static let maximumLength = 4_000

    public init(target: WorkflowInstructionTarget, text: String, requestID: String = UUID().uuidString) throws {
        guard validWorkflowID(requestID), validWorkflowID(target.taskID), target.supportsSubmission,
              !target.taskUpdatedAt.isEmpty,
              (target.processID == nil && target.processRevision == nil) ||
                (target.processID.map { validWorkflowID($0) } == true && (target.processRevision ?? 0) > 0) else {
            throw ReviewError.invalid("Refresh this blocked task before sending instructions.")
        }
        guard Self.validText(text) else {
            throw ReviewError.invalid("Enter instructions of 1–4,000 characters.")
        }
        self.requestID = requestID; taskID = target.taskID; taskUpdatedAt = target.taskUpdatedAt
        processID = target.processID; processRevision = target.processRevision; self.text = text
    }

    public static func validText(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.unicodeScalars.count <= maximumLength
    }
    enum CodingKeys: String, CodingKey {
        case text
        case requestID = "request_id", taskID = "task_id", taskUpdatedAt = "task_updated_at"
        case processID = "process_id", processRevision = "process_revision"
    }
    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(requestID, forKey: .requestID); try values.encode(taskID, forKey: .taskID)
        try values.encode(taskUpdatedAt, forKey: .taskUpdatedAt); try values.encode(text, forKey: .text)
        // Explore commands require explicit nulls, not omitted application fields.
        try values.encode(processID, forKey: .processID); try values.encode(processRevision, forKey: .processRevision)
    }
}
