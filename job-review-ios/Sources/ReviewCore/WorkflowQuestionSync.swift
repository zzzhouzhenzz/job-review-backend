import Foundation

/// Transport synchronization is separate from application and question business state.
public enum WorkflowQuestionSyncPhase: String, Equatable, Sendable {
    case unsupported, loading, ready, updating, pending, failed
}

public struct WorkflowQuestionSync: Codable, Equatable, Sendable {
    public var state: String
    public var revision: Int
    public var poolRevision: Int

    public init(state: String, revision: Int, poolRevision: Int) {
        self.state = state; self.revision = revision; self.poolRevision = poolRevision
    }

    enum CodingKeys: String, CodingKey {
        case state, revision
        case poolRevision = "pool_revision"
    }
}

public struct WorkflowSharedQuestionScope: Codable, Equatable, Sendable {
    public var kind: String
    public var key: String
}

/// Canonical personal/employer record. An application binding is not required for display.
public struct WorkflowSharedQuestion: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var text: String
    public var type: String
    public var choices: [WorkflowChoice]
    public var required: Bool
    public var scope: WorkflowSharedQuestionScope
    public var status: String
    public var answer: JSONValue?
    public var answerRevision: Int
    public var answeredAt: String?
    public var bindingIDs: [String]
    public var hasAcceptedAnswer: Bool { status == "answered" }
    public var answerDisplayText: String? {
        guard let answer, answer != .null else { return nil }
        if type == "text" { return answer.text }
        let ids = type == "single_choice" ? answer.text.map { [$0] } ?? [] : answer.array.compactMap(\.text)
        return ids.map { id in choices.first { $0.id == id }?.label ?? id }.joined(separator: ", ")
    }
    enum CodingKeys: String, CodingKey {
        case id, text, type, choices, required, scope, status, answer
        case answerRevision = "answer_revision", answeredAt = "answered_at", bindingIDs = "binding_ids"
    }
}

/// A ready response is a complete database snapshot. Other states carry no replacement data.
public struct WorkflowQuestionSyncSnapshot: Codable, Equatable, Sendable {
    public var sync: WorkflowQuestionSync
    public var sharedQuestions: [WorkflowSharedQuestion]?
    public var questions: [WorkflowQuestion]?
    public var processes: [WorkflowProcess]?
    enum CodingKeys: String, CodingKey {
        case sync, questions, processes
        case sharedQuestions = "shared_questions"
    }
}

extension WorkflowView {
    public var questionSyncPhase: WorkflowQuestionSyncPhase {
        guard capabilities?.questionSync == true else { return .unsupported }
        if readErrors["question_sync"] != nil { return .failed }
        guard online, let questionSync, let questionSyncReadAt else { return .loading }
        guard Date().timeIntervalSince(questionSyncReadAt) <= 45 else { return .failed }
        switch questionSync.state {
        case "updating": return .updating
        case "pending": return .pending
        case "ready":
            return questionSyncPoolRevision == questionSync.poolRevision ? .ready : .loading
        default: return .failed
        }
    }

    /// Existing legacy capability, online, pending-save and form gates remain required.
    public var questionSyncAllowsAnswers: Bool {
        [.unsupported, .ready].contains(questionSyncPhase)
    }

    public var questionSyncMessage: String? {
        switch questionSyncPhase {
        case .unsupported, .ready: return nil
        case .loading, .updating, .pending: return "Waiting for sync…"
        case .failed: return "Questions could not sync. Your drafts were kept. Refresh to try again."
        }
    }

    public var questionSyncRevision: Int? { questionSync?.revision }
}
