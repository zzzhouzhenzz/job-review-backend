import Foundation
import SwiftUI
import ReviewCore

@MainActor
final class AppModel: ObservableObject {
    @Published var state: ReviewState?
    @Published var workflow: WorkflowView?
    @Published private(set) var usesWorkflow = false
    @Published var savedAt: Date?
    @Published var pending: PendingSave?
    @Published var online = false
    @Published var busy = false
    @Published var error: String?
    @Published var notice: String?
    @Published var legacyReadError: String?
    @Published var demo = true
    @Published var serverText = "http://localhost:8766"
    @Published private(set) var connectedOrigin: String?
    private var session: ReviewSession?
    private var workflowSession: WorkflowSession?
    private var connectionVerified = false
    private var legacyOnline = false
    private var started = false
    private let preferences = UserDefaults.standard
    var canSave: Bool { connectionVerified && online && !busy && pending == nil }
    var canSaveLegacy: Bool { connectionVerified && legacyOnline && !busy && pending == nil }
    var ready: [Job] { state?.readyJobs ?? [] }

    func start() async {
        guard !started else { return }; started = true
        demo = preferences.object(forKey: "useDemo") == nil ? true : preferences.bool(forKey: "useDemo")
        serverText = preferences.string(forKey: "serverAddress") ?? "http://localhost:8766"
        await connect(demo: demo, address: serverText, accessToken: nil)
    }

    @discardableResult
    func connect(demo useDemo: Bool, address: String, accessToken: String?) async -> Bool {
        guard !busy else { return false }
        busy = true; defer { busy = false }
        do {
            let server = try ServerAddress(useDemo ? DemoData.origin : address)
            guard pending == nil || server.origin == connectedOrigin else {
                throw ReviewError.invalid("Check the pending save before changing servers. You can update credentials for the current server.")
            }
            let storage = try FilePersistence(directory: dataDirectory, origin: server.origin)
            let transport: any ReviewTransport
            if useDemo { transport = DemoTransport(state: try storage.load()?.state ?? DemoData.state()) }
            else { transport = URLSessionTransport() }
            if !useDemo, let accessToken { try SecureTokenStore.save(accessToken, for: server.origin) }
            let credential = useDemo ? nil : try SecureTokenStore.read(for: server.origin)
            let next = try ReviewSession(api: ReviewAPI(address: server, transport: transport, accessToken: credential), persistence: storage)
            let nextWorkflow = useDemo ? nil : try WorkflowSession(
                api: WorkflowAPI(address: server, transport: transport, accessToken: credential),
                persistence: WorkflowFilePersistence(directory: dataDirectory, origin: server.origin))
            // Install both caches together. A failed cache read cannot hide an unresolved command.
            session = next; workflowSession = nextWorkflow
            demo = useDemo; serverText = address; connectedOrigin = server.origin
            connectionVerified = false; legacyOnline = false
            let cached = await nextWorkflow?.view()
            usesWorkflow = cached?.capabilities != nil || cached?.pending != nil
            workflow = cached
            preferences.set(useDemo, forKey: "useDemo"); preferences.set(address, forKey: "serverAddress")
            error = nil; notice = nil; legacyReadError = nil
            await update()
            do { try await refreshConnection() }
            catch { self.error = error.localizedDescription; await update(); return false }
            await update()
            return true
        } catch { self.error = error.localizedDescription; return false }
    }

    func refresh() async { await refresh(silent: false) }
    private func refresh(silent: Bool) async {
        guard !busy, session != nil else { return }
        busy = true; defer { busy = false }
        do { try await refreshConnection(); if !silent { error = nil } }
        catch { self.error = error.localizedDescription }
        await update()
    }

    /// The root view cancels this task when the scene leaves the foreground.
    func pollWhileActive() async {
        while !Task.isCancelled {
            await refresh(silent: true)
            // Keep shared question answers current while an application needs input.
            // The server still fences writes against changes between these polls.
            let needsQuestionRefresh = QuestionDraftSync.shouldPollQuickly(workflow)
            do { try await Task.sleep(for: .seconds(needsQuestionRefresh ? 5 : 20)) }
            catch { return }
        }
    }

    private func refreshConnection() async throws {
        guard let session else { return }
        let legacy = await session.view()
        if demo || legacy.pending != nil {
            // Finish the original legacy command before discovering a different write protocol.
            usesWorkflow = false
            connectionVerified = demo
            try await session.refresh()
            return
        }
        guard let workflowSession else { return }
        do {
            let next = try await workflowSession.refresh()
            usesWorkflow = next.capabilities != nil
            connectionVerified = true
        } catch {
            connectionVerified = false
            throw error // Authentication, malformed data, and network errors never select legacy mode.
        }
        if usesWorkflow {
            await refreshLegacyHistory()
        } else {
            // WorkflowSession returns nil capabilities only after a discovery 404 with no pending v1 command.
            try await session.refresh()
            legacyReadError = nil
        }
    }

    private func refreshLegacyHistory() async {
        do { try await session?.refresh(); legacyReadError = nil }
        catch { legacyReadError = "Saved jobs and the manual queue could not refresh. \(error.localizedDescription)" }
    }

    @discardableResult
    func decide(_ card: FlashCard, action: CardAction, selected: Set<String>) async -> Bool {
        guard !usesWorkflow, canSaveLegacy, let session else { return false }
        busy = true; defer { busy = false }
        var confirmed = false
        do {
            try await session.decide(card: card, action: action, selected: action == .approve ? selected : [])
            error = nil
            notice = action == .approve ? "\(selected.count) \(selected.count == 1 ? "job" : "jobs") saved in Ready." : action == .research ? "Research request saved." : "Company excluded."
            confirmed = true
        } catch { self.error = error.localizedDescription; notice = nil }
        await update()
        return confirmed
    }

    @discardableResult
    func queue(_ keys: Set<String>, expectedVersions: [JobVersion]? = nil) async -> Bool {
        guard canSaveLegacy, let session else { return false }
        busy = true; defer { busy = false }
        var confirmed = false
        do {
            try await session.queue(keys: keys, expectedVersions: expectedVersions)
            error = nil; notice = "Work saved. Return to the chat to start it."
            confirmed = true
        } catch { self.error = error.localizedDescription; notice = nil }
        await update()
        return confirmed
    }

    @discardableResult
    func workflowDecision(card: WorkflowCard, action: String, selected: Set<String>,
                          excludeJobKeys: Set<String> = [], note: String = "") async -> Bool {
        await performWorkflow(notice: action == "apply" ? "Apply authorized. Follow it in Progress." : "Decision saved.") { session in
            try await session.decide(card: card, action: action, selected: selected, excludeJobKeys: excludeJobKeys, note: note)
        }
    }

    @discardableResult
    func submitAnswers(process: WorkflowProcess, questions: [WorkflowQuestion], values: [String: JSONValue]) async -> Bool {
        await performWorkflow(notice: "Answers saved.") { session in
            try await session.answer(process: process, questions: questions, values: values)
        }
    }

    @discardableResult
    func cancelWorkflowTask(_ task: WorkflowTask) async -> Bool {
        await performWorkflow(notice: "Work cancelled. Its history is kept.") { session in
            try await session.cancel(task: task)
        }
    }

    func instructions(taskID: String) async throws -> WorkflowInstructionSnapshot {
        guard usesWorkflow, !busy, let workflowSession else { throw ReviewError.pending }
        busy = true; defer { busy = false }
        return try await workflowSession.instructions(taskID: taskID)
    }

    @discardableResult
    func sendInstructions(target: WorkflowInstructionTarget, text: String) async -> Bool {
        await performWorkflow(notice: "Instructions saved.") { session in
            try await session.sendInstructions(target: target, text: text)
        }
    }

    private func performWorkflow(notice message: String,
                                 operation: (WorkflowSession) async throws -> WorkflowView) async -> Bool {
        guard usesWorkflow, canSave, let workflowSession else { return false }
        busy = true; defer { busy = false }
        var confirmed = false
        do {
            _ = try await operation(workflowSession)
            error = nil; notice = message; confirmed = true
            // Failure here cannot turn an acknowledged command into a retry.
            await refreshLegacyHistory()
        } catch {
            self.error = error.localizedDescription; notice = nil
        }
        await update()
        return confirmed
    }

    func retryPending() async {
        guard !busy, pending != nil else { return }
        busy = true; defer { busy = false }
        do {
            if await session?.view().pending != nil { try await session?.retryPending() }
            else if let workflowSession { _ = try await workflowSession.retryPending() }
            error = nil; notice = "Save confirmed."
            do { try await refreshConnection() }
            catch { self.error = "Save confirmed. Refresh failed: \(error.localizedDescription)" }
        } catch { self.error = error.localizedDescription; notice = nil }
        await update()
    }

    private func update() async {
        let legacy = await session?.view()
        let next = await workflowSession?.view()
        state = legacy?.state
        legacyOnline = legacy?.online ?? false
        workflow = usesWorkflow ? next : nil
        // Even a hidden cache blocks a second command until the first one is recovered.
        pending = legacy?.pending ?? next?.pending
        online = usesWorkflow ? next?.online ?? false : legacyOnline
        savedAt = usesWorkflow ? next?.savedAt : legacy?.savedAt
    }

    private var dataDirectory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("JobReview/Sessions", isDirectory: true)
    }
}
