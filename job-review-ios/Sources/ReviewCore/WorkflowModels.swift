import Foundation

/// Status and type fields remain strings so future server states stay visible.
public struct WorkflowCapabilities: Codable, Equatable, Sendable {
    public var apiVersion: Int
    public var workerEnabled: Bool
    public var workerStatus: String
    public var explicitApply: Bool
    public var questions: Bool
    public var feedDelivery: String
    public var questionScopes: [String]
    public var remoteAccess: Bool
    public var unblockInstructions: Bool? = nil
    public var questionSync: Bool? = nil
    enum CodingKeys: String, CodingKey {
        case questions
        case apiVersion = "api_version", workerEnabled = "worker_enabled", workerStatus = "worker_status"
        case explicitApply = "explicit_apply", feedDelivery = "feed_delivery"
        case questionScopes = "question_scopes", remoteAccess = "remote_access"
        case unblockInstructions = "unblock_instructions"
        case questionSync = "question_sync"
    }
}

public struct WorkflowHealthCheck: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var status: String
    public var message: String
}

public struct WorkflowHealth: Codable, Equatable, Sendable {
    public var bootID: String
    public var status: String
    public var ready: Bool
    public var checkedAt: String?
    public var expiresAt: String?
    public var retryAt: String?
    public var checks: [WorkflowHealthCheck]
    // Browser checks run when a task needs them. Older servers may include extra checks.
    static let requiredCheckIDs: Set<String> = ["storage", "chrome_attachment", "linkedin_auth"]
    /// A cached result is not evidence of a live connection. Callers must also check online/read-error state.
    public func isFresh(at date: Date = Date()) -> Bool {
        guard ready, status == "ready", let checkedAt, let expiresAt,
              Self.requiredCheckIDs.isSubset(of: Set(checks.map(\.id))), checks.allSatisfy({ $0.status == "passed" }),
              let checked = Self.timestamp(checkedAt), let deadline = Self.timestamp(expiresAt) else { return false }
        return checked <= date && deadline > checked && deadline > date
    }
    static func timestamp(_ value: String) -> Date? {
        let format = ISO8601DateFormatter()
        format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = format.date(from: value) { return date }
        format.formatOptions = [.withInternetDateTime]
        return format.date(from: value)
    }
    enum CodingKeys: String, CodingKey {
        case status, ready, checks
        case bootID = "boot_id", checkedAt = "checked_at", expiresAt = "expires_at", retryAt = "retry_at"
    }
}

public struct WorkflowCard: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var revision: String
    public var company: Company
    public var jobs: [Job]
    public var content: WorkflowCardContent?
    public var createdAt: String
    enum CodingKeys: String, CodingKey { case id, revision, company, jobs, content; case createdAt = "created_at" }
}

public struct WorkflowCardBrief: Codable, Equatable, Sendable {
    public var work: String
    public var fit: String
    public var gap: String
}

public struct WorkflowCardJobContent: Codable, Equatable, Identifiable, Sendable {
    public var key: String
    public var revision: Int
    public var reason: String
    public var brief: WorkflowCardBrief
    public var id: String { key }
}

/// Render these fields as plain text. They do not contain frontend instructions.
public struct WorkflowCardContent: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var source: String
    public var summary: String
    public var jobs: [WorkflowCardJobContent]
    public var isSupported: Bool { schemaVersion == 1 && source == "ranker" }
    public func validate(for card: WorkflowCard) throws {
        func textFits(_ text: String, limit: Int) -> Bool {
            !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.unicodeScalars.count <= limit
        }
        guard isSupported, textFits(summary, limit: 1_000), (1...3).contains(jobs.count),
              jobs.count == card.jobs.count,
              zip(jobs, card.jobs).allSatisfy({ content, job in
                  content.key == job.key && content.revision == job.revision && content.revision > 0 &&
                  textFits(content.reason, limit: 5_000) && textFits(content.brief.work, limit: 600) &&
                  textFits(content.brief.fit, limit: 600) && textFits(content.brief.gap, limit: 600)
              }) else {
            throw ReviewError.uncertain("The ranked card text is unsupported or does not match these exact job versions.")
        }
    }
    enum CodingKeys: String, CodingKey { case source, summary, jobs; case schemaVersion = "schema_version" }
}

public struct WorkflowCounts: Codable, Equatable, Sendable {
    public var readyCards: Int
    public var readyJobs: Int
    enum CodingKeys: String, CodingKey { case readyCards = "ready_cards", readyJobs = "ready_jobs" }
}

public struct WorkflowRefill: Codable, Equatable, Sendable {
    public var lowWatermark: Int
    public var targetCards: Int
    public var nextAllowedAt: String?
    enum CodingKeys: String, CodingKey {
        case lowWatermark = "low_watermark", targetCards = "target_cards", nextAllowedAt = "next_allowed_at"
    }
}

/// Token-free data from one complete feed snapshot.
public struct WorkflowFeed: Codable, Equatable, Sendable {
    public var revision: String
    public var cards: [WorkflowCard]
    public var counts: WorkflowCounts
    public var refill: WorkflowRefill
    public var capabilities: WorkflowCapabilities
}

public struct WorkflowReceipt: Codable, Equatable, Sendable {
    public var url: String
    public var evidence: String
    public var observedAt: String
    public var reference: String
    public var safeURL: URL? { Job.safeURL(url) }
    enum CodingKeys: String, CodingKey { case url, evidence, reference; case observedAt = "observed_at" }
}

public struct WorkflowEvent: Codable, Equatable, Identifiable, Sendable {
    public var id: Int
    public var processID: String
    public var type: String
    public var actor: String
    public var data: [String: JSONValue]
    public var createdAt: String
    enum CodingKeys: String, CodingKey { case id, type, actor, data; case processID = "process_id", createdAt = "created_at" }
}

public struct WorkflowProcess: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var jobKey: String
    public var taskID: String
    public var decisionID: String
    public var snapshot: [String: JSONValue]
    public var submissionStatus: String
    public var recruitingStatus: String
    public var taskStatus: String
    public var revision: Int
    public var receipt: WorkflowReceipt?
    public var blockReason: String?
    public var formVersion: String?
    public var createdAt: String
    public var updatedAt: String
    public var events: [WorkflowEvent]
    public var title: String { snapshot["title"]?.text ?? "Application" }
    public var companyName: String { snapshot["company_name"]?.text ?? "Company" }
    public var officialURL: URL? { Job.safeURL(snapshot["url"]?.text) }
    public var canAnswer: Bool {
        taskStatus == "waiting_for_user" && submissionStatus == "preparing" && formVersion?.isEmpty == false
    }
    enum CodingKeys: String, CodingKey {
        case id, snapshot, revision, receipt, events
        case jobKey = "job_key", taskID = "task_id", decisionID = "decision_id"
        case submissionStatus = "submission_status", recruitingStatus = "recruiting_status", taskStatus = "task_status"
        case blockReason = "block_reason", formVersion = "form_version", createdAt = "created_at", updatedAt = "updated_at"
    }
}

public struct WorkflowChoice: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var label: String
}

public struct WorkflowQuestion: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var processID: String
    public var formVersion: String
    public var revision: Int
    public var text: String
    public var type: String
    public var choices: [WorkflowChoice]
    public var required: Bool
    public var scope: String
    public var reason: String
    public var answer: JSONValue?
    public var answeredAt: String?
    public var createdAt: String
    public var updatedAt: String
    /// Optional additive metadata; application-local IDs and choice values stay unchanged.
    public var poolQuestionID: String? = nil
    public var poolRevision: Int? = nil
    public var poolAnswerRevision: Int? = nil
    /// Provenance of the coherent snapshot the user reviewed. This is not field identity.
    public var snapshotPoolRevision: Int? = nil
    public var isSupported: Bool { scope == "application" && ["text", "single_choice", "multi_choice"].contains(type) }
    public var hasAcceptedAnswer: Bool {
        answer != nil && answer != .null && answeredAt.flatMap(WorkflowHealth.timestamp) != nil
    }
    /// Shared-pool observation changes do not change this form's answer binding.
    public func hasSameAnswerBinding(as other: WorkflowQuestion) -> Bool {
        id == other.id && processID == other.processID && formVersion == other.formVersion &&
            revision == other.revision && text == other.text && type == other.type &&
            choices == other.choices && required == other.required && scope == other.scope &&
            answer == other.answer && hasAcceptedAnswer == other.hasAcceptedAnswer
    }
    public func validate(_ value: JSONValue) throws {
        guard isSupported else { throw ReviewError.invalid("This question type is not supported by this app.") }
        let choiceIDs = Set(choices.map(\.id))
        guard type == "text" ? choices.isEmpty : ((1...100).contains(choices.count) && choiceIDs.count == choices.count) else {
            throw ReviewError.invalid("The question's choices are invalid. Refresh the current form.")
        }
        switch (type, value) {
        case ("text", .string(let text)):
            guard text.unicodeScalars.count <= 20_000,
                  !required || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw ReviewError.invalid("Enter an answer of at most 20,000 characters for this required question.")
            }
        case ("single_choice", .string(let choice)):
            guard choiceIDs.contains(choice) else { throw ReviewError.invalid("Select one of the listed choices.") }
        case ("multi_choice", .array(let values)):
            let chosen = values.compactMap(\.text)
            guard chosen.count == values.count, Set(chosen).count == chosen.count,
                  Set(chosen).isSubset(of: choiceIDs), !required || !chosen.isEmpty else {
                throw ReviewError.invalid("Select unique listed choices for this question.")
            }
        default: throw ReviewError.invalid("The answer does not match this question type.")
        }
    }
    enum CodingKeys: String, CodingKey {
        case id, revision, text, type, choices, required, scope, reason, answer
        case processID = "process_id", formVersion = "form_version", answeredAt = "answered_at"
        case createdAt = "created_at", updatedAt = "updated_at"
        case poolQuestionID = "pool_question_id", poolRevision = "pool_revision"
        case poolAnswerRevision = "pool_answer_revision"
        case snapshotPoolRevision = "snapshot_pool_revision"
    }
}

public struct WorkflowTask: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var kind: String
    public var status: String
    public var attempts: Int
    public var failureCount: Int
    public var maxAttempts: Int
    public var availableAt: String
    public var resource: String?
    public var resourceQuarantined: Bool
    public var error: String?
    public var createdAt: String
    public var updatedAt: String
    public var canCancel: Bool { ["queued", "retry_wait"].contains(status) && !resourceQuarantined }
    enum CodingKeys: String, CodingKey {
        case id, kind, status, attempts, resource, error
        case failureCount = "failure_count", maxAttempts = "max_attempts", availableAt = "available_at"
        case resourceQuarantined = "resource_quarantined", createdAt = "created_at", updatedAt = "updated_at"
    }
}

/// A saved acknowledgement confirms authorization, never employer submission.
public struct WorkflowAcknowledgement: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var requestID: String
    public var accepted: Bool
    public var decisionID: String?
    public var cardID: String?
    public var action: String?
    public var processIDs: [String]
    public var taskIDs: [String]
    public var instructionID: String? = nil
    public var mode: String? = nil
    public var wakeStatus: String? = nil
    public var taskUpdatedAt: String? = nil
    public var alreadyAnswered: Bool? = nil
    enum CodingKeys: String, CodingKey {
        case accepted, action, mode
        case schemaVersion = "schema_version", requestID = "request_id", decisionID = "decision_id", cardID = "card_id"
        case processIDs = "process_ids", taskIDs = "task_ids"
        case instructionID = "instruction_id", wakeStatus = "wake_status", taskUpdatedAt = "task_updated_at"
        case alreadyAnswered = "already_answered"
    }
}

public struct WorkflowDecisionRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var action: String
    public var cardID: String
    public var cardRevision: String
    public var selected: [String]
    public var excludeJobKeys: [String]
    public var note: String
    public init(card: WorkflowCard, action: String, selected: Set<String>, excludeJobKeys: Set<String> = [],
                note: String = "", requestID: String = UUID().uuidString) throws {
        let keys = Set(card.jobs.map(\.key))
        guard validWorkflowID(requestID), validWorkflowID(card.id), !card.revision.isEmpty,
              (1...3).contains(card.jobs.count), keys.count == card.jobs.count,
              card.jobs.allSatisfy({ $0.companyID == card.company.id && $0.revision > 0 }),
              ["apply", "pass", "later", "find_more", "exclude"].contains(action),
              selected.isSubset(of: keys), selected.allSatisfy({ validWorkflowID($0, limit: 1_000) }),
              excludeJobKeys.count <= 1_000, excludeJobKeys.allSatisfy({ validWorkflowID($0, limit: 1_000) }),
              action == "find_more" || excludeJobKeys.isEmpty, note.unicodeScalars.count <= 2_500 else {
            throw ReviewError.invalid("Check the current card, selected jobs, exclusions, and note.")
        }
        guard ["apply", "pass", "later"].contains(action) ? !selected.isEmpty : selected.isEmpty else {
            throw ReviewError.invalid("Select jobs for Apply, Pass, or Later. Find more and Exclude require an empty selection.")
        }
        try card.content?.validate(for: card)
        self.requestID = requestID; self.action = action; cardID = card.id; cardRevision = card.revision
        self.selected = selected.sorted(); self.excludeJobKeys = excludeJobKeys.sorted(); self.note = note
    }
    enum CodingKeys: String, CodingKey {
        case action, selected, note
        case requestID = "request_id", cardID = "card_id", cardRevision = "card_revision", excludeJobKeys = "exclude_job_keys"
    }
}

public struct WorkflowAnswer: Codable, Equatable, Sendable {
    public var questionID: String
    public var questionRevision: Int
    public var value: JSONValue
    enum CodingKeys: String, CodingKey { case value; case questionID = "question_id", questionRevision = "question_revision" }
}

public struct WorkflowAnswerRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var processID: String
    public var processRevision: Int
    public var formVersion: String
    public var answers: [WorkflowAnswer]
    public var expectedPoolRevision: Int? = nil
    public init(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue],
                requestID: String = UUID().uuidString, expectedPoolRevision: Int? = nil) throws {
        guard validWorkflowID(requestID), validWorkflowID(process.id), process.revision > 0,
              process.canAnswer, let form = process.formVersion, (1...50).contains(values.count),
              Set(questions.map(\.id)).count == questions.count,
              Set(values.keys).isSubset(of: Set(questions.map(\.id))),
              questions.allSatisfy({ $0.processID == process.id && $0.formVersion == form && $0.revision > 0 }),
              expectedPoolRevision.map({ $0 >= 0 }) ?? true else {
            throw ReviewError.invalid("Refresh this application and its current questions before answering.")
        }
        self.requestID = requestID; processID = process.id; processRevision = process.revision; formVersion = form
        self.expectedPoolRevision = expectedPoolRevision
        answers = try questions.filter { values[$0.id] != nil }.sorted { $0.id < $1.id }.map { question in
            let value = values[question.id]!
            try question.validate(value)
            return WorkflowAnswer(questionID: question.id, questionRevision: question.revision, value: value)
        }
    }
    enum CodingKeys: String, CodingKey {
        case answers
        case requestID = "request_id", processID = "process_id", processRevision = "process_revision", formVersion = "form_version"
        case expectedPoolRevision = "expected_pool_revision"
    }
}

public struct WorkflowCancelRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var taskID: String
    public init(task: WorkflowTask, requestID: String = UUID().uuidString) throws {
        guard validWorkflowID(requestID), validWorkflowID(task.id), task.canCancel else {
            throw ReviewError.invalid("Only queued work or work waiting to retry can be cancelled.")
        }
        self.requestID = requestID; taskID = task.id
    }
    enum CodingKeys: String, CodingKey { case requestID = "request_id", taskID = "task_id" }
}

func validWorkflowID(_ value: String, limit: Int = 200) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && value.unicodeScalars.count <= limit
}
