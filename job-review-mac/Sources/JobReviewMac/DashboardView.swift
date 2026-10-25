import SwiftUI
import MacDashboardCore
import ReviewCore

private let accent = AdminTheme.link

func statusColor(_ status: String) -> Color {
    switch status {
    case "failed", "blocked", "submission_unknown": return AdminTheme.dangerText
    case "waiting_for_user", "retry_wait", "questions_requested": return AdminTheme.warningText
    case "running", "checking", "preparing": return AdminTheme.infoText
    case "passed", "ready", "succeeded", "submitted": return AdminTheme.successText
    default: return .secondary
    }
}

struct StatusLabel: View {
    let status: String
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(statusColor(status)).frame(width: 6, height: 6)
            Text(status.readableStatus).font(.system(size: 11, weight: .medium))
        }.foregroundStyle(statusColor(status))
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(statusColor(status).opacity(0.08), in: Capsule())
        .accessibilityElement(children: .combine)
    }
}

struct DashboardView: View {
    @ObservedObject var model: DashboardModel
    @State private var cancellationTask: WorkflowTask?
    var body: some View {
        AdminShell(model: model) {
            VStack(spacing: 0) {
                LocalServerControls(server: model.localServer, saving: model.isSaving || model.projectConsent.isSaving)
                if model.demo { notice("Fictional demo", detail: "This window uses sample data. It is not connected to your backend.", color: accent) }
                if model.phase == .stale || (model.phase == .disconnected && model.workflow != nil) {
                    notice("Updates paused", detail: "Showing the last available updates. \(model.errorMessage ?? "Refresh to get current status.")", color: .orange)
                }
                commandStatus
                Group {
                    if !model.hasWorkspaceData && model.page != .connection && model.page != .applicationWorkflow {
                        unavailable
                    } else {
                        switch model.page {
                        case .overview: AdminOverviewView(model: model)
                        case .tasks: tasks
                        case .feed: JobBrowserView(model: model)
                        case .questions: questionsInbox
                        case .activity: activity
                        case .applicationWorkflow: ApplicationWorkflowView(model: model.applicationWorkflow)
                        case .connection: connection
                        }
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
            .modifier(ProjectConsentPresentation(model: model.projectConsent))
            .sheet(item: $model.instructionSelection) { selection in
                AgentInstructionsView(model: model, editor: model.agentInstructions, selection: selection)
            }
            .confirmationDialog("Cancel queued work?", isPresented: Binding(get: { cancellationTask != nil }, set: { if !$0 { cancellationTask = nil } }), presenting: cancellationTask) { task in
                Button("Cancel queued work", role: .destructive) { Task { await model.cancel(task) }; cancellationTask = nil }
                Button("Keep work", role: .cancel) { cancellationTask = nil }
            } message: { _ in Text("This stops the queued task. It does not withdraw an application already sent to an employer.") }
    }

    @ViewBuilder private var commandStatus: some View {
        if let pending = model.pendingSave {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Save needs confirmation").font(.callout.weight(.semibold))
                    Text(pending.summary.safeDiagnostic).font(.caption)
                    Text("Check the original request before making another decision.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Button("Check saved request") { Task { await model.retryPending() } }
                    .disabled(model.isSaving || model.isRefreshing).accessibilityIdentifier("retry-pending-save")
            }.padding(16).background(Color.orange.opacity(0.08))
        }
        if let error = model.commandError {
            notice("Could not save", detail: error, color: .orange)
        } else if let message = model.commandMessage {
            HStack(spacing: 12) {
                Image(systemName: "checkmark.circle").foregroundStyle(accent)
                Text(message).font(.callout)
                Spacer(minLength: 4)
                Button { model.commandMessage = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).help("Dismiss confirmation")
            }.padding(16).background(accent.opacity(0.07))
        } else if model.isSaving {
            HStack(spacing: 10) { ProgressView().controlSize(.small); Text("Saving your decision…").font(.callout); Spacer() }.padding(16)
        }
    }

    private var unavailable: some View {
        VStack(spacing: 18) {
            if model.phase == .loading {
                ProgressView().controlSize(.large)
                Text("Connecting to Job Review").font(.title2.weight(.semibold))
                Text("Loading health, tasks, and your feed.").foregroundStyle(.secondary)
            } else {
                Image(systemName: "network.slash").font(.system(size: 38, weight: .light)).foregroundStyle(.secondary)
                Text("Backend unavailable").font(.title2.weight(.semibold))
                Text(model.errorMessage ?? "The connection could not be made.").foregroundStyle(.secondary)
                Text(model.recovery ?? "Check your connection and try again.").font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420)
                HStack {
                    Button("Connection details") { model.page = .connection }
                    Button("Try again") { Task { await model.refresh() } }.buttonStyle(.borderedProminent).disabled(model.demo)
                }
            }
        }.padding(40).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var tasks: some View {
        HSplitView {
            TaskBoardView(model: model)
            if let id = model.selectedTaskID, let task = model.tasks.first(where: { $0.id == id }) {
                taskDetails(task).frame(minWidth: 300, idealWidth: 380, maxWidth: 460)
            }
        }
    }
    private var questionsInbox: some View {
        HSplitView {
            QuestionsInboxView(model: model)
            if let id = model.selectedTaskID, let task = model.tasks.first(where: { $0.id == id }) {
                Group {
                    if model.questionsFilter == .issues {
                        ScrollView { taskDetailContent(task).padding(22) }.background(AdminTheme.surface)
                    } else { taskDetails(task) }
                }.frame(minWidth: 300, idealWidth: 380, maxWidth: 460)
            }
        }
    }
    @ViewBuilder private func taskDetails(_ task: WorkflowTask) -> some View {
        let process = model.process(for: task)
        let questions = model.questions.filter { $0.processID == process?.id && $0.formVersion == process?.formVersion }
        if let process, !questions.isEmpty {
            ApplicationQuestionsView(model: model, process: process, questions: questions, context: AnyView(
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("APPLICATION").font(.caption.weight(.semibold)).tracking(1).foregroundStyle(AdminTheme.muted)
                        Spacer()
                        Button { model.selectedTaskID = nil } label: { Image(systemName: "xmark") }
                            .buttonStyle(.plain).help("Close task details").accessibilityLabel("Close task details")
                    }
                    instructionEntry(task)
                    DisclosureGroup("Task details") { taskDetailContent(task, showsInstructions: false).padding(.top, 12) }
                        .font(.caption)
                }
            ), editor: model.questionEditor(process: process, questions: questions))
        } else {
            ScrollView { taskDetailContent(task).padding(22) }.background(AdminTheme.surface)
        }
    }
    @ViewBuilder private func instructionEntry(_ task: WorkflowTask) -> some View {
        if ["apply", "explore"].contains(task.kind) {
            Button { model.openInstructions(task) } label: {
                Label("Instructions for agent", systemImage: "text.bubble")
            }.disabled(model.isSaving || model.isRefreshing)
                .accessibilityIdentifier("open-agent-instructions")
        }
    }
    private func taskDetailContent(_ task: WorkflowTask, showsInstructions: Bool = true) -> some View {
        let process = model.process(for: task)
        let questions = model.questions.filter { $0.processID == process?.id && $0.formVersion == process?.formVersion }
        return VStack(alignment: .leading, spacing: 20) {
                HStack {
                    Text(task.kind.readableStatus.uppercased()).font(.caption.weight(.semibold)).tracking(1.5).foregroundStyle(.secondary)
                    Spacer()
                    Button { model.selectedTaskID = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).help("Close task details")
                }
                VStack(alignment: .leading, spacing: 7) {
                    Text(model.taskTitle(task)).font(.title2.weight(.semibold))
                    Text(model.taskSubtitle(task)).foregroundStyle(.secondary)
                    StatusLabel(status: task.status)
                }
                if let process {
                    Divider()
                    detail("Submission", process.submissionStatus.readableStatus)
                    if process.submissionStatus == "submitted" { detail("Recruiting", process.recruitingStatus.readableStatus) }
                    if process.submissionStatus == "submission_unknown" {
                        guidance("Confirm the outcome", "A submission may have occurred. Check the saved receipt with the backend operator before any retry.")
                    }
                }
                if task.resourceQuarantined { guidance("Browser work is held", "The backend cannot confirm that earlier browser work stopped. An operator must verify this before releasing the task.") }
                if let reason = process?.blockReason ?? task.error, !reason.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("Reported issue").font(.headline)
                        Text(reason.contains(" ") ? reason.safeDiagnostic : reason.safeDiagnostic.readableStatus).font(.callout.weight(.medium))
                        if let detail = process?.events.last(where: { $0.type == "blocked" && $0.data["reason"]?.text == reason })?.data["detail"]?.text {
                            Text(detail.safeDiagnostic).font(.callout).textSelection(.enabled)
                        }
                    }
                    guidance("Next step", nextStep(task, hasQuestions: questions.contains { !$0.hasAcceptedAnswer }))
                } else if task.status == "failed" { guidance("Next step", "Read the task status and add Instructions for agent to help review the blocker.") }
                if showsInstructions { instructionEntry(task) }
                if task.canCancel {
                    Button("Cancel queued work", role: .destructive) { cancellationTask = task }
                        .disabled(!model.canCancel(task)).accessibilityIdentifier("cancel-queued-task")
                }
                Divider()
                detail("Task updated", formattedDate(task.updatedAt))
                if task.status == "retry_wait" || task.status == "queued" { detail("Available after", formattedDate(task.availableAt)) }
                detail("Worker attempts", "\(task.attempts)")
                detail("Failures / limit", "\(task.failureCount) / \(task.maxAttempts)")
                DisclosureGroup("Task identifiers") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(task.id).font(.caption.monospaced()).textSelection(.enabled)
                        if let process { Text(process.jobKey).font(.caption.monospaced()).textSelection(.enabled) }
                    }.padding(.top, 8)
                }.font(.caption)
                if let url = process?.officialURL {
                    Link(destination: url) { Label("Open official posting", systemImage: "arrow.up.right") }.font(.callout)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
    }
    private func nextStep(_ task: WorkflowTask, hasQuestions: Bool) -> String {
        if task.resourceQuarantined { return "Ask the backend operator to confirm that browser work stopped. Do not repeat an uncertain submission." }
        if let process = model.process(for: task), process.submissionStatus == "submission_unknown" {
            return "Confirm the earlier submission outcome before continuing. Answering questions does not resolve this uncertainty."
        }
        if hasQuestions, let process = model.process(for: task), model.acceptsQuestionAnswers(process) {
            return "Answer the current questions below or on your iPhone. Complete required answers to resume the same application."
        }
        if task.error == "tool_approval_required" { return "This task needs tool approval in the worker chat. There are no application answers to submit for this block." }
        if task.error == "ranked_output_pending" { return "The result is saved. The backend will retry writing its output. If this persists, check storage with the backend operator." }
        if task.error == "posting_changed" { return "The posting changed after approval. Review its current details with the backend operator before continuing." }
        if task.error == "prior_application_found" { return "An earlier application was found. Ask the backend operator to verify it before continuing." }
        if task.status == "retry_wait" { return "The backend will retry after the scheduled time. No action is needed unless the task fails again." }
        return "Review the reported issue. Add Instructions for agent to explain what changed or what the agent should check."
    }

    private var activity: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                Text("Activity").font(.system(size: 24, weight: .semibold))
                Text("Application events and the latest update for each discovery task.").foregroundStyle(.secondary)
                if model.activity.isEmpty { empty("No activity yet", detail: "Saved backend events will appear here.") }
                ForEach(model.activity) { item in activityRow(item) }
            }.padding(30).frame(maxWidth: 1100, alignment: .leading).frame(maxWidth: .infinity)
        }
    }
    private func activityRow(_ item: ActivityItem) -> some View {
        Button { model.showTask(item.taskID) } label: {
            HStack(alignment: .top, spacing: 13) {
                Circle().fill(statusColor(item.status)).frame(width: 7, height: 7).padding(.top, 5)
                VStack(alignment: .leading, spacing: 5) {
                    Text(item.title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.primary)
                    Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                Text(formattedDate(item.date)).font(.caption2).foregroundStyle(.secondary)
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading).adminPanel().contentShape(Rectangle())
        }.buttonStyle(.plain)
    }
    private var connection: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("Connection").font(.system(size: 24, weight: .semibold))
                HStack {
                    Label(model.phase.title, systemImage: model.phase == .connected ? "checkmark.circle.fill" : "network")
                        .font(.title3.weight(.medium)).foregroundStyle(model.phase == .connected ? accent : Color.orange)
                    Spacer()
                    if !model.demo { Button("Refresh") { Task { await model.refresh() } }.disabled(model.isRefreshing) }
                }
                if let error = model.errorMessage { guidance(error, model.recovery ?? "Check the connection configuration.") }
                detail("Backend", model.settings?.origin ?? (model.demo ? "Fictional demo" : "No valid connection loaded"))
                ProjectConsentStatusView(model: model.projectConsent)
                Divider()
                if let snapshot = model.snapshot {
                    detail("Last complete update", snapshot.fetchedAt.formatted(date: .abbreviated, time: .standard))
                    detail("Workers", snapshot.feed.capabilities.workerConfigurationTitle)
                    Divider()
                    Text("Backend checks").font(.title3.weight(.semibold))
                    ForEach(snapshot.health.checks) { check in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(["storage": "Storage", "chrome_attachment": "Chrome", "linkedin_auth": "LinkedIn sign-in"][check.id] ?? check.id.readableStatus).font(.headline)
                                Text(check.message.safeDiagnostic).font(.callout).foregroundStyle(.secondary)
                            }
                            Spacer()
                            StatusLabel(status: check.status)
                        }.padding(.vertical, 5)
                    }
                    Text("Chrome and LinkedIn are checked when Explore or Apply needs the browser. Ranking uses saved postings. An idle browser is normal.").font(.callout).foregroundStyle(.secondary)
                    if let checked = snapshot.health.checkedAt { detail("Health observation", formattedDate(checked)) }
                }
                Divider()
                Text("About this connection").font(.title3.weight(.semibold))
                Text("Job Review reads the existing backend every 15 seconds. Closing this window keeps the menu bar available. Quitting the app leaves the backend running.").font(.callout).foregroundStyle(.secondary)
                DisclosureGroup("Configuration and diagnostics") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("The app loads your private local connection configuration. Credentials stay in the existing private file and are never included in the app bundle.")
                        if let settings = model.settings { detail("Credential file", settings.credentialFile) }
                        Text("To use another connection, create ~/.config/job-review-mac/connection.json with origin and credential_file. Use an HTTPS origin and owner-only permissions (0600). Then refresh.")
                        Text("The API reports task states, not worker thread heartbeats. Lane activity is inferred from running tasks. Separate API reads can observe nearby points in time.")
                        Text("Decisions, answers, and queued-work cancellations use the same workspace as iPhone. Pending saves stay in a private local journal so a connection failure can be checked without creating another request.")
                    }.font(.caption).foregroundStyle(.secondary).padding(.top, 12).textSelection(.enabled)
                }
            }.padding(30).frame(maxWidth: 1000, alignment: .leading).frame(maxWidth: .infinity)
        }
    }
    private func detail(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) { Text(label).font(.caption).foregroundStyle(.secondary); Text(value).font(.callout).textSelection(.enabled) }
    }
    private func guidance(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) { Text(title).font(.headline); Text(text).font(.callout).foregroundStyle(.secondary) }
            .padding(14).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
    }
    private func empty(_ title: String, detail: String) -> some View {
        VStack(spacing: 10) { Text(title).font(.headline); Text(detail).foregroundStyle(.secondary) }.frame(maxWidth: .infinity).padding(35)
    }
    private func notice(_ title: String, detail: String, color: Color) -> some View {
        HStack(spacing: 12) { Image(systemName: "info.circle"); Text(title).bold(); Text(detail); Spacer() }
            .font(.caption).foregroundStyle(color).padding(.horizontal, 24).padding(.vertical, 10).background(color.opacity(0.07))
    }
    private func formattedDate(_ value: String) -> String { timestamp(value)?.formatted(date: .abbreviated, time: .shortened) ?? "Time unavailable" }
}
