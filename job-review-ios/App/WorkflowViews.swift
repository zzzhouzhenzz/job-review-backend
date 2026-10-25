import SwiftUI
import ReviewCore

struct WorkflowReadNotice: View {
    @EnvironmentObject private var model: AppModel
    let endpoints: [String]
    private var failures: [(String, String)] {
        endpoints.compactMap { key in model.workflow?.readErrors[key].map { (key, $0) } }
    }
    var body: some View {
        if !failures.isEmpty {
            DisclosureGroup {
                ForEach(failures, id: \.0) { endpoint, message in
                    Text("\(endpoint): \(message)")
                        .font(AppTypography.font(.footnote)).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button { Task { await model.refresh() } } label: {
                    Text("Refresh current data").frame(minHeight: 44)
                }.disabled(model.busy)
            } label: {
                Label("Some data is out of date", systemImage: "exclamationmark.circle")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct WorkflowHealthBanner: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let summary = workflowHealthSummary(model.workflow, online: model.online, at: context.date)
            Label(summary.title, systemImage: summary.symbol)
                .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct WorkflowHealthSection: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        Section("Service checks") {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let summary = workflowHealthSummary(model.workflow, online: model.online, at: context.date)
                VStack(alignment: .leading, spacing: 6) {
                    Label(summary.title, systemImage: summary.symbol).font(AppTypography.font(.headline))
                    Text(summary.detail).font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                }.fixedSize(horizontal: false, vertical: true)
            }
            WorkflowReadNotice(endpoints: ["health"])
            if let health = model.workflow?.health {
                DisclosureGroup("Check results (\(health.checks.count))") {
                    ForEach(Array(health.checks.enumerated()), id: \.offset) { _, check in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(workflowCheckName(check.id)).font(AppTypography.font(.subheadline, weight: .semibold))
                            Text("Last result: \(workflowCheckStatus(check.status))").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                            Text(check.message).font(AppTypography.font(.footnote))
                        }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 4)
                    }
                }
                DisclosureGroup("Check details") {
                    if let date = health.checkedAt { LabeledContent("Checked", value: workflowDate(date)) }
                    if let date = health.expiresAt { LabeledContent("Expires", value: workflowDate(date)) }
                    if let date = health.retryAt { LabeledContent("Next check after", value: workflowDate(date)) }
                    Text("Monitor: \(health.bootID)").textSelection(.enabled)
                }.font(AppTypography.font(.caption))
            }
        }.listRowBackground(Palette.surface)
    }
}

func workflowHealthSummary(_ view: WorkflowView?, online: Bool, at date: Date) -> (title: String, symbol: String, detail: String) {
    if view?.capabilities?.workerEnabled == false {
        return ("Worker off · Work stays queued", "pause.circle", "The worker is not configured. Decisions and answers can still be saved.")
    }
    guard view?.capabilities?.workerEnabled == true, online,
          view?.readErrors["health"] == nil, let health = view?.health else {
        return ("Service status unavailable", "questionmark.circle", "There is no current service check result. Check each task for its status. Saved work does not establish submission.")
    }
    if let storage = health.checks.first(where: { $0.id == "storage" }), storage.status != "passed" {
        return ("Saved data check: \(workflowCheckStatus(storage.status))", "info.circle", "Check the saved data result below. Each task shows its current status or error.")
    }
    if health.status == "idle" || health.status == "on_demand" {
        return ("Browser checks run when needed", "clock", "The worker is configured. Research and application tasks check Chrome when they need it. Ranking can run without Chrome. Check each task for its current status.")
    }
    if health.ready && health.status == "ready" && health.isFresh(at: date) {
        return ("Browser checks passed", "checkmark.circle", "The checks are current. This does not mean a task started or an application was sent. Check each task for its current status.")
    }
    let status = ["checking": "Browser checks running", "blocked": "Browser checks blocked", "not_configured": "Browser not configured",
                  "timed_out": "Browser checks timed out", "stale": "Browser checks expired", "ready": "Browser checks are not current"][health.status]
        ?? "Browser status: \(health.status)"
    return (status, "info.circle", "These checks apply to tasks that need Chrome. Ranking can run without Chrome. Check each task for its current status or error.")
}

private func workflowCheckName(_ value: String) -> String {
    ["storage": "Saved data", "chrome_attachment": "Chrome connection", "linkedin_auth": "LinkedIn sign-in",
     "linkedin_job_read": "LinkedIn job access", "explorer_agent": "Research agent", "ranker_agent": "Ranking agent",
     "application_agent": "Application agent"][value] ?? value
}

func workflowCheckStatus(_ value: String) -> String {
    ["checking": "Checking", "passed": "Passed", "failed": "Failed", "blocked": "Blocked",
     "not_configured": "Not configured", "timed_out": "Timed out", "stale": "Expired",
     "idle": "Idle", "on_demand": "Checked when needed"][value] ?? value
}

struct WorkflowFeedView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var firstCard: WorkflowCard? { model.workflow?.feed?.cards.first }
    private var cardIdentity: String? { firstCard.map { $0.id + ":" + $0.revision } }
    var body: some View {
        VStack(spacing: 0) {
            WorkflowReadNotice(endpoints: ["capabilities", "feed"])
                .padding(.horizontal, 20)
            if let card = firstCard {
                CompanyCardView(workflowCard: card, companiesLeft: model.workflow?.feed?.counts.readyCards ?? 0)
                    .id(cardIdentity)
                    .transition(reduceMotion ? .identity : .opacity)
            } else {
                ContentUnavailableView {
                    Label(model.workflow?.feed == nil ? "Feed unavailable" : "No cards to review", systemImage: "rectangle.stack")
                } description: {
                    Text(model.workflow?.capabilities?.workerEnabled == false
                         ? "The worker is not configured. Research tasks stay queued until a worker is available."
                         : "New cards appear after research is saved. Refresh to check the current feed.")
                } actions: {
                    Button("Refresh feed") { Task { await model.refresh() } }.disabled(model.busy)
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.22), value: cardIdentity)
    }
}

/// An acknowledged save can invalidate the reviewed snapshot before its sheet dismisses.
@MainActor
final class WorkflowConfirmationSave: ObservableObject {
    @Published private(set) var isSaving = false

    func showsStale(isCurrent: Bool, hasPending: Bool) -> Bool {
        !isSaving && !isCurrent && !hasPending
    }

    func run(_ operation: () async -> Bool, onConfirmed: () -> Void) async {
        guard !isSaving else { return }
        isSaving = true
        if await operation() {
            // Keep stale feedback and repeat taps suppressed through the dismissal animation.
            onConfirmed()
        } else {
            isSaving = false
        }
    }
}

/// Unknown outcomes remain open and use the stored command, never a new intent.
private struct WorkflowSaveFeedback: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        if let pending = model.pending {
            Section("Save needs a check") {
                Text(pending.summary)
                Text("This may already be saved. Check the same request before continuing.")
                    .foregroundStyle(Palette.muted)
                Button { Task { await model.retryPending() } } label: {
                    Text("Check pending save").frame(minHeight: 44)
                }.disabled(model.busy)
            }.listRowBackground(Palette.surface)
        }
        if let error = model.error {
            Section("Save not confirmed") { Text(error).foregroundStyle(Palette.rust).textSelection(.enabled) }
                .listRowBackground(Palette.surface)
        }
    }
}

private struct WorkflowInstructionComposer: View {
    @Binding var text: String
    let canSend: Bool
    let isSending: Bool
    let isLocked: Bool
    let send: () -> Void

    var body: some View {
        Section {
            TextEditor(text: $text)
                .font(AppTypography.font(.body))
                .frame(minHeight: 130)
                .accessibilityLabel("Instructions for agent")
                .accessibilityHint("Describe how the agent can handle this task's blocker.")
                .disabled(isLocked)
            HStack {
                Text("This task only")
                Spacer()
                Text("\(text.unicodeScalars.count) / 4,000")
                    .foregroundStyle(text.unicodeScalars.count > 4_000 ? Palette.rust : Palette.muted)
            }.font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
            Button(action: send) {
                HStack(spacing: 8) {
                    if isSending { ProgressView() }
                    else { Image(systemName: "paperplane").accessibilityHidden(true) }
                    Text(isSending ? "Sending…" : "Send instructions")
                }.font(AppTypography.font(.subheadline, weight: .semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.borderedProminent).tint(Palette.primary)
            .disabled(!canSend || isLocked || isSending)
        } header: {
            Label("Instructions for agent", systemImage: "text.bubble")
                .foregroundStyle(Palette.brandText)
        } footer: {
            Text("New instructions replace earlier guidance for this task. Application answers and required permissions still use their own controls. Instructions do not authorize another submission when its outcome is unknown.")
                .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .listRowBackground(Palette.surface)
    }
}

struct WorkflowProgressView: View {
    @EnvironmentObject private var model: AppModel
    @State private var cancelTask: WorkflowTask?
    @State private var instructionTask: WorkflowTask?
    private var processes: [WorkflowProcess] { model.workflow?.processes ?? [] }
    private var tasks: [WorkflowTask] { model.workflow?.tasks ?? [] }
    var body: some View {
        List {
            WorkflowReadNotice(endpoints: ["processes", "tasks"])
                .listRowBackground(Color.clear)
            WorkflowHealthSection()
            if processes.isEmpty {
                ContentUnavailableView {
                    Label("No application progress yet", systemImage: "list.bullet.rectangle")
                } description: {
                    Text("Select jobs and tap Apply in Review to authorize those jobs. Saved and Queue keep the separate manual history.")
                }.listRowBackground(Color.clear)
            }
            ForEach(processes) { process in
                Section {
                    NavigationLink {
                        WorkflowProcessDetail(processID: process.id)
                    } label: {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(process.companyName).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                            Text(process.title).font(AppTypography.font(.headline)).fixedSize(horizontal: false, vertical: true)
                            processStatus(process)
                        }.padding(.vertical, 4)
                    }
                }.listRowBackground(Palette.surface)
            }
            Section("Tasks") {
                if tasks.isEmpty { Text("No tasks saved.").foregroundStyle(Palette.muted) }
                ForEach(tasks) { task in
                    VStack(alignment: .leading, spacing: 8) {
                        if task.kind == "apply", let process = processes.first(where: { $0.taskID == task.id }) {
                            workflowApplicationIdentity(process)
                        } else {
                            Text(workflowLabel(task.kind)).font(AppTypography.font(.headline))
                        }
                        LabeledContent("Task", value: workflowLabel(task.status))
                        if let error = task.error, !error.isEmpty { Text(workflowLabel(error)).font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust) }
                        if task.resourceQuarantined {
                            Text("Resource quarantined. An operator must check the previous worker before work can continue.")
                                .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust)
                        }
                        if canCancel(task) {
                            Button { cancelTask = task } label: { Text("Cancel queued task").frame(minHeight: 44) }
                                .buttonStyle(.borderless).disabled(!model.canSave)
                        }
                        if model.workflow?.capabilities?.unblockInstructions == true, ["apply", "explore"].contains(task.kind) {
                            Button { instructionTask = task } label: {
                                Label(["waiting_for_user", "failed"].contains(task.status) ? "Instructions for agent" : "Instruction history", systemImage: "text.bubble")
                                    .frame(minHeight: 44)
                            }.buttonStyle(.borderless).foregroundStyle(Palette.brandText)
                        }
                        DisclosureGroup("Task details") {
                            Text(task.id).font(AppTypography.font(.caption)).textSelection(.enabled)
                            LabeledContent("Attempts", value: String(task.attempts))
                            LabeledContent("Failures", value: "\(task.failureCount) of \(task.maxAttempts)")
                            LabeledContent("Available", value: workflowDate(task.availableAt))
                        }.font(AppTypography.font(.footnote))
                    }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 6)
                }
            }.listRowBackground(Palette.surface)
        }
        .scrollContentBackground(.hidden).background(Palette.paper)
        .refreshable { await model.refresh() }
        .sheet(item: $cancelTask) { task in WorkflowCancelConfirmation(task: task) }
        .sheet(item: $instructionTask) { task in WorkflowInstructionsView(taskID: task.id) }
    }
    private func canCancel(_ task: WorkflowTask) -> Bool {
        workflowCanCancel(task, in: model.workflow)
    }
}

@ViewBuilder private func processStatus(_ process: WorkflowProcess) -> some View {
    VStack(alignment: .leading, spacing: 5) {
        Text("Submission · \(workflowLabel(process.submissionStatus))")
        Text("Recruiting · \(process.submissionStatus == "submitted" ? workflowLabel(process.recruitingStatus) : "Not yet submitted")")
        Text("Task · \(workflowLabel(process.taskStatus))")
    }.font(AppTypography.font(.subheadline)).foregroundStyle(Palette.muted)
    if process.submissionStatus == "submission_unknown" {
        Text("Submission outcome unknown. An operator must check the evidence. Do not submit again.")
            .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust)
    }
}

private struct WorkflowProcessDetail: View {
    @EnvironmentObject private var model: AppModel
    let processID: String
    @State private var showInstructions = false
    private var process: WorkflowProcess? { model.workflow?.processes.first { $0.id == processID } }
    var body: some View {
        List {
            WorkflowReadNotice(endpoints: ["processes"])
            if let process {
                Section {
                    Text(process.companyName).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                    Text(process.title).font(AppTypography.font(.title2, weight: .semibold))
                    processStatus(process)
                    if let reason = process.blockReason, !reason.isEmpty { Text(workflowLabel(reason)).foregroundStyle(Palette.rust) }
                    if model.workflow?.capabilities?.unblockInstructions == true {
                        Button { showInstructions = true } label: {
                            Label("Instructions for agent", systemImage: "text.bubble").frame(minHeight: 44)
                        }.foregroundStyle(Palette.brandText)
                    }
                    if let url = process.officialURL { Link("Open job posting", destination: url).frame(minHeight: 44) }
                    Text("Updated \(workflowDate(process.updatedAt))").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                }.listRowBackground(Palette.surface)
                if let receipt = process.receipt {
                    Section("Submission receipt") {
                        Text(receipt.evidence).textSelection(.enabled)
                        if !receipt.reference.isEmpty { LabeledContent("Reference", value: receipt.reference) }
                        Text("Observed \(workflowDate(receipt.observedAt))").font(AppTypography.font(.caption))
                        if let url = receipt.safeURL { Link("Open receipt source", destination: url).frame(minHeight: 44) }
                        Text("A submission receipt is not a recruiter response.").font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                    }.listRowBackground(Palette.surface)
                }
                Section("History") {
                    ForEach(process.events) { event in
                        DisclosureGroup {
                            Text("Actor: \(event.actor)").font(AppTypography.font(.caption))
                            Text(workflowJSON(.object(event.data))).font(AppTypography.font(.footnote)).textSelection(.enabled)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(workflowLabel(event.type))
                                Text(workflowDate(event.createdAt)).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                            }.frame(minHeight: 44)
                        }
                    }
                }.listRowBackground(Palette.surface)
                Section("Application record") {
                    LabeledContent("Process revision", value: String(process.revision))
                    Text(process.id).font(AppTypography.font(.caption)).textSelection(.enabled)
                    Text(process.jobKey).font(AppTypography.font(.caption)).textSelection(.enabled)
                }.listRowBackground(Palette.surface)
            } else {
                Text("This application is not in the current snapshot. Refresh to check its status.")
            }
        }.scrollContentBackground(.hidden).background(Palette.paper)
            .navigationTitle("Application").inlineTitle()
            .refreshable { await model.refresh() }
            .sheet(isPresented: $showInstructions) {
                if let process { WorkflowInstructionsView(taskID: process.taskID) }
            }
    }
}

private struct WorkflowInstructionsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let taskID: String
    @State private var snapshot: WorkflowInstructionSnapshot?
    @State private var text = ""
    @State private var loading = false
    @State private var sending = false
    @State private var readError: String?
    @State private var savedNotice: String?
    @State private var pendingRequestID: String?
    @State private var submittedText: String?

    private var process: WorkflowProcess? { model.workflow?.processes.first { $0.taskID == taskID } }
    private var pendingInstruction: WorkflowInstructionRequest? {
        guard let pending = model.pending, pending.path == "api/v1/unblock-instructions",
              let request = try? JSONDecoder().decode(WorkflowInstructionRequest.self, from: pending.body),
              request.taskID == taskID else { return nil }
        return request
    }
    private var canSend: Bool {
        model.canSave && model.workflow?.capabilities?.unblockInstructions == true &&
        snapshot?.target.supportsSubmission == true && readError == nil && !loading &&
        WorkflowInstructionRequest.validText(text)
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let process { workflowApplicationIdentity(process) }
                    else { Text("Research task").font(AppTypography.font(.headline)) }
                    if let target = snapshot?.target {
                        LabeledContent("Task", value: workflowLabel(target.taskStatus))
                        if let reason = target.blockReason { Text(workflowLabel(reason)).foregroundStyle(Palette.rust) }
                        Text(workflowInstructionEffect(mode: target.mode, unavailable: target.unavailableReason))
                            .font(AppTypography.font(.footnote)).foregroundStyle(Palette.detail)
                    } else if loading { ProgressView("Loading current task…") }
                    else { Text("Load the current blocker before sending instructions.").foregroundStyle(Palette.muted) }
                }.listRowBackground(Palette.brandSurface)
                if let readError {
                    Section("Instructions could not refresh") {
                        Text(readError).foregroundStyle(Palette.rust).textSelection(.enabled)
                        Text("Your draft and the last loaded history are kept.").font(AppTypography.font(.footnote))
                        Button("Refresh instructions") { Task { await load() } }.disabled(model.busy || sending)
                    }.listRowBackground(Palette.surface)
                }
                if let savedNotice {
                    Section {
                        Label(savedNotice, systemImage: "checkmark.circle")
                            .foregroundStyle(Palette.fit).fixedSize(horizontal: false, vertical: true)
                    }.listRowBackground(Palette.surface)
                }
                WorkflowSaveFeedback()
                WorkflowInstructionComposer(text: $text, canSend: canSend, isSending: sending,
                                            isLocked: sending || model.pending != nil || snapshot?.target.supportsSubmission != true) {
                    Task { await send() }
                }
                Section("Saved instructions") {
                    if let snapshot {
                        if snapshot.instructions.isEmpty {
                            Text("No instructions saved for this task yet.").foregroundStyle(Palette.muted)
                        }
                        ForEach(snapshot.instructions.reversed()) { instruction in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(alignment: .firstTextBaseline) {
                                    Text(workflowInstructionStatus(instruction.status)).font(AppTypography.font(.caption, weight: .semibold))
                                    Spacer()
                                    Text(workflowDate(instruction.createdAt)).font(AppTypography.font(.caption))
                                }.foregroundStyle(instruction.status == "current" ? Palette.brandText : Palette.muted)
                                Text(verbatim: instruction.text).font(AppTypography.font(.subheadline)).textSelection(.enabled)
                                Text(workflowInstructionReceipt(mode: instruction.mode, wakeStatus: instruction.wakeStatus))
                                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                            }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 5)
                        }
                    } else { Text("History has not loaded.").foregroundStyle(Palette.muted) }
                }.listRowBackground(Palette.surface)
                Section("Task record") {
                    Text(taskID).font(AppTypography.font(.caption)).textSelection(.enabled)
                }.listRowBackground(Palette.surface)
            }
            .scrollContentBackground(.hidden).background(Palette.paper)
            .navigationTitle("Agent instructions").inlineTitle()
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() }.disabled(sending) } }
            .interactiveDismissDisabled(sending)
            .refreshable { await load() }
            .task(id: taskID) {
                capturePending()
                while !Task.isCancelled {
                    if !model.busy && !sending { await load() }
                    do { try await Task.sleep(for: .seconds(snapshot == nil && readError == nil ? 1 : 20)) }
                    catch { return }
                }
            }
            .onChange(of: model.pending?.body) { _, _ in capturePending() }
            .onChange(of: model.workflow?.lastAcknowledgement?.requestID) { _, _ in
                guard let acknowledgement = model.workflow?.lastAcknowledgement,
                      acknowledgement.requestID == pendingRequestID else { return }
                confirm(acknowledgement)
                Task { await loadWhenReady() }
            }
        }
    }

    private func capturePending() {
        guard let pendingInstruction else { return }
        pendingRequestID = pendingInstruction.requestID
        submittedText = pendingInstruction.text
        if text.isEmpty { text = pendingInstruction.text }
    }
    private func confirm(_ acknowledgement: WorkflowAcknowledgement) {
        guard acknowledgement.instructionID != nil, acknowledgement.taskIDs == [taskID] else { return }
        savedNotice = workflowInstructionReceipt(mode: acknowledgement.mode ?? "", wakeStatus: acknowledgement.wakeStatus ?? "")
        if text == submittedText { text = "" }
        pendingRequestID = nil; submittedText = nil
    }
    private func load() async {
        guard !model.busy, !sending else { return }
        loading = true; defer { loading = false }
        do { snapshot = try await model.instructions(taskID: taskID); readError = nil }
        catch { readError = error.localizedDescription }
    }
    private func loadWhenReady() async {
        while model.busy {
            do { try await Task.sleep(for: .milliseconds(100)) } catch { return }
        }
        await load()
    }
    private func send() async {
        guard canSend, !sending, let target = snapshot?.target else { return }
        sending = true; savedNotice = nil; pendingRequestID = nil; submittedText = text
        let confirmed = await model.sendInstructions(target: target, text: text)
        sending = false
        if confirmed, let acknowledgement = model.workflow?.lastAcknowledgement { confirm(acknowledgement) }
        else { capturePending() }
        await load()
    }
}

func workflowInstructionStatus(_ status: String) -> String {
    ["current": "Current guidance", "superseded": "Replaced by newer guidance", "stale": "No longer current"][status] ?? status
}

func workflowInstructionEffect(mode: String, unavailable: String?) -> String {
    if let unavailable {
        return ["task_busy": "The task is active. Instructions can be sent after it stops.",
                "task_not_blocked": "This task is not blocked. Saved instructions remain available below.",
                "task_closed": "This task is closed. You can read its saved instructions.",
                "authorization_changed": "The application authorization changed. Review the application before adding instructions.",
                "stale_revision": "The task changed. Refresh and review the current blocker."][unavailable] ?? "Instructions are unavailable: \(unavailable)"
    }
    return ["browser_recheck": "Sending instructions schedules the next read-only prerequisite check. Progress shows whether work resumes.",
            "preparation_retry": "Sending instructions schedules another preparation attempt for this already approved application. Required answers and permissions still apply.",
            "questions_pending": "Guidance is saved for this task. Send required application answers in Questions.",
            "reconciliation_only": "The submission outcome is unknown. Guidance can help check the existing attempt; it cannot start another submission.",
            "manual_review": "Guidance is saved for manual review. Sending it does not restart this task."][mode] ?? "This instruction mode is not supported: \(mode)"
}

func workflowInstructionReceipt(mode: String, wakeStatus: String) -> String {
    if wakeStatus == "recheck_scheduled" { return "Saved. A prerequisite check is scheduled. Follow the task in Progress." }
    if wakeStatus == "retry_scheduled" { return "Saved. Preparation is scheduled to retry for this approved application. Follow the task in Progress." }
    guard wakeStatus == "saved_only" else { return "Saved. Server status: \(wakeStatus)" }
    return ["questions_pending": "Saved. Required answers still need to be sent in Questions.",
            "reconciliation_only": "Saved for reconciliation. No new submission or retry was started.",
            "manual_review": "Saved for manual review. The task remains stopped."][mode] ?? "Instructions saved. No task restart was requested."
}

private func workflowCanCancel(_ task: WorkflowTask, in view: WorkflowView?) -> Bool {
    guard task.canCancel, view?.readErrors["tasks"] == nil else { return false }
    if task.kind == "apply" {
        guard view?.readErrors["processes"] == nil,
              let process = view?.processes.first(where: { $0.taskID == task.id }),
              ["queued", "retry_wait"].contains(process.taskStatus),
              ["not_started", "preparing"].contains(process.submissionStatus) else { return false }
    }
    return ["explore", "rank", "apply"].contains(task.kind)
}

private struct WorkflowCancelConfirmation: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var save = WorkflowConfirmationSave()
    let task: WorkflowTask
    private var current: WorkflowTask? { model.workflow?.tasks.first { $0.id == task.id } }
    private var canCancel: Bool { current.map { workflowCanCancel($0, in: model.workflow) } ?? false }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Cancel this \(workflowLabel(task.kind).lowercased()) task?").font(AppTypography.font(.headline))
                    if task.kind == "apply", let process = model.workflow?.processes.first(where: { $0.taskID == task.id }) {
                        workflowApplicationIdentity(process)
                    }
                    Text("This keeps the work history. An application cancellation clears its saved approval. It does not restore the old review card.")
                    Text(task.id).font(AppTypography.font(.caption)).textSelection(.enabled)
                }.listRowBackground(Palette.surface)
                WorkflowSaveFeedback()
                if save.showsStale(isCurrent: canCancel, hasPending: model.pending != nil) {
                    Text("This task cannot be cancelled in its current state.").foregroundStyle(Palette.rust)
                }
                Button(role: .destructive) {
                    Task {
                        guard canCancel, let current else { return }
                        await save.run { await model.cancelWorkflowTask(current) } onConfirmed: { dismiss() }
                    }
                } label: { Text("Cancel task").frame(minHeight: 44) }
                    .disabled(save.isSaving || !model.canSave || !canCancel)
            }.scrollContentBackground(.hidden).background(Palette.paper)
                .navigationTitle("Cancel queued work").inlineTitle()
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Keep task") { dismiss() }.frame(minHeight: 44) } }
        }
    }
}

private func workflowApplicationIdentity(_ process: WorkflowProcess) -> some View {
    VStack(alignment: .leading, spacing: 5) {
        Text(process.companyName).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
        Text(process.title).font(AppTypography.font(.headline))
        if let jobID = process.snapshot["job_id"]?.text {
            Text("Job ID: \(jobID)").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
        } else {
            Text("Job key: \(process.jobKey)").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
        }
    }.fixedSize(horizontal: false, vertical: true)
}

struct WorkflowQuestionsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var confirmation: WorkflowAnswerSelection?
    private var usesCanonicalQuestions: Bool { model.workflow?.capabilities?.questionSync == true }
    private var sharedQuestions: [WorkflowSharedQuestion] { model.workflow?.sharedQuestions ?? [] }
    private var questions: [WorkflowQuestion] {
        QuestionDraftSync.displayQuestions(model.workflow?.questions ?? [],
            answerableProcessIDs: Set((model.workflow?.processes ?? []).filter(\.canAnswer).map(\.id)))
    }
    private var processIDs: [String] { Array(Set(questions.map(\.processID))).sorted() }
    var body: some View {
        List {
            WorkflowReadNotice(endpoints: ["capabilities", "question_sync", "processes", "questions"])
                .listRowBackground(Color.clear)
            WorkflowQuestionSyncNotice().listRowBackground(Color.clear)
            if (usesCanonicalQuestions ? sharedQuestions.isEmpty : questions.isEmpty) && model.workflow?.questionSyncAllowsAnswers != false {
                ContentUnavailableView {
                    Label("No questions yet", systemImage: "questionmark.bubble")
                } description: {
                    Text("Questions appear when an application needs your answer. Saved answers sync with your other devices.")
                }.listRowBackground(Color.clear)
            }
            if usesCanonicalQuestions {
                ForEach(sharedQuestions) { shared in
                    if let workflow = model.workflow,
                       let binding = QuestionDraftSync.editorBinding(for: shared, workflow: workflow),
                       let process = workflow.processes.first(where: { $0.id == binding.processID }) {
                        WorkflowQuestionEditor(process: process, questions: [binding], sharedQuestion: shared) { confirmation = $0 }
                    } else {
                        WorkflowSharedQuestionReadOnly(question: shared)
                    }
                }
            } else {
                ForEach(processIDs, id: \.self) { processID in
                    let round = questions.filter { $0.processID == processID }
                    if let process = model.workflow?.processes.first(where: { $0.id == processID }) {
                        WorkflowQuestionEditor(process: process, questions: round) { confirmation = $0 }
                    } else {
                        Section("Application details unavailable") {
                            Text("Refresh application progress before answering these questions.").foregroundStyle(Palette.rust)
                            ForEach(round) { question in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(question.text)
                                    Text("Form \(question.formVersion) · Question revision \(question.revision)").font(AppTypography.font(.caption))
                                }
                            }
                        }.listRowBackground(Palette.surface)
                    }
                }
            }
        }.scrollContentBackground(.hidden).background(Palette.paper)
            .refreshable { await model.refresh() }
            .task { await model.refresh() }
            // The presentation belongs to the screen, not a lazily managed list section.
            .sheet(item: $confirmation) { captured in WorkflowAnswerConfirmation(selection: captured) }
    }
}

private struct WorkflowSharedQuestionReadOnly: View {
    let question: WorkflowSharedQuestion
    var body: some View {
        Section {
            Text(question.text).font(AppTypography.font(.headline)).fixedSize(horizontal: false, vertical: true)
            Text(workflowQuestionScope(question.scope.kind)).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
            if question.hasAcceptedAnswer, let answer = question.answerDisplayText {
                Text("Saved answer: \(answer)").textSelection(.enabled)
                Text("This saved answer has no current application form to edit.")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
            } else {
                Text("Waiting for an application form. This question is read only.")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
            }
        }.listRowBackground(Palette.surface)
    }
}

private func workflowQuestionScope(_ kind: String) -> String {
    ["personal": "Reusable personal answer", "company": "This employer only", "application": "This application only"][kind] ?? kind
}

private struct WorkflowQuestionSyncNotice: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        if let message = model.workflow?.questionSyncMessage {
            VStack(alignment: .leading, spacing: 6) {
                Label(message, systemImage: "arrow.triangle.2.circlepath")
                    .font(AppTypography.font(.footnote))
                Text("Saved questions and your drafts stay visible. Answer controls will be available after sync finishes.")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
            }.fixedSize(horizontal: false, vertical: true)
                .accessibilityElement(children: .combine)
        }
    }
}

private struct WorkflowAnswerSelection: Identifiable {
    let id = UUID()
    let process: WorkflowProcess
    let questions: [WorkflowQuestion]
    let values: [String: JSONValue]
    var scopeKind: String? = nil
}

private struct WorkflowQuestionEditor: View {
    @EnvironmentObject private var model: AppModel
    let process: WorkflowProcess
    let questions: [WorkflowQuestion]
    var sharedQuestion: WorkflowSharedQuestion? = nil
    let reviewAnswers: (WorkflowAnswerSelection) -> Void
    @State private var values: [String: JSONValue] = [:]
    @State private var changedNotice: String?
    @State private var editingSaved: Set<String> = []
    private var canEdit: Bool {
        model.online && model.pending == nil && process.canAnswer && model.workflow?.capabilities?.questions == true &&
        model.workflow?.questionSyncAllowsAnswers != false &&
        model.workflow?.readErrors["questions"] == nil && model.workflow?.readErrors["processes"] == nil &&
        questions.allSatisfy { $0.formVersion == process.formVersion }
    }
    private var validationError: String? {
        for question in questions {
            guard let value = values[question.id] else { continue }
            do { try question.validate(value) } catch { return error.localizedDescription }
        }
        return nil
    }
    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Text(process.companyName).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                Text(process.title).font(AppTypography.font(.headline))
                Text("Saved answers sync across your devices. The server reuses them only within their confirmed scope.")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                if let sharedQuestion, sharedQuestion.bindingIDs.count > 1 {
                    Text("Shared by \(sharedQuestion.bindingIDs.count) application forms.")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                }
                if let reason = process.blockReason, !reason.isEmpty { Text(workflowLabel(reason)).font(AppTypography.font(.footnote)) }
                if !process.canAnswer { Text("This application is not accepting answers in its current state.").font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted) }
                if let changedNotice { Text(changedNotice).font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust) }
                DisclosureGroup("Form details") {
                    Text("Form: \(process.formVersion ?? "Not available")")
                    Text("Application revision: \(process.revision)")
                    Text(process.id).textSelection(.enabled)
                }.font(AppTypography.font(.caption))
            }.fixedSize(horizontal: false, vertical: true)
            ForEach(questions) { question in
                let hasAcceptedAnswer = sharedQuestion?.hasAcceptedAnswer ?? question.hasAcceptedAnswer
                VStack(alignment: .leading, spacing: 10) {
                    Text(sharedQuestion?.text ?? question.text).font(AppTypography.font(.headline)).fixedSize(horizontal: false, vertical: true)
                    Text("\(question.required ? "Required" : "Optional") · \(workflowLabel(question.type)) · \(workflowQuestionScope(sharedQuestion?.scope.kind ?? question.scope))")
                        .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                    if !question.reason.isEmpty { Text(question.reason).font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted) }
                    if let answerText = displayedAnswer(question) {
                        Text("\(hasAcceptedAnswer ? "Saved answer" : "Suggested answer"): \(answerText)")
                            .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted).textSelection(.enabled)
                    }
                    if question.isSupported && (!hasAcceptedAnswer || editingSaved.contains(question.id)) {
                        answerField(question).disabled(!canEdit)
                        if values[question.id] != nil {
                            Button { values.removeValue(forKey: question.id) } label: {
                                Text("Remove draft answer").font(AppTypography.font(.footnote)).frame(minHeight: 44)
                            }.buttonStyle(.borderless).disabled(!canEdit)
                        }
                    } else if question.isSupported {
                        Button("Change saved answer") { editingSaved.insert(question.id) }
                            .buttonStyle(.borderless).disabled(!canEdit)
                    } else {
                        Text("This question type or scope is not supported. Its current answer is read only.")
                            .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust)
                    }
                    DisclosureGroup("Question details") {
                        if let sharedQuestion, sharedQuestion.text != question.text { Text("Application wording: \(question.text)") }
                        Text("Type: \(question.type) · Scope: \(question.scope)")
                        Text("Form: \(question.formVersion)")
                        Text("Question revision: \(question.revision)")
                        Text(question.id).textSelection(.enabled)
                    }.font(AppTypography.font(.caption))
                }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 6)
            }
            VStack(alignment: .leading, spacing: 8) {
                if let validationError { Text(validationError).font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust) }
                Button {
                    reviewAnswers(WorkflowAnswerSelection(process: process, questions: questions, values: values,
                        scopeKind: sharedQuestion?.scope.kind))
                } label: {
                    Text(values.isEmpty ? "Enter answers to review" : "Review \(values.count) \(values.count == 1 ? "answer" : "answers")")
                        .lineLimit(nil).fixedSize(horizontal: false, vertical: true).frame(minHeight: 44)
                }.buttonStyle(.borderless).disabled(!canEdit || !model.canSave || values.isEmpty || validationError != nil)
                Text("Only your draft answers will be saved. Unanswered optional questions are omitted. Missing required answers keep the application paused.")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }.listRowBackground(Palette.surface)
        .onChange(of: questions) { previous, current in
            let update = QuestionDraftSync.reconcile(values: values, previous: previous, current: current,
                snapshotReady: model.workflow?.questionSyncAllowsAnswers != false)
            values = update.values
            editingSaved.subtract(update.remotelyAnsweredIDs.union(update.invalidatedIDs))
            if !update.remotelyAnsweredIDs.isEmpty {
                changedNotice = "Saved answers synced. Your other draft answers were kept."
            } else if !update.invalidatedIDs.isEmpty {
                changedNotice = "Some questions changed. Their drafts were cleared; unchanged drafts were kept."
            }
        }
        .onChange(of: process.formVersion) { _, _ in clearChangedDrafts() }
    }
    private func displayedAnswer(_ question: WorkflowQuestion) -> String? {
        if let sharedQuestion { return sharedQuestion.answerDisplayText }
        return question.answer.map { workflowAnswer($0, question: question) }
    }
    private func clearChangedDrafts() {
        guard model.workflow?.questionSyncAllowsAnswers != false else { return }
        if !values.isEmpty {
            values = [:]
            changedNotice = "The application form changed. Review its current questions before answering."
        }
        editingSaved = []
    }
    @ViewBuilder private func answerField(_ question: WorkflowQuestion) -> some View {
        if question.type == "text" {
            TextField("Your answer", text: Binding(
                get: { values[question.id]?.text ?? "" },
                set: { values[question.id] = .string($0); changedNotice = nil }
            ), axis: .vertical)
                .textFieldStyle(.roundedBorder).lineLimit(3...12).frame(minHeight: 44)
                .accessibilityLabel(question.text)
        } else {
            ForEach(question.choices) { choice in
                let checked = question.type == "single_choice" ? values[question.id]?.text == choice.id : values[question.id]?.array.compactMap(\.text).contains(choice.id) == true
                Button {
                    if question.type == "single_choice" {
                        values[question.id] = .string(choice.id)
                    } else {
                        var selected = Set(values[question.id]?.array.compactMap(\.text) ?? [])
                        if selected.contains(choice.id) { selected.remove(choice.id) } else { selected.insert(choice.id) }
                        values[question.id] = .array(selected.sorted().map(JSONValue.string))
                    }
                    changedNotice = nil
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: checked ? "checkmark.circle.fill" : "circle").frame(width: 24)
                        Text(choice.label).lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }.frame(minHeight: 44).contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundStyle(checked ? Palette.primary : Palette.ink)
                    .accessibilityLabel(choice.label).accessibilityValue(checked ? "Selected" : "Not selected")
                    .accessibilityAddTraits(checked ? .isSelected : [])
            }
        }
    }
}

private struct WorkflowAnswerConfirmation: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var save = WorkflowConfirmationSave()
    let selection: WorkflowAnswerSelection
    private var supplied: [WorkflowQuestion] { selection.questions.filter { selection.values[$0.id] != nil } }
    private var savedElsewhere: [WorkflowQuestion] {
        guard model.online, model.pending == nil,
              model.workflow?.questionSyncAllowsAnswers != false,
              model.workflow?.readErrors["questions"] == nil else { return [] }
        let ids = Set(supplied.map(\.id))
        return (model.workflow?.questions ?? []).filter { ids.contains($0.id) && $0.hasAcceptedAnswer &&
            $0.processID == selection.process.id && $0.formVersion == selection.process.formVersion }
    }
    private var isCurrent: Bool {
        guard QuestionDraftSync.confirmationHasCurrentSnapshot(selection.questions, workflow: model.workflow),
              let current = model.workflow?.processes.first(where: { $0.id == selection.process.id }),
              current.revision == selection.process.revision, current.formVersion == selection.process.formVersion,
              current.canAnswer, model.workflow?.capabilities?.questions == true,
              model.workflow?.readErrors["questions"] == nil,
              model.workflow?.readErrors["processes"] == nil else { return false }
        return selection.questions.allSatisfy { captured in
            model.workflow?.questions.contains { $0.id == captured.id && $0.revision == captured.revision && $0.formVersion == captured.formVersion } == true
        }
    }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(selection.process.companyName).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                    Text(selection.process.title).font(AppTypography.font(.headline))
                    Text("Save these answers. The server reuses them only within their confirmed scope and resumes compatible applications when their required answers are complete.")
                    if model.workflow?.capabilities?.workerEnabled == false {
                        Text("The worker is not configured. Saving answers will not start application work.").foregroundStyle(Palette.muted)
                    }
                    Text("Form \(selection.process.formVersion ?? "") · Application revision \(selection.process.revision)")
                        .font(AppTypography.font(.caption)).textSelection(.enabled)
                }.listRowBackground(Palette.surface)
                Section("\(supplied.count) supplied \(supplied.count == 1 ? "answer" : "answers")") {
                    ForEach(supplied) { question in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(question.text).font(AppTypography.font(.headline))
                            if let value = selection.values[question.id] {
                                Text(workflowAnswer(value, question: question)).textSelection(.enabled)
                            }
                            Text("Question revision \(question.revision) · \(workflowQuestionScope(selection.scopeKind ?? question.scope))").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                        }.fixedSize(horizontal: false, vertical: true)
                    }
                }.listRowBackground(Palette.surface)
                WorkflowSaveFeedback()
                WorkflowQuestionSyncNotice().listRowBackground(Color.clear)
                if !save.isSaving && !savedElsewhere.isEmpty && !isCurrent {
                    Section("Answers already saved") {
                        Text("These questions have saved answers. Close this review to see the current questions. Your unrelated drafts are kept.")
                        ForEach(savedElsewhere) { question in
                            if let answer = question.answer {
                                Text(question.text)
                                Text(workflowAnswer(answer, question: question)).textSelection(.enabled)
                            }
                        }
                    }.listRowBackground(Palette.surface)
                }
                if model.workflow?.questionSyncAllowsAnswers != false && save.showsStale(isCurrent: isCurrent, hasPending: model.pending != nil) {
                    Text("The questions or sync snapshot changed. Close this confirmation and review the current answers. Your unchanged drafts are kept.")
                        .foregroundStyle(Palette.rust)
                }
                Button {
                    Task {
                        guard isCurrent else { return }
                        await save.run {
                            await model.submitAnswers(process: selection.process, questions: selection.questions, values: selection.values)
                        } onConfirmed: { dismiss() }
                    }
                } label: {
                    Text("Save \(supplied.count) \(supplied.count == 1 ? "answer" : "answers")")
                        .lineLimit(nil).fixedSize(horizontal: false, vertical: true).frame(minHeight: 44)
                }.disabled(save.isSaving || !model.canSave || !isCurrent || supplied.isEmpty)
            }.scrollContentBackground(.hidden).background(Palette.paper)
                .navigationTitle("Confirm answers").inlineTitle()
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() }.frame(minHeight: 44) } }
        }
    }
}

private func workflowAnswer(_ value: JSONValue, question: WorkflowQuestion) -> String {
    func label(_ id: String) -> String { question.choices.first { $0.id == id }?.label ?? id }
    switch value {
    case .string(let text): return question.type == "single_choice" ? label(text) : (text.isEmpty ? "Empty answer" : text)
    case .array(let items):
        let labels = items.compactMap(\.text).map(label)
        return labels.isEmpty ? "No choices selected" : labels.joined(separator: ", ")
    default: return workflowJSON(value)
    }
}

private func workflowLabel(_ value: String) -> String {
    ["not_started": "Not started", "preparing": "Preparing", "submission_unknown": "Outcome unknown",
     "submitted": "Submitted", "awaiting_response": "Awaiting employer response", "responded": "Employer responded",
     "interviewing": "Interviewing", "offer": "Offer", "rejected": "Rejected", "withdrawn": "Withdrawn", "hired": "Hired",
     "queued": "Queued", "running": "Running", "waiting_for_user": "Needs your input", "retry_wait": "Waiting to retry",
     "succeeded": "Task finished", "failed": "Failed", "cancelled": "Cancelled", "explore": "Research",
     "rank": "Rank jobs", "apply": "Application", "authorized": "Apply authorized", "questions_requested": "Questions requested",
     "answers_saved": "Answers saved", "unblock_instruction_saved": "Agent instructions saved", "submission_started": "Submission attempt started", "recruiting_updated": "Recruiting status updated",
     "blocked": "Blocked", "questions_pending": "Waiting for your answers", "text": "Written answer",
     "single_choice": "Choose one", "multi_choice": "Choose any"][value] ?? value
}

private func workflowDate(_ value: String) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    var date = formatter.date(from: value)
    if date == nil { formatter.formatOptions = [.withInternetDateTime]; date = formatter.date(from: value) }
    return date?.formatted(date: .abbreviated, time: .shortened) ?? value
}

private func workflowJSON(_ value: JSONValue) -> String {
    let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    do {
        let data = try encoder.encode(value)
        return String(decoding: data, as: UTF8.self)
    } catch { return "Could not display this value: \(error.localizedDescription)" }
}
