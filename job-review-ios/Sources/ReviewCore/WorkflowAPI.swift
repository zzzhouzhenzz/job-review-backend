import Foundation

/// Credentials exist only in transport results, never in the durable workflow cache.
public struct WorkflowRead<Value: Sendable>: Sendable {
    public var value: Value
    public var revision: String
    public var token: String
}

public struct WorkflowHTTPError: Error, LocalizedError, Equatable, Sendable {
    public var status: Int
    public var code: String?
    public var reason: String?
    public var commitStatus: String?
    public var message: String
    public var errorDescription: String? { message }
    /// Only a stable conflict reason can distinguish stale data from a reused request ID.
    public var isDefinitiveRejection: Bool {
        if status == 409 {
            return code == "conflict" && commitStatus == "not_committed" &&
                ["stale_revision", "question_sync_stale", "operation_rejected", "task_busy", "task_not_blocked", "task_closed", "authorization_changed"].contains(reason ?? "")
        }
        // Other 4xx responses (including intermediary 413/415/422 responses) do not
        // have defined commit guarantees in this contract, so keep their pending body.
        let codes = [400: "invalid_request", 404: "not_found"]
        return commitStatus == "not_committed" && codes[status] == code && code != nil
    }
}

public struct WorkflowAPI: Sendable {
    public let address: ServerAddress
    private let transport: any ReviewTransport
    private let accessToken: String?
    public init(address: ServerAddress, transport: any ReviewTransport, accessToken: String? = nil) {
        self.address = address; self.transport = transport; self.accessToken = accessToken
    }

    /// Only a 404 means legacy fallback. Malformed, unsupported, and failed discovery stay errors.
    public func discover() async throws -> WorkflowCapabilities? { try await discoverRead()?.value }
    public func discoverRead() async throws -> WorkflowRead<WorkflowCapabilities>? {
        let response = try await transport.send(request(path: "api/v1/capabilities"))
        if response.status == 404 { return nil }
        let object = try readObject(response)
        let capabilities = try decode(WorkflowCapabilities.self, object: object["capabilities"])
        try validate(capabilities)
        return try envelope(capabilities, object: object)
    }

    /// Older v1 servers have no readiness endpoint. That 404 does not select the legacy write protocol.
    public func fetchHealth() async throws -> WorkflowRead<WorkflowHealth>? {
        let response = try await transport.send(request(path: "api/v1/health"))
        if response.status == 404 { return nil }
        let object = try readObject(response)
        let health = try decode(WorkflowHealth.self, object: object["health"])
        try requiredNullable(object["health"]?.object ?? [:], keys: ["checked_at", "expires_at", "retry_at"])
        let ids = Set(health.checks.map(\.id))
        guard !health.bootID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !health.status.isEmpty, ids.count == health.checks.count,
              WorkflowHealth.requiredCheckIDs.isSubset(of: ids),
              health.checks.allSatisfy({ !$0.id.isEmpty && !$0.status.isEmpty }),
              [health.checkedAt, health.expiresAt, health.retryAt].compactMap({ $0 }).allSatisfy({ WorkflowHealth.timestamp($0) != nil }),
              !health.ready || (health.status == "ready" && health.checkedAt != nil && health.expiresAt != nil &&
                               health.checks.allSatisfy({ $0.status == "passed" })) else { throw malformed() }
        return try envelope(health, object: object)
    }

    public func fetchFeed() async throws -> WorkflowRead<WorkflowFeed> {
        let object = try readObject(await transport.send(request(path: "api/v1/feed")))
        let feed = try decode(WorkflowFeed.self, object: .object(object))
        try validate(feed.capabilities)
        guard feed.counts.readyCards >= 0, feed.counts.readyJobs >= 0,
              feed.refill.lowWatermark >= 0, feed.refill.targetCards >= 0,
              Set(feed.cards.map(\.id)).count == feed.cards.count,
              Set(feed.cards.map { $0.company.id }).count == feed.cards.count else { throw malformed() }
        try requiredNullable(object["refill"]?.object ?? [:], keys: ["next_allowed_at"])
        for (index, card) in feed.cards.enumerated() {
            guard validWorkflowID(card.id), !card.revision.isEmpty, (1...3).contains(card.jobs.count),
                  card.company.revision > 0, !card.company.id.isEmpty,
                  Set(card.jobs.map(\.key)).count == card.jobs.count,
                  card.jobs.allSatisfy({ $0.companyID == card.company.id && $0.revision > 0 && !$0.key.isEmpty && $0.officialURL != nil }) else {
                throw malformed()
            }
            try card.content?.validate(for: card)
            let rawCard = object["cards"]!.array[index].object
            try timestampFields(rawCard["company"]?.object ?? [:], names: ["first_seen", "updated_at"])
            for rawJob in rawCard["jobs"]?.array ?? [] {
                try timestampFields(rawJob.object, names: ["first_seen", "updated_at"])
                let data = rawJob.object["data"]?.object ?? [:]
                guard data["job_id"]?.text != nil, data["title"]?.text != nil else { throw malformed() }
            }
        }
        return try envelope(feed, object: object)
    }

    public func fetchProcesses() async throws -> WorkflowRead<[WorkflowProcess]> {
        let object = try readObject(await transport.send(request(path: "api/v1/processes")))
        let processes = try decode([WorkflowProcess].self, object: object["processes"])
        guard Set(processes.map(\.id)).count == processes.count else { throw malformed() }
        for (index, process) in processes.enumerated() {
            try requiredNullable(object["processes"]!.array[index].object, keys: ["receipt", "block_reason", "form_version"])
            guard process.revision > 0, !process.id.isEmpty, !process.taskID.isEmpty, !process.jobKey.isEmpty,
                  process.snapshot["company_id"]?.text != nil, process.snapshot["company_name"]?.text != nil,
                  process.snapshot["job_key"]?.text == process.jobKey,
                  process.snapshot["material_hash"]?.text?.isEmpty == false,
                  process.snapshot["job_id"]?.text != nil, process.snapshot["title"]?.text != nil,
                  Job.safeURL(process.snapshot["url"]?.text) != nil,
                  case .number(let revision) = process.snapshot["job_revision"], revision > 0, revision.rounded() == revision,
                  Set(process.events.map(\.id)).count == process.events.count,
                  process.events.map(\.id) == process.events.map(\.id).sorted(),
                  process.events.allSatisfy({ $0.id > 0 && $0.processID == process.id }),
                  process.submissionStatus != "submitted" || process.receipt != nil else { throw malformed() }
            if let receipt = process.receipt, receipt.safeURL == nil { throw malformed() }
        }
        return try envelope(processes, object: object)
    }

    public func fetchQuestions() async throws -> WorkflowRead<[WorkflowQuestion]> {
        let object = try readObject(await transport.send(request(path: "api/v1/questions")))
        let questions = try decode([WorkflowQuestion].self, object: object["questions"])
        guard Set(questions.map(\.id)).count == questions.count else { throw malformed() }
        for (index, question) in questions.enumerated() {
            try requiredNullable(object["questions"]!.array[index].object, keys: ["answer", "answered_at"])
            guard !question.id.isEmpty, !question.processID.isEmpty, !question.formVersion.isEmpty, question.revision > 0,
                  Set(question.choices.map(\.id)).count == question.choices.count else { throw malformed() }
            if let id = question.poolQuestionID, id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { throw malformed() }
            if let revision = question.poolRevision, revision < 0 { throw malformed() }
            if let stamp = question.answeredAt, WorkflowHealth.timestamp(stamp) == nil { throw malformed() }
            if question.type == "text", !question.choices.isEmpty { throw malformed() }
            if ["single_choice", "multi_choice"].contains(question.type), !(1...100).contains(question.choices.count) { throw malformed() }
            if let answer = question.answer, answer != .null, question.isSupported { try question.validate(answer) }
        }
        return try envelope(questions, object: object)
    }

    public func fetchTasks() async throws -> WorkflowRead<[WorkflowTask]> {
        let object = try readObject(await transport.send(request(path: "api/v1/tasks")))
        let tasks = try decode([WorkflowTask].self, object: object["tasks"])
        guard Set(tasks.map(\.id)).count == tasks.count else { throw malformed() }
        for (index, task) in tasks.enumerated() {
            try requiredNullable(object["tasks"]!.array[index].object, keys: ["resource", "error"])
            guard !task.id.isEmpty, task.attempts >= 0, task.failureCount >= 0, task.maxAttempts > 0 else { throw malformed() }
        }
        return try envelope(tasks, object: object)
    }

    public func fetchQuestionSync() async throws -> WorkflowRead<WorkflowQuestionSyncSnapshot> {
        let object = try readObject(await transport.send(request(path: "api/v1/question-sync")))
        let raw = object["question_sync"]?.object ?? [:]
        try requiredNullable(raw, keys: ["shared_questions", "questions", "processes"])
        var snapshot = try decode(WorkflowQuestionSyncSnapshot.self, object: object["question_sync"])
        guard ["ready", "updating", "pending"].contains(snapshot.sync.state),
              snapshot.sync.revision >= 0, snapshot.sync.poolRevision >= 0 else { throw malformed() }
        guard snapshot.sync.state == "ready" else {
            guard snapshot.sharedQuestions == nil, snapshot.questions == nil, snapshot.processes == nil else { throw malformed() }
            return try envelope(snapshot, object: object)
        }
        guard let shared = snapshot.sharedQuestions, var questions = snapshot.questions, let processes = snapshot.processes,
              Set(shared.map(\.id)).count == shared.count,
              Set(questions.map(\.id)).count == questions.count,
              Set(processes.map(\.id)).count == processes.count else { throw malformed() }
        for (index, process) in processes.enumerated() {
            try requiredNullable(raw["processes"]!.array[index].object, keys: ["receipt", "block_reason", "form_version"])
            guard !process.id.isEmpty, !process.taskID.isEmpty, !process.jobKey.isEmpty, process.revision > 0,
                  process.snapshot["company_name"]?.text != nil, process.snapshot["title"]?.text != nil,
                  process.submissionStatus != "submitted" || process.receipt != nil else { throw malformed() }
            if let receipt = process.receipt, receipt.safeURL == nil { throw malformed() }
        }
        for (index, question) in shared.enumerated() {
            try requiredNullable(raw["shared_questions"]!.array[index].object, keys: ["answer", "answered_at"])
            guard !question.id.isEmpty, !question.text.isEmpty, !question.scope.kind.isEmpty,
                  ["open", "answered"].contains(question.status), question.answerRevision >= 0,
                  Set(question.bindingIDs).count == question.bindingIDs.count else { throw malformed() }
            try validateSyncValue(type: question.type, choices: question.choices, required: question.required, answer: question.answer)
            if question.hasAcceptedAnswer {
                guard question.answer != nil, question.answer != .null, question.answerRevision > 0,
                      question.answeredAt.flatMap(WorkflowHealth.timestamp) != nil else { throw malformed() }
            } else {
                guard (question.answer == nil || question.answer == .null), question.answerRevision == 0,
                      question.answeredAt == nil else { throw malformed() }
            }
            let bindings = questions.filter { $0.poolQuestionID == question.id }
            guard Set(bindings.map(\.id)) == Set(question.bindingIDs), bindings.allSatisfy({
                $0.poolAnswerRevision == question.answerRevision && $0.hasAcceptedAnswer == question.hasAcceptedAnswer
            }) else { throw malformed() }
        }
        for index in questions.indices {
            let question = questions[index]
            try requiredNullable(raw["questions"]!.array[index].object, keys: ["answer", "answered_at"])
            guard !question.id.isEmpty, !question.processID.isEmpty, !question.formVersion.isEmpty,
                  question.revision > 0, question.poolRevision == snapshot.sync.poolRevision,
                  let poolID = question.poolQuestionID, shared.contains(where: { $0.id == poolID }),
                  processes.contains(where: { $0.id == question.processID }),
                  question.poolAnswerRevision.map({ $0 >= 0 }) == true else { throw malformed() }
            if let stamp = question.answeredAt, WorkflowHealth.timestamp(stamp) == nil { throw malformed() }
            try validateSyncValue(type: question.type, choices: question.choices, required: question.required, answer: question.answer)
            // Provenance is supplied by the coherent envelope, never trusted from a form row.
            questions[index].snapshotPoolRevision = snapshot.sync.poolRevision
        }
        snapshot.questions = questions
        return try envelope(snapshot, object: object)
    }

    private func validateSyncValue(type: String, choices: [WorkflowChoice], required: Bool, answer: JSONValue?) throws {
        guard Set(choices.map(\.id)).count == choices.count, choices.allSatisfy({ !$0.id.isEmpty }) else { throw malformed() }
        if type == "text", !choices.isEmpty { throw malformed() }
        if ["single_choice", "multi_choice"].contains(type), !(1...100).contains(choices.count) { throw malformed() }
        guard let answer, answer != .null else { return }
        switch (type, answer) {
        case ("text", .string(let value)):
            guard value.unicodeScalars.count <= 20_000, !required || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw malformed() }
        case ("single_choice", .string(let id)):
            guard choices.contains(where: { $0.id == id }) else { throw malformed() }
        case ("multi_choice", .array(let values)):
            let ids = values.compactMap(\.text)
            guard ids.count == values.count, Set(ids).count == ids.count,
                  !required || !ids.isEmpty, Set(ids).isSubset(of: Set(choices.map(\.id))) else { throw malformed() }
        default:
            if ["text", "single_choice", "multi_choice"].contains(type) { throw malformed() }
        }
    }

    public func fetchInstructions(taskID: String) async throws -> WorkflowRead<WorkflowInstructionSnapshot> {
        guard validWorkflowID(taskID) else { throw ReviewError.invalid("Choose a saved task before reading instructions.") }
        var req = request(path: "api/v1/unblock-instructions")
        var components = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "task_id", value: taskID)]
        components.percentEncodedQuery = components.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        req.url = components.url
        let object = try readObject(await transport.send(req))
        let snapshot = try decode(WorkflowInstructionSnapshot.self, object: .object(object))
        let target = snapshot.target
        try requiredNullable(object["target"]?.object ?? [:], keys: ["process_id", "process_revision", "block_reason", "unavailable_reason"])
        guard target.taskID == taskID, !target.taskStatus.isEmpty, !target.mode.isEmpty,
              WorkflowHealth.timestamp(target.taskUpdatedAt) != nil,
              validInstructionProcess(target.processID, revision: target.processRevision),
              Set(snapshot.instructions.map(\.id)).count == snapshot.instructions.count,
              Set(snapshot.instructions.map(\.requestID)).count == snapshot.instructions.count else { throw malformed() }
        for (index, instruction) in snapshot.instructions.enumerated() {
            try requiredNullable(object["instructions"]!.array[index].object, keys: ["process_id", "process_revision"])
            guard validWorkflowID(instruction.id), validWorkflowID(instruction.requestID), instruction.taskID == taskID,
                  validInstructionProcess(instruction.processID, revision: instruction.processRevision),
                  WorkflowInstructionRequest.validText(instruction.text),
                  WorkflowHealth.timestamp(instruction.taskUpdatedAt) != nil,
                  WorkflowHealth.timestamp(instruction.createdAt) != nil,
                  !instruction.mode.isEmpty, !instruction.wakeStatus.isEmpty, !instruction.status.isEmpty else { throw malformed() }
        }
        return try envelope(snapshot, object: object)
    }

    private func validInstructionProcess(_ id: String?, revision: Int?) -> Bool {
        (id == nil && revision == nil) || (id.map { validWorkflowID($0) } == true && (revision ?? 0) > 0)
    }

    public func send(_ pending: PendingSave, sessionToken: String) async throws -> WorkflowRead<WorkflowAcknowledgement> {
        guard pending.origin == address.origin,
              ["api/v1/decisions", "api/v1/answers", "api/v1/tasks/cancel", "api/v1/unblock-instructions"].contains(pending.path),
              !sessionToken.isEmpty,
              let command = try? JSONDecoder().decode([String: JSONValue].self, from: pending.body),
              let requestID = command["request_id"]?.text, validWorkflowID(requestID) else {
            throw ReviewError.invalid("The saved workflow request is invalid or belongs to another server. Keep it for recovery.")
        }
        var req = request(path: pending.path)
        req.httpMethod = "POST"; req.httpBody = pending.body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(address.origin, forHTTPHeaderField: "Origin")
        req.setValue(sessionToken, forHTTPHeaderField: "X-Review-Token")
        let response = try await transport.send(req)
        try checkStatus(response)
        guard response.status == 200 else { throw malformed() }
        let object = try JSONDecoder().decode([String: JSONValue].self, from: response.body)
        let ack = try JSONDecoder().decode(WorkflowAcknowledgement.self, from: response.body)
        guard ack.schemaVersion == 1, ack.accepted, ack.requestID == requestID,
              let token = object["token"]?.text, !token.isEmpty,
              Set(ack.processIDs).count == ack.processIDs.count, Set(ack.taskIDs).count == ack.taskIDs.count,
              (ack.processIDs + ack.taskIDs).allSatisfy({ !$0.isEmpty }) else { throw malformed() }
        switch pending.path {
        case "api/v1/decisions":
            guard let action = command["action"]?.text, let cardID = command["card_id"]?.text,
                  ack.action == action, ack.cardID == cardID, ack.decisionID == requestID else { throw malformed() }
            if action == "apply" {
                let count = command["selected"]?.array.count ?? 0
                guard count > 0, ack.processIDs.count == count, ack.taskIDs.count == count else { throw malformed() }
            } else if !ack.processIDs.isEmpty { throw malformed() }
        case "api/v1/answers":
            guard let processID = command["process_id"]?.text, ack.processIDs == [processID], ack.taskIDs.count == 1 else { throw malformed() }
        case "api/v1/unblock-instructions":
            let expectedProcesses = command["process_id"]?.text.map { [$0] } ?? []
            guard let taskID = command["task_id"]?.text, ack.taskIDs == [taskID],
                  command["process_id"] != nil,
                  ack.processIDs == expectedProcesses,
                  ack.instructionID.map({ validWorkflowID($0) }) == true,
                  ack.mode.map({ WorkflowInstructionTarget.supportedModes.contains($0) }) == true,
                  ["saved_only", "recheck_scheduled", "retry_scheduled"].contains(ack.wakeStatus ?? ""),
                  ack.wakeStatus != "recheck_scheduled" || ack.mode == "browser_recheck",
                  ack.wakeStatus != "retry_scheduled" || ack.mode == "preparation_retry",
                  ack.taskUpdatedAt.flatMap(WorkflowHealth.timestamp) != nil else { throw malformed() }
        default:
            guard let taskID = command["task_id"]?.text, ack.processIDs.isEmpty, ack.taskIDs == [taskID] else { throw malformed() }
        }
        return WorkflowRead(value: ack, revision: "", token: token)
    }

    private func request(path: String) -> URLRequest {
        var result = URLRequest(url: address.url.appendingPathComponent(path), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        result.setValue("application/json", forHTTPHeaderField: "Accept")
        result.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let accessToken, !accessToken.isEmpty { result.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization") }
        return result
    }
    private func checkStatus(_ response: HTTPResult) throws {
        guard !(200...299).contains(response.status) else { return }
        let object = (try? JSONDecoder().decode([String: JSONValue].self, from: response.body)) ?? [:]
        let error = object["schema_version"] == .number(1) ? object["error"]?.object ?? [:] : [:]
        throw WorkflowHTTPError(status: response.status, code: error["code"]?.text, reason: error["reason"]?.text,
                                commitStatus: error["commit_status"]?.text,
                                message: error["message"]?.text ?? "The server returned HTTP \(response.status). Your pending request is kept unless its rejection is confirmed.")
    }
    private func readObject(_ response: HTTPResult) throws -> [String: JSONValue] {
        try checkStatus(response)
        guard response.status == 200 else { throw malformed() }
        let object = try JSONDecoder().decode([String: JSONValue].self, from: response.body)
        guard object["schema_version"] == .number(1), object["complete"] == .bool(true),
              object["next_cursor"] == .null,
              object["revision"]?.text?.isEmpty == false, object["token"]?.text?.isEmpty == false else { throw malformed() }
        return object
    }
    private func envelope<T: Sendable>(_ value: T, object: [String: JSONValue]) throws -> WorkflowRead<T> {
        guard let revision = object["revision"]?.text, let token = object["token"]?.text else { throw malformed() }
        return WorkflowRead(value: value, revision: revision, token: token)
    }
    private func decode<T: Decodable>(_ type: T.Type, object: JSONValue?) throws -> T {
        guard let object else { throw malformed() }
        return try JSONDecoder().decode(type, from: JSONEncoder().encode(object))
    }
    private func validate(_ capabilities: WorkflowCapabilities) throws {
        guard capabilities.apiVersion == 1 else { throw ReviewError.invalid("This server uses an unsupported workflow API version.") }
    }
    private func requiredNullable(_ object: [String: JSONValue], keys: [String]) throws {
        guard keys.allSatisfy({ object[$0] != nil }) else { throw malformed() }
    }
    private func timestampFields(_ object: [String: JSONValue], names: [String]) throws {
        guard names.allSatisfy({ object[$0]?.text != nil }) else { throw malformed() }
    }
    private func malformed() -> ReviewError { .uncertain("The workflow response is incomplete, unsupported, or does not match the saved request.") }
}
