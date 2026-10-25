import Foundation
import CryptoKit

public struct WorkflowView: Equatable, Sendable {
    public var feed: WorkflowFeed?
    public var processes: [WorkflowProcess]
    public var questions: [WorkflowQuestion]
    public var tasks: [WorkflowTask]
    public var capabilities: WorkflowCapabilities?
    public var health: WorkflowHealth?
    public var pending: PendingSave?
    public var lastAcknowledgement: WorkflowAcknowledgement?
    public var savedAt: Date?
    public var online: Bool
    /// Independent endpoint failures; successful endpoints still replace their cached snapshot.
    public var readErrors: [String: String]
    public var questionSync: WorkflowQuestionSync? = nil
    public var questionSyncReadAt: Date? = nil
    /// Revision of the retained complete snapshot, even while a newer sync is in progress.
    public var questionSyncPoolRevision: Int? = nil
    public var sharedQuestions: [WorkflowSharedQuestion] = []
}

public struct SavedWorkflowSession: Codable, Sendable {
    public var schemaVersion = 1
    public var origin: String
    public var feed: WorkflowFeed?
    public var processes: [WorkflowProcess]
    public var questions: [WorkflowQuestion]
    public var tasks: [WorkflowTask]
    public var capabilities: WorkflowCapabilities?
    public var health: WorkflowHealth?
    public var pending: PendingSave?
    public var lastAcknowledgement: WorkflowAcknowledgement?
    public var savedAt: Date?
    public var questionSync: WorkflowQuestionSync? = nil
    public var questionSyncReadAt: Date? = nil
    public var questionSyncPoolRevision: Int? = nil
    public var sharedQuestions: [WorkflowSharedQuestion]? = nil
    public init(origin: String, feed: WorkflowFeed? = nil, processes: [WorkflowProcess] = [],
                questions: [WorkflowQuestion] = [], tasks: [WorkflowTask] = [],
                capabilities: WorkflowCapabilities? = nil, health: WorkflowHealth? = nil, pending: PendingSave? = nil,
                lastAcknowledgement: WorkflowAcknowledgement? = nil, savedAt: Date? = nil) {
        self.origin = origin; self.feed = feed; self.processes = processes; self.questions = questions
        self.tasks = tasks; self.capabilities = capabilities; self.health = health; self.pending = pending
        self.lastAcknowledgement = lastAcknowledgement; self.savedAt = savedAt
    }
}

public protocol WorkflowPersistence: Sendable {
    func load() throws -> SavedWorkflowSession?
    func save(_ value: SavedWorkflowSession) throws
}

/// A separate file namespace prevents migration or replacement of legacy pending writes.
public final class WorkflowFilePersistence: WorkflowPersistence, @unchecked Sendable {
    public let file: URL
    public init(directory: URL, origin: String) throws {
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            var folder = directory; try folder.setResourceValues(values)
            let digest = SHA256.hash(data: Data(origin.utf8)).map { String(format: "%02x", $0) }.joined()
            file = directory.appendingPathComponent("workflow-" + digest + ".json")
        } catch { throw ReviewError.storage("Could not create the workflow cache. \(error.localizedDescription)") }
    }
    public func load() throws -> SavedWorkflowSession? {
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        do {
            let result = try JSONDecoder().decode(SavedWorkflowSession.self, from: Data(contentsOf: file))
            guard result.schemaVersion == 1 else { throw ReviewError.storage("The workflow cache uses a newer app version.") }
            return result
        } catch { throw ReviewError.storage("Could not read the saved workflow. Keep this file for recovery. \(error.localizedDescription)") }
    }
    public func save(_ value: SavedWorkflowSession) throws {
        do {
            let data = try JSONEncoder().encode(value)
            #if os(iOS)
            try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            #else
            try data.write(to: file, options: .atomic)
            #endif
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            var url = file; try url.setResourceValues(values)
        } catch { throw ReviewError.storage("Could not save the workflow on this device. \(error.localizedDescription)") }
    }
}

/// One durable command at a time. Reads never infer that a command was accepted.
public actor WorkflowSession {
    private let api: WorkflowAPI
    private let persistence: any WorkflowPersistence
    private var saved: SavedWorkflowSession
    private var token: String?
    private var online = false
    private var busy = false
    private var readErrors: [String: String] = [:]

    public init(api: WorkflowAPI, persistence: any WorkflowPersistence) throws {
        self.api = api; self.persistence = persistence
        let value = try persistence.load() ?? SavedWorkflowSession(origin: api.address.origin)
        guard value.schemaVersion == 1, value.origin == api.address.origin,
              value.pending == nil || value.pending?.origin == api.address.origin else {
            throw ReviewError.storage("The workflow cache belongs to another server. Its pending request was kept.")
        }
        saved = value
    }

    public func view() -> WorkflowView {
        WorkflowView(feed: saved.feed, processes: saved.processes, questions: saved.questions,
                     tasks: saved.tasks, capabilities: saved.capabilities, health: saved.health, pending: saved.pending,
                     lastAcknowledgement: saved.lastAcknowledgement, savedAt: saved.savedAt,
                     online: online, readErrors: readErrors, questionSync: saved.questionSync,
                     questionSyncReadAt: saved.questionSyncReadAt,
                     questionSyncPoolRevision: saved.questionSyncPoolRevision,
                     sharedQuestions: saved.sharedQuestions ?? [])
    }

    @discardableResult public func refresh() async throws -> WorkflowView {
        guard !busy else { throw ReviewError.pending }
        busy = true; defer { busy = false }
        do {
            guard let read = try await api.discoverRead() else {
                guard saved.pending == nil else {
                    throw ReviewError.uncertain("This server no longer advertises the workflow API. The pending request was kept.")
                }
                var next = saved; next.capabilities = nil
                try persist(next); token = nil; online = true; readErrors = [:]
                return view()
            }
            var next = saved; next.capabilities = read.value
            try persist(next); token = read.token; online = true; readErrors.removeValue(forKey: "capabilities")
        } catch {
            online = false; token = nil; readErrors["capabilities"] = error.localizedDescription
            throw error
        }
        await refreshSnapshots()
        return view()
    }

    @discardableResult public func decide(card: WorkflowCard, action: String, selected: Set<String>,
                                         excludeJobKeys: Set<String> = [], note: String = "") async throws -> WorkflowView {
        try requireWrite(endpoints: ["feed"])
        guard saved.feed?.cards.first(where: { $0.id == card.id }) == card else {
            throw ReviewError.conflict("This card changed. Refresh and review its current jobs.")
        }
        guard action != "apply" || saved.capabilities?.explicitApply == true else {
            throw ReviewError.invalid("This server does not support explicit Apply decisions.")
        }
        // The feed is the only complete job inventory available to this client. Do not invent exclusions.
        guard excludeJobKeys.isSubset(of: Set(card.jobs.map(\.key))) else {
            throw ReviewError.invalid("Only jobs on this reviewed card can be excluded by this client.")
        }
        let payload = try WorkflowDecisionRequest(card: card, action: action, selected: selected, excludeJobKeys: excludeJobKeys, note: note)
        let verb = ["apply": "Apply to \(selected.count) job(s) at", "find_more": "Find more jobs at",
                    "exclude": "Exclude", "pass": "Pass \(selected.count) job(s) at", "later": "Save \(selected.count) job(s) for later at"][action] ?? action
        try prepare(PendingSave(origin: api.address.origin, path: "api/v1/decisions", payload: payload,
                                summary: "\(verb) \(card.company.name)"))
        return try await commit()
    }

    @discardableResult public func answer(process: WorkflowProcess, questions: [WorkflowQuestion],
                                         values: [String: JSONValue]) async throws -> WorkflowView {
        try requireWrite(endpoints: ["processes", "questions"])
        guard saved.capabilities?.questions == true, saved.capabilities?.questionScopes.contains("application") == true else {
            throw ReviewError.invalid("This server does not support application questions.")
        }
        guard saved.processes.first(where: { $0.id == process.id }) == process,
              questions.allSatisfy({ question in
                  saved.questions.first(where: { $0.id == question.id })?.hasSameAnswerBinding(as: question) == true
              }) else {
            throw ReviewError.conflict("The application or questions changed. Review the current version before sending answers.")
        }
        var expectedPoolRevision: Int?
        if saved.capabilities?.questionSync == true {
            guard view().questionSyncAllowsAnswers else {
                throw ReviewError.conflict("Waiting for sync… Your draft was kept. Refresh before sending answers.")
            }
            guard let captured = questions.first?.snapshotPoolRevision,
                  questions.allSatisfy({ $0.snapshotPoolRevision == captured }),
                  captured == saved.questionSyncPoolRevision else {
                throw ReviewError.conflict("Shared questions changed. Your draft was kept. Refresh and review before sending.")
            }
            expectedPoolRevision = captured
        }
        let payload = try WorkflowAnswerRequest(process: process, questions: questions, values: values,
                                                expectedPoolRevision: expectedPoolRevision)
        try prepare(PendingSave(origin: api.address.origin, path: "api/v1/answers", payload: payload,
                                summary: "Answer \(values.count) question(s) for \(process.title)"))
        return try await commit()
    }

    @discardableResult public func cancel(task: WorkflowTask) async throws -> WorkflowView {
        try requireWrite(endpoints: ["tasks", "processes"])
        guard saved.tasks.first(where: { $0.id == task.id }) == task else {
            throw ReviewError.conflict("This task changed. Refresh before cancelling it.")
        }
        guard !saved.processes.contains(where: { $0.taskID == task.id && !["not_started", "preparing"].contains($0.submissionStatus) }) else {
            throw ReviewError.invalid("Work with a completed or uncertain submission cannot be cancelled.")
        }
        let payload = try WorkflowCancelRequest(task: task)
        try prepare(PendingSave(origin: api.address.origin, path: "api/v1/tasks/cancel", payload: payload,
                                summary: "Cancel queued work"))
        return try await commit()
    }

    /// History is loaded on demand. A caller may keep the last snapshot visible if a later read fails.
    public func instructions(taskID: String) async throws -> WorkflowInstructionSnapshot {
        guard !busy else { throw ReviewError.pending }
        guard saved.capabilities?.unblockInstructions == true else {
            throw ReviewError.invalid("This server does not support instructions for blocked tasks yet.")
        }
        busy = true; defer { busy = false }
        let read = try await api.fetchInstructions(taskID: taskID)
        token = read.token
        return read.value
    }

    @discardableResult public func sendInstructions(target: WorkflowInstructionTarget, text: String) async throws -> WorkflowView {
        try requireWrite(endpoints: [])
        guard saved.capabilities?.unblockInstructions == true else {
            throw ReviewError.invalid("This server does not support instructions for blocked tasks yet.")
        }
        // The read is fresh, but its revisions must still match the context the user reviewed.
        // Never silently retarget a draft to a changed task or a new application revision.
        let current = try await instructions(taskID: target.taskID)
        guard current.target == target else {
            throw ReviewError.conflict("This task changed. Refresh its instructions and review the current blocker before sending again. Your draft is kept.")
        }
        try requireWrite(endpoints: [])
        let payload = try WorkflowInstructionRequest(target: target, text: text)
        try prepare(PendingSave(origin: api.address.origin, path: "api/v1/unblock-instructions", payload: payload,
                                summary: "Instructions for task \(target.taskID)"))
        return try await commit()
    }

    /// Credentials are refreshed, but the original path, request ID, revisions, arrays, and bytes stay unchanged.
    @discardableResult public func retryPending() async throws -> WorkflowView {
        guard !busy, saved.pending != nil else { throw ReviewError.invalid("There is no pending workflow save to check.") }
        busy = true
        do {
            guard let read = try await api.discoverRead() else {
                throw ReviewError.uncertain("The workflow API is unavailable. The original request was kept.")
            }
            token = read.token; online = true
        } catch {
            busy = false; online = false; token = nil
            throw error
        }
        busy = false
        return try await commit()
    }

    private func requireWrite(endpoints: [String]) throws {
        guard !busy, saved.pending == nil else { throw ReviewError.pending }
        guard online, token != nil, saved.capabilities != nil,
              endpoints.allSatisfy({ readErrors[$0] == nil }) else { throw ReviewError.offline }
    }
    private func prepare(_ pending: PendingSave) throws {
        guard (1...1_000_000).contains(pending.body.count) else {
            throw ReviewError.invalid("This request exceeds the server's size limit. Shorten the answers or note before sending.")
        }
        var next = saved; next.pending = pending
        try persist(next) // No request is sent if the durable save fails.
    }
    private func persist(_ next: SavedWorkflowSession) throws {
        try persistence.save(next); saved = next
    }
    private func commit() async throws -> WorkflowView {
        guard !busy, let pending = saved.pending, let token else { throw ReviewError.offline }
        busy = true; defer { busy = false }
        do {
            let read = try await api.send(pending, sessionToken: token)
            // Answers must acknowledge the same existing task, not a newly invented task.
            if pending.path == "api/v1/answers", let processID = read.value.processIDs.first,
               let process = saved.processes.first(where: { $0.id == processID }), read.value.taskIDs != [process.taskID] {
                throw ReviewError.uncertain("The acknowledgement names a different task. The original request was kept.")
            }
            var next = saved; next.pending = nil; next.lastAcknowledgement = read.value; next.savedAt = Date()
            // Never show a consumed card as available when the follow-up feed read fails.
            if pending.path == "api/v1/decisions" { next.feed = nil }
            try persist(next)
            self.token = read.token; online = true
        } catch let error as WorkflowHTTPError {
            if error.isDefinitiveRejection {
                var next = saved; next.pending = nil
                try persist(next)
                // A new intent must use the refreshed card/process/questions and a new request ID.
                // Read failures retain their own errors and keep those actions unavailable.
                await refreshSnapshots()
                throw error
            }
            online = false; self.token = nil
            throw error
        } catch {
            online = false; self.token = nil
            throw error
        }
        // A read failure after this point cannot restore the pending command or resend it.
        await refreshSnapshots()
        return view()
    }

    private func refreshSnapshots() async {
        var succeeded = false
        do {
            let read = try await api.fetchFeed()
            var next = saved
            if saved.pending?.path != "api/v1/decisions" { next.feed = read.value }
            next.capabilities = read.value.capabilities; next.savedAt = Date()
            try persist(next); token = read.token; succeeded = true; readErrors.removeValue(forKey: "feed")
        } catch { readErrors["feed"] = error.localizedDescription }
        if saved.capabilities?.questionSync == true {
            do {
                let read = try await api.fetchQuestionSync()
                let incoming = read.value
                guard incoming.sync.revision >= (saved.questionSync?.revision ?? 0),
                      incoming.sync.poolRevision >= (saved.questionSync?.poolRevision ?? 0) else {
                    throw ReviewError.conflict("An older shared question snapshot was received. Your current snapshot and drafts were kept.")
                }
                var next = saved
                next.questionSync = incoming.sync; next.questionSyncReadAt = Date()
                if incoming.sync.state == "ready", let shared = incoming.sharedQuestions,
                   let questions = incoming.questions, let processes = incoming.processes {
                    next.sharedQuestions = shared; next.questions = questions; next.processes = processes
                    next.questionSyncPoolRevision = incoming.sync.poolRevision; next.savedAt = Date()
                }
                try persist(next); token = read.token; succeeded = true
                readErrors.removeValue(forKey: "question_sync")
                readErrors.removeValue(forKey: "processes"); readErrors.removeValue(forKey: "questions")
            } catch { readErrors["question_sync"] = error.localizedDescription }
        } else {
            readErrors.removeValue(forKey: "question_sync")
            do {
                let read = try await api.fetchProcesses()
                var next = saved; next.processes = read.value; next.savedAt = Date()
                try persist(next); token = read.token; succeeded = true; readErrors.removeValue(forKey: "processes")
            } catch { readErrors["processes"] = error.localizedDescription }
            do {
                let read = try await api.fetchQuestions()
                var next = saved; next.questions = read.value; next.savedAt = Date()
                try persist(next); token = read.token; succeeded = true; readErrors.removeValue(forKey: "questions")
            } catch { readErrors["questions"] = error.localizedDescription }
        }
        do {
            let read = try await api.fetchTasks()
            var next = saved; next.tasks = read.value; next.savedAt = Date()
            try persist(next); token = read.token; succeeded = true; readErrors.removeValue(forKey: "tasks")
        } catch { readErrors["tasks"] = error.localizedDescription }
        do {
            let read = try await api.fetchHealth()
            var next = saved; next.health = read?.value
            try persist(next)
            if let read { token = read.token }
            readErrors.removeValue(forKey: "health")
        } catch { readErrors["health"] = error.localizedDescription }
        // Readiness is diagnostic. It cannot make stale decision/process snapshots writable,
        // and a failed readiness read cannot disable independently refreshed workflow data.
        online = succeeded
        if !succeeded { token = nil }
    }
}
