import AppKit
import Combine
import MacDashboardCore
import ReviewCore

extension WorkflowCapabilities {
    var workerConfigurationTitle: String {
        if workerStatus == "paused" { return "Paused" }
        return workerEnabled ? "Enabled" : "Not configured"
    }
}

extension String {
    var readableStatus: String {
        ["waiting_for_user": "Needs attention", "retry_wait": "Waiting to retry", "not_configured": "Not configured",
         "on_demand": "When needed", "not_started": "Not started", "submission_unknown": "Submission unconfirmed",
         "awaiting_response": "Awaiting response", "succeeded": "Complete",
         "questions_pending": "Answers needed", "browser_work_uncertain": "Browser work needs review",
         "waiting_for_browser": "Waiting for Chrome",
         "ranked_output_pending": "Saving ranked results", "application_action_required": "Application needs review",
         "posting_changed": "Posting changed", "prior_application_found": "Earlier application found",
         "worker_lease_lost": "Worker connection lost"][self]
        ?? replacingOccurrences(of: "_", with: " ").capitalized
    }
    /// Public diagnostics are plain text. Never show recognizable credentials in the UI.
    var safeDiagnostic: String {
        var result = self
        for pattern in ["(?i)Bearer\\s+[A-Za-z0-9._~+/-]+=*", "[A-Za-z0-9_-]{43,}"] {
            if let regex = try? NSRegularExpression(pattern: pattern) {
                result = regex.stringByReplacingMatches(in: result, range: NSRange(result.startIndex..., in: result), withTemplate: "[redacted]")
            }
        }
        return String(result.prefix(4000))
    }
}

func timestamp(_ text: String) -> Date? {
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let value = format.date(from: text) { return value }
    format.formatOptions = [.withInternetDateTime]
    return format.date(from: text)
}

enum DashboardPage: String, CaseIterable, Identifiable {
    case tasks = "Board", overview = "Overview", feed = "Browse", questions = "Questions", activity = "Activity", applicationWorkflow = "Workflow", connection = "Connection"
    var id: String { rawValue }
    var icon: String {
        switch self { case .overview: return "rectangle.3.group"; case .tasks: return "rectangle.split.3x1"; case .feed: return "rectangle.stack"; case .questions: return "questionmark.bubble"; case .activity: return "clock"; case .applicationWorkflow: return "point.3.connected.trianglepath.dotted"; case .connection: return "network" }
    }
}

enum ConnectionPhase: Equatable {
    case loading, connected, stale, disconnected
    var title: String {
        switch self { case .loading: return "Connecting"; case .connected: return "Connected"; case .stale: return "Updates paused"; case .disconnected: return "Disconnected" }
    }
}

struct ActivityItem: Identifiable {
    let id: String
    let title: String
    let detail: String
    let date: String
    let status: String
    let taskID: String
}

@MainActor final class DashboardModel: ObservableObject {
    @Published var snapshot: DashboardSnapshot?
    @Published var settings: ConnectionSettings?
    @Published var errorMessage: String?
    @Published var recovery: String?
    @Published var isRefreshing = false
    @Published var page: DashboardPage = .tasks
    @Published var selectedTaskID: String?
    @Published var taskFilter = "All"
    @Published var search = ""
    @Published var now = Date()
    @Published var workflow: WorkflowView?
    @Published var isSaving = false
    @Published var commandError: String?
    @Published var commandMessage: String?
    @Published var selectedCardID: String?
    @Published var browseSearch = ""
    @Published var questionSearch = ""
    @Published var questionsFilter: QuestionsFilter = .pending
    @Published var focusedQuestionID: String?
    @Published var instructionSelection: AgentInstructionSelection?
    let agentInstructions = AgentInstructionsModel()
    var workflowService: (any MacWorkflowServing)?
    let injectedWorkflowService: Bool
    var questionEditors: [String: ApplicationQuestionEditor] = [:]
    var workflowReadAt: Date?
    let demo: Bool
    let projectConsent = ProjectConsentModel()
    let localServer: LocalServerModel
    let applicationWorkflow: ApplicationWorkflowModel
    private var poller: Task<Void, Never>?
    var onChange: (() -> Void)?

    init(demo: Bool = false, workflowService: (any MacWorkflowServing)? = nil, serverService: (any LocalServerServing)? = nil) {
        self.demo = demo
        applicationWorkflow = ApplicationWorkflowModel(fictional: demo)
        self.workflowService = workflowService
        injectedWorkflowService = workflowService != nil
        let scenario = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--scenario=") }?.split(separator: "=").last.map(String.init) ?? "active"
        localServer = LocalServerModel(service: serverService ?? (demo ? DemoLocalServer(scenario: scenario) : LocalServerController()))
        localServer.onChange = { [weak self] in self?.objectWillChange.send(); self?.onChange?() }
    }

    var phase: ConnectionPhase {
        if let workflow {
            if errorMessage != nil || !workflow.online || workflowReadAt == nil || now.timeIntervalSince(workflowReadAt!) > 45 {
                return workflow.savedAt == nil && snapshot == nil ? .disconnected : .stale
            }
            return .connected
        }
        if let snapshot {
            return errorMessage != nil || now.timeIntervalSince(snapshot.fetchedAt) > 45 ? .stale : .connected
        }
        return errorMessage == nil ? .loading : .disconnected
    }
    var tasks: [WorkflowTask] { workflow?.tasks ?? snapshot?.tasks ?? [] }
    var processes: [WorkflowProcess] { workflow?.processes ?? snapshot?.processes ?? [] }
    var questions: [WorkflowQuestion] { workflow?.questions ?? snapshot?.questions ?? [] }
    var running: [WorkflowTask] { tasks.filter { $0.status == "running" } }
    var queued: [WorkflowTask] { tasks.filter { ["queued", "retry_wait"].contains($0.status) } }
    var attention: [WorkflowTask] { tasks.filter(Self.needsAttention) }
    static func needsAttention(_ task: WorkflowTask) -> Bool {
        ["waiting_for_user", "failed"].contains(task.status) || task.resourceQuarantined
    }
    var unansweredQuestions: [WorkflowQuestion] {
        return questions.filter { question in
            question.answer == nil && processes.contains { $0.id == question.processID && $0.formVersion == question.formVersion && $0.taskStatus == "waiting_for_user" }
        }
    }
    var menuTitle: String {
        if demo { return "JR Demo" }
        switch phase {
        case .loading: return "JR …"
        case .stale: return "JR Stale"
        case .disconnected: return "JR Offline"
        case .connected:
            if !running.isEmpty { return "JR \(running.count) active" }
            if !attention.isEmpty { return "JR \(attention.count) issues" }
            return "JR Idle"
        }
    }
    var filteredTasks: [WorkflowTask] {
        tasks.filter { task in
            let matchesFilter = taskFilter == "All" || TaskBoardColumn.column(for: task).filterName == taskFilter
            return matchesFilter && (search.isEmpty || "\(taskTitle(task)) \(taskSubtitle(task)) \(task.kind) \(task.id) \(task.status)".localizedCaseInsensitiveContains(search))
        }.sorted { $0.updatedAt == $1.updatedAt ? $0.id < $1.id : $0.updatedAt > $1.updatedAt }
    }
    var activity: [ActivityItem] {
        let events = processes.flatMap { process in
            process.events.map { event in
                ActivityItem(id: "\(process.id)-\(event.id)", title: event.type.readableStatus,
                             detail: "\(process.companyName) · \(process.title)", date: event.createdAt, status: event.type, taskID: process.taskID)
            }
        }
        let discovery = tasks.filter { $0.kind != "apply" }.map {
            ActivityItem(id: $0.id, title: "\($0.kind.readableStatus) · \($0.status.readableStatus)", detail: "Latest task update", date: $0.updatedAt, status: $0.status, taskID: $0.id)
        }
        return (events + discovery).sorted { $0.date > $1.date }
    }
    func process(for task: WorkflowTask) -> WorkflowProcess? { processes.first { $0.taskID == task.id } }
    func acceptedAnswersBlockerMessage(process: WorkflowProcess, questions: [WorkflowQuestion]) -> String? {
        let task = tasks.first { $0.id == process.taskID }
        guard ["waiting_for_user", "failed"].contains(process.taskStatus) || task?.resourceQuarantined == true,
              !questions.isEmpty, questions.allSatisfy({
            $0.processID == process.id && $0.formVersion == process.formVersion && $0.hasAcceptedAnswer
        }), let reason = (process.blockReason ?? task?.error)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !reason.isEmpty, reason != "questions_pending" else { return nil }
        let issue = reason.contains(" ") ? reason.safeDiagnostic : reason.safeDiagnostic.readableStatus
        return "All answers for this form are accepted. Remaining issue: \(issue)\nSee Task details for the next step."
    }
    func taskTitle(_ task: WorkflowTask) -> String {
        if let process = process(for: task) { return process.companyName }
        return ["explore": "Explore job sources", "rank": "Rank saved jobs", "reconcile": "Reconcile saved work"][task.kind] ?? "Application task"
    }
    func taskSubtitle(_ task: WorkflowTask) -> String { process(for: task)?.title ?? (task.kind == "rank" ? "Uses saved postings and profile evidence" : "Discovery pipeline") }
    func showTask(_ id: String) {
        search = ""; taskFilter = "All"; page = .tasks; selectedTaskID = id; focusedQuestionID = nil
    }
    func start() {
        guard poller == nil else { return }
        localServer.beginMonitoring()
        poller = Task { [weak self] in
            guard let self else { return }
            if self.demo {
                self.loadDemo()
                if let scenario = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--scenario=canonical-") }) {
                    do { try await self.loadQuestionSyncDemo(String(scenario.dropFirst("--scenario=".count))) }
                    catch { self.errorMessage = "The canonical question demo could not be loaded: \(error.localizedDescription)" }
                }
            } else { await self.refresh() }
            var seconds = 0
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { break }
                self.now = Date(); self.onChange?(); seconds += 1
                let questionsVisible = self.page == .questions || (self.page == .tasks && self.selectedTaskID.flatMap { id in self.processes.first { $0.taskID == id } }.map { process in self.questions.contains { $0.processID == process.id } } == true)
                if seconds % (questionsVisible ? 5 : 15) == 0 {
                    if !self.demo { await self.refresh() }
                }
            }
        }
    }
    func stop() { poller?.cancel(); poller = nil; localServer.endMonitoring() }
    func refresh() async {
        guard !isRefreshing, !isSaving, !demo else { return }
        isRefreshing = true; onChange?()
        defer { isRefreshing = false; now = Date(); onChange?() }
        do {
            if !injectedWorkflowService {
                let nextSettings = try ConnectionSettings.loadDefault()
                try validateConnectionChange(to: nextSettings)
                // Construct first: a bad new connection must not label old data with a new origin.
                let candidate = try MacWorkflowClient.make(settings: nextSettings)
                if settings?.origin != nextSettings.origin {
                    snapshot = nil; workflow = nil; workflowReadAt = nil; selectedCardID = nil
                    instructionSelection = nil; agentInstructions.reset()
                }
                settings = nextSettings
                workflowService = candidate
                let consentClient = try ProjectConsentClient(settings: nextSettings)
                await projectConsent.load(client: consentClient, origin: nextSettings.origin)
            }
            guard let workflowService else { throw DashboardError.configuration }
            let result = try await workflowService.refresh()
            guard !Task.isCancelled else { return }
            receiveWorkflow(result)
        } catch is CancellationError {
            return
        } catch {
            if let workflowService { receiveWorkflow(await workflowService.view(), successfulRead: false) }
            workflow?.online = false; workflowReadAt = nil
            errorMessage = error.localizedDescription.safeDiagnostic
            recovery = (error as? LocalizedError)?.recoverySuggestion ?? "Check the connection settings and try Refresh."
        }
    }
    func loadDemo() {
        do {
            snapshot = try DemoSnapshot.make()
            let scenario = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--scenario=") }?.split(separator: "=").last.map(String.init) ?? "active"
            projectConsent.loadDemo(scenario)
            applicationWorkflow.loadDemo(scenario)
            selectedCardID = snapshot?.feed.cards.first?.id
            if scenario == "stale" { errorMessage = "The backend did not respond."; recovery = "Check the Mac and Tailscale connection, then refresh." }
            if scenario == "disconnected" { snapshot = nil; errorMessage = "The backend could not be reached."; recovery = "Check the Mac and Tailscale connection, then refresh." }
            if scenario == "loading" { snapshot = nil }
            onChange?()
        } catch { errorMessage = "The demo fixture could not be loaded." }
    }
}
