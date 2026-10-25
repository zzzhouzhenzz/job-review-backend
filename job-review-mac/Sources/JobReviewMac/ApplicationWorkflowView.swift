import SwiftUI
import MacDashboardCore

struct ApplicationWorkflowView: View {
    @ObservedObject var model: ApplicationWorkflowModel
    private var fresh: Bool { model.phase == .observed }
    private var observationColor: Color { fresh ? AdminTheme.successText : AdminTheme.warningText }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if let value = model.observation {
                    BrowserAccessView(value: value, fresh: fresh)
                    workerLanes(value)
                    sharedPool(value)
                    if let worker = model.selectedWorker { detail(worker, value: value) }
                } else { unavailable }
            }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }.background(AdminTheme.canvas)
            .task { await model.monitor() }
            .accessibilityIdentifier("application-workflow")
    }
    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Application workflow").font(.system(size: 24, weight: .semibold))
                    Text(model.observation?.browserAccess == nil ? "Three workers. One shared question pool." : "Three workers. One shared Chrome connection.").foregroundStyle(AdminTheme.muted)
                }
                Spacer()
                Button { Task { await model.refresh() } } label: {
                    Label(model.isRefreshing ? "Reading…" : "Refresh", systemImage: "arrow.clockwise")
                }.disabled(model.isRefreshing || model.fictional).accessibilityIdentifier("refresh-application-workflow")
            }
            HStack(spacing: 8) {
                Image(systemName: fresh ? "checkmark.circle" : "clock.badge.exclamationmark")
                Text(model.fictional ? "Fictional example · \(model.phase == .observed ? "fixed observation" : model.phase.title)" : model.phase.title).fontWeight(.medium)
                if let value = model.observation {
                    Text("· revision \(value.revision) · \(shortTime(value.generatedAt))")
                }
            }.font(.caption).foregroundStyle(observationColor).accessibilityIdentifier("workflow-freshness")
            if let error = model.error { Text(error).font(.caption).foregroundStyle(AdminTheme.warningText) }
            Text(fresh ? "Only reported stages are highlighted. Observations refresh every 5 seconds while this page is open." : "Current activity is unknown. Any visible stages belong to the last observation.")
                .font(.caption).foregroundStyle(AdminTheme.muted)
        }
    }
    private func workerLanes(_ value: ApplicationWorkflowObservation) -> some View {
        VStack(spacing: 0) {
            ForEach(value.workers) { worker in
                Button { model.selectedWorkerID = worker.id } label: {
                    VStack(alignment: .leading, spacing: 13) {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(worker.id).font(.system(size: 12, weight: .semibold)).frame(width: 60, alignment: .leading)
                            Text(worker.company?.safeDiagnostic ?? (worker.state == "available" ? "Available for a job" : "No job reported"))
                                .fontWeight(.semibold).lineLimit(1)
                            if let title = worker.jobTitle { Text(title.safeDiagnostic).foregroundStyle(AdminTheme.muted).lineLimit(1) }
                            Spacer(minLength: 4)
                            stateBadge(worker)
                        }
                        HStack(spacing: 5) {
                            ForEach(ApplicationWorkerStep.allCases) { step in
                                let selected = step.contains(worker.stage) && worker.knownState
                                Text(step.rawValue).font(.system(size: 10, weight: selected ? .semibold : .regular))
                                    .frame(maxWidth: .infinity).padding(.vertical, 7)
                                    .foregroundStyle(selected ? (fresh ? workerColor(worker) : AdminTheme.muted) : AdminTheme.muted)
                                    .background(selected ? (fresh ? workerColor(worker) : AdminTheme.muted).opacity(0.13) : AdminTheme.canvas,
                                                in: RoundedRectangle(cornerRadius: 4))
                                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(selected ? (fresh ? workerColor(worker) : AdminTheme.muted) : .clear, lineWidth: 1))
                                if step != .done { Image(systemName: "chevron.right").font(.system(size: 8)).foregroundStyle(AdminTheme.muted).accessibilityHidden(true) }
                            }
                        }.accessibilityElement(children: .ignore)
                            .accessibilityLabel("\(worker.id), reported stage: \(stageTitle(worker.stage, known: worker.knownStage))")
                        HStack {
                            Text("\(fresh ? "Observed" : "Last reported"): \(stageTitle(worker.stage, known: worker.knownStage))")
                            Spacer()
                            if !worker.questionIds.isEmpty { Text("\(worker.questionIds.count) linked questions") }
                        }.font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
                    }.padding(16).contentShape(Rectangle())
                        .background(model.selectedWorker?.id == worker.id ? AdminTheme.primary.opacity(0.04) : .clear)
                }.buttonStyle(.plain).accessibilityIdentifier("workflow-worker-\(worker.id)")
                    .accessibilityAddTraits(model.selectedWorker?.id == worker.id ? .isSelected : [])
                if worker.id != value.workers.last?.id { Divider() }
            }
        }.background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border, lineWidth: 1))
    }
    private func stateBadge(_ worker: ApplicationWorker) -> some View {
        Label(worker.stage == "waiting_for_browser" ? "Waiting for Chrome" : worker.knownState ? worker.state.readableStatus : "Unknown state", systemImage: worker.stage == "waiting_for_browser" ? "clock" : worker.state == "paused" ? "pause.circle" : worker.state == "available" ? "circle.dashed" : worker.state == "ready" ? "checkmark.circle" : "circle.fill")
            .font(.system(size: 10, weight: .semibold)).foregroundStyle(fresh ? workerColor(worker) : AdminTheme.muted)
    }
    private func workerColor(_ worker: ApplicationWorker) -> Color {
        if worker.stage == "waiting_for_browser" { return AdminTheme.warningText }
        switch worker.state {
        case "paused": return AdminTheme.warningText
        case "running": return AdminTheme.infoText
        case "ready": return AdminTheme.successText
        case "error": return AdminTheme.dangerText
        default: return AdminTheme.muted
        }
    }
    private func sharedPool(_ value: ApplicationWorkflowObservation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Shared question pool", systemImage: "arrow.triangle.branch").font(.headline)
                    Text("Workers match their own blockers. Each worker keeps its own application and model session.").font(.caption).foregroundStyle(AdminTheme.muted)
                }
                Spacer()
                Text("Pool revision \(value.questionPool.revision)").font(.caption).foregroundStyle(AdminTheme.muted)
            }
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Shared lock · \(lockTitle(value.questionLock.state))", systemImage: value.questionLock.state == "held" ? "lock.fill" : "lock.open")
                        .fontWeight(.semibold)
                    Text("Owner: \(lockOwner(value.questionLock))")
                    Text("Waiting: \(value.questionLock.waitingWorkerIds.isEmpty ? "None reported" : value.questionLock.waitingWorkerIds.joined(separator: ", "))")
                    if let acquired = value.questionLock.acquiredAt { Text("Acquired \(shortTime(acquired))") }
                }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                Divider()
                VStack(alignment: .leading, spacing: 5) {
                    Label("Pending Markdown", systemImage: "doc.text").fontWeight(.semibold)
                    Text("\(value.questionPool.pendingCount) pending").font(.title3.weight(.semibold)).monospacedDigit()
                    Text("Linked by stable question ID").foregroundStyle(AdminTheme.muted)
                }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "arrow.right").foregroundStyle(AdminTheme.muted).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Label("Answered Markdown", systemImage: "doc.text").fontWeight(.semibold)
                    Text("\(value.questionPool.answeredCount) answered").font(.title3.weight(.semibold)).monospacedDigit()
                    Text("Snapshot revision \(value.answerFile.revision.map(String.init) ?? "unknown")").foregroundStyle(AdminTheme.muted)
                }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
            }.fixedSize(horizontal: false, vertical: true)
            Divider()
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Stored hash · \(value.answerFile.hashAlgorithm?.safeDiagnostic.uppercased() ?? "Unknown algorithm")").font(.caption.weight(.semibold))
                    Text(value.answerFile.hash.map { "\($0.prefix(16))…\($0.suffix(8))" } ?? "No published hash reported")
                        .font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Text("Answer handler: \(stageTitle(value.answerHandler.stage, known: ApplicationAnswerHandler.stages.contains(value.answerHandler.stage) && value.answerHandler.stage != "unknown"))")
                        .font(.caption.weight(.semibold)).foregroundStyle(fresh && value.answerHandler.stage != "idle" ? AdminTheme.infoText : AdminTheme.muted)
                    if let updated = value.answerHandler.updatedAt { Text("Updated \(shortTime(updated))").font(.caption).foregroundStyle(AdminTheme.muted) }
                }
            }
            handlerSteps(value.answerHandler)
            Text("Format before locking → validate and save under the same lock → publish snapshot + hash → check blockers → release lock and wake ready workers with code.")
                .font(.system(size: 11)).foregroundStyle(AdminTheme.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(18).background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border, lineWidth: 1))
    }
    private func handlerSteps(_ handler: ApplicationAnswerHandler) -> some View {
        let stages = ["formatting", "waiting_for_lock", "saving_answers", "publishing_answers", "checking_blockers"]
        return HStack(spacing: 6) {
            ForEach(stages, id: \.self) { stage in
                Text(stage == "formatting" ? "Format · LLM" : stage == "waiting_for_lock" ? "Acquire lock" : stage == "saving_answers" ? "Save · code" : stage == "publishing_answers" ? "Publish · code" : "Check · code")
                    .font(.system(size: 10, weight: stage == handler.stage ? .semibold : .regular))
                    .frame(maxWidth: .infinity).padding(.vertical, 6)
                    .background(stage == handler.stage ? (fresh ? AdminTheme.infoText : AdminTheme.muted).opacity(0.12) : AdminTheme.canvas, in: RoundedRectangle(cornerRadius: 4))
                    .foregroundStyle(stage == handler.stage && fresh ? AdminTheme.infoText : AdminTheme.muted)
            }
        }
    }
    private func detail(_ worker: ApplicationWorker, value: ApplicationWorkflowObservation) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("\(worker.id) · \(stageTitle(worker.stage, known: worker.knownStage))").font(.headline)
                Spacer()
                Text(worker.state == "paused" ? "Application retained" : worker.state == "available" ? "No application assigned" : "Reported worker state").font(.caption).foregroundStyle(AdminTheme.muted)
            }
            Text(workerExplanation(worker)).font(.callout)
            HStack(alignment: .top, spacing: 26) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Stage started: \(worker.stageStartedAt.map(shortTime) ?? "Unknown")")
                    Text("Worker updated: \(worker.updatedAt.map(shortTime) ?? "Unknown")")
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Questions: \(worker.questionIds.isEmpty ? "None linked" : worker.questionIds.joined(separator: ", "))")
                    Text("Hash comparison: \(worker.stage == "checking_hash" ? "checking stored digest" : worker.stage == "refreshing_answers" ? "refreshing answer snapshot" : "no result exposed")")
                }
            }.font(.caption).foregroundStyle(AdminTheme.muted).textSelection(.enabled)
            DisclosureGroup("Observation details") {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Task: \(worker.taskId ?? "Not assigned")")
                    Text("Process: \(worker.processId ?? "Not assigned")")
                    Text("State: \(worker.state.safeDiagnostic) · stage: \(worker.stage.safeDiagnostic)")
                    Text("Server observation: \(value.generatedAt)")
                    Text("Received: \(model.receivedAt?.formatted(date: .abbreviated, time: .standard) ?? "Unknown")")
                    Text("Stored digest: \(value.answerFile.hash ?? "Unknown")").textSelection(.enabled)
                    Text("The API does not report each worker's cached digest or its last comparison result.")
                }.font(.caption).foregroundStyle(AdminTheme.muted).padding(.top, 6)
            }.font(.caption)
        }.padding(18).background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border, lineWidth: 1))
    }
    private func workerExplanation(_ worker: ApplicationWorker) -> String {
        switch worker.stage {
        case "checking_hash": return "This worker is comparing its cached hash with the stored manifest digest under the shared lock."
        case "refreshing_answers": return "This worker is refreshing its answer snapshot. Code computes the delta; this worker judges whether it resolves its blockers."
        case "matching_questions": return "This applying worker is matching its remaining questions against the shared pending pool."
        case "saving_questions": return "The complete known blocker batch is being saved to the pending Markdown pool."
        case "waiting_for_lock": return "This worker is waiting for access to the shared question pool."
        case "waiting_for_browser": return "This worker is queued for the shared Chrome connection. Its application stays assigned while another worker uses Chrome."
        case "paused": return "This application and its checkpoint stay with the worker while other workers can continue."
        case "ready": return "This worker is ready to resume. It is not reported as actively applying."
        case "idle": return worker.state == "available" ? "This slot is available for another application." : "No active stage is reported for this worker."
        case "submitting": return "The backend reports submission in progress. Completion is not yet confirmed."
        case "reconciling": return "The worker is reconciling the application outcome. This view does not establish submission."
        case "completed": return "The backend reports this workflow stage complete. Check the application record for its outcome."
        case "loading_answers": return "This worker is loading the published answered-question Markdown snapshot and its stored hash."
        case "applying": return "This worker is working on its assigned application. Check the shared Chrome connection above for current browser ownership."
        case "error": return "The backend reports a worker error. Check the application record for details."
        default: return "This stage is not recognized. No progress is inferred."
        }
    }
    private var unavailable: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 30)).foregroundStyle(AdminTheme.muted)
            Text(model.phase.title).font(.title3.weight(.semibold))
            Text(model.phase == .unsupported ? "This server does not advertise application-workflow observations. Worker activity will appear here when that capability is available." : "No complete worker observation is available. Refresh after the connection is restored.")
                .foregroundStyle(AdminTheme.muted).frame(maxWidth: 500, alignment: .leading)
        }.padding(30).frame(maxWidth: .infinity, alignment: .leading).background(AdminTheme.surface)
    }
    private func shortTime(_ text: String) -> String { ApplicationWorkflowObservation.date(text)?.formatted(date: .omitted, time: .standard) ?? "Unknown" }
    private func stageTitle(_ stage: String, known: Bool) -> String { known ? stage.readableStatus : "Unknown (\(stage.safeDiagnostic))" }
    private func lockTitle(_ state: String) -> String { ["free", "held"].contains(state) ? state.capitalized : "Unknown" }
    private func lockOwner(_ lock: ApplicationQuestionLock) -> String {
        if lock.state == "free" { return "None" }
        return [lock.ownerKind?.readableStatus, lock.ownerId?.safeDiagnostic].compactMap { $0 }.joined(separator: " · ").nonEmpty ?? "Unknown"
    }
}
private extension String { var nonEmpty: String? { isEmpty ? nil : self } }
