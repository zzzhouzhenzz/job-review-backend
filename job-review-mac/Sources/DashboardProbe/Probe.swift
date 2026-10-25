import Foundation
import MacDashboardCore
import ReviewCore

/// Read-only native URLSession check. Prints counts/status only, never envelopes or diagnostics.
@main struct Probe {
    static func main() async {
        do {
            if ProcessInfo.processInfo.arguments.contains("--local-server-status") {
                let status = try await LocalServerController().status()
                print("{\"local_server\":\"\(status.state.rawValue)\",\"read_only\":true}")
                return
            }
            let settings = try ConnectionSettings.loadDefault()
            if ProcessInfo.processInfo.arguments.contains("--question-sync") {
                let service = try MacWorkflowClient.make(settings: settings, persistence: ProbePersistence())
                let view = try await service.refresh()
                let output: [String: Any] = ["read_only": true,
                    "verified_at": ISO8601DateFormatter().string(from: Date()),
                    "question_sync_supported": view.capabilities?.questionSync == true,
                    "phase": view.questionSyncPhase.rawValue,
                    "revision": view.questionSyncRevision as Any? ?? NSNull(),
                    "pool_revision": view.questionSyncPoolRevision as Any? ?? NSNull(),
                    "has_complete_snapshot": view.questionSyncPoolRevision != nil,
                    "canonical_count": view.sharedQuestions.count,
                    "saved_count": view.sharedQuestions.filter(\.hasAcceptedAnswer).count,
                    "open_count": view.sharedQuestions.filter { $0.status == "open" }.count,
                    "unbound_saved_count": view.sharedQuestions.filter { $0.hasAcceptedAnswer && $0.bindingIDs.isEmpty }.count,
                    "projected_form_count": view.questions.count,
                    "read_errors": view.readErrors.keys.sorted()]
                print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self))
                return
            }
            if ProcessInfo.processInfo.arguments.contains("--application-workflow") {
                let result = try await ApplicationWorkflowClient(settings: settings).fetch()
                var output: [String: Any] = ["read_only": true, "verified_at": ISO8601DateFormatter().string(from: Date())]
                switch result {
                case .unsupported: output["application_workflow_supported"] = false
                case .observed(let value):
                    output["application_workflow_supported"] = true
                    output["revision"] = value.revision; output["generated_at"] = value.generatedAt
                    output["worker_count"] = value.workers.count
                    output["worker_states"] = value.workers.map(\.state)
                    output["worker_stages"] = value.workers.map(\.stage)
                    output["lock_state"] = value.questionLock.state
                    output["answer_handler_stage"] = value.answerHandler.stage
                }
                print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self))
                return
            }
            let consent = try await ProjectConsentClient(settings: settings).fetch()
            let snapshot = try await DashboardClient(settings: settings).fetch()
            if ProcessInfo.processInfo.arguments.contains("--attention") {
                let tasks = snapshot.tasks.filter { ["waiting_for_user", "failed"].contains($0.status) || $0.resourceQuarantined }
                let service = try MacWorkflowClient.make(settings: settings, persistence: ProbePersistence())
                let view = try await service.refresh()
                guard view.online, view.readErrors.isEmpty else { throw DashboardError.invalidResponse }
                var items: [[String: Any]] = []
                for task in tasks {
                    let process = snapshot.processes.first { $0.taskID == task.id }
                    let questions = snapshot.questions.filter { $0.processID == process?.id && $0.formVersion == process?.formVersion }
                    var item: [String: Any] = ["task_id": task.id, "kind": task.kind, "status": task.status,
                        "quarantined": task.resourceQuarantined, "can_cancel": task.canCancel,
                        "has_answer_form": process?.canAnswer ?? false, "current_questions": questions.count,
                        "accepted_answers": questions.filter(\.hasAcceptedAnswer).count,
                        "supported_questions": questions.filter(\.isSupported).count]
                    if let process {
                        item["company"] = process.companyName; item["title"] = process.title
                        item["submission_status"] = process.submissionStatus
                    }
                    // Report machine codes only, never free-form diagnostics or answer values.
                    if let reason = process?.blockReason ?? task.error,
                       reason.range(of: "^[a-z_]{1,80}$", options: .regularExpression) != nil { item["block_reason"] = reason }
                    if view.capabilities?.unblockInstructions == true {
                        let instructions = try await service.instructions(taskID: task.id)
                        item["instructions_read_passed"] = true
                        item["instructions_mode"] = instructions.target.mode
                        item["instructions_supported"] = instructions.target.supportsSubmission
                        item["saved_instruction_count"] = instructions.instructions.count
                    } else { item["instructions_supported"] = false }
                    items.append(item)
                }
                let output: [String: Any] = ["read_only": true, "verified_at": ISO8601DateFormatter().string(from: snapshot.fetchedAt),
                                            "attention_count": items.count, "items": items]
                print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self))
                return
            }
            let output: [String: Any] = [
                "verified_at": ISO8601DateFormatter().string(from: snapshot.fetchedAt),
                "authenticated_https": true,
                "project_consent_granted": consent.granted,
                "worker_enabled": snapshot.feed.capabilities.workerEnabled,
                "health_status": snapshot.health.status,
                "health_checks": snapshot.health.checks.map { ["id": $0.id, "status": $0.status] },
                "ready_cards": snapshot.feed.counts.readyCards,
                "ready_jobs": snapshot.feed.counts.readyJobs,
                "tasks": snapshot.tasks.count,
                "task_status_counts": Dictionary(grouping: snapshot.tasks, by: \.status).mapValues(\.count),
                "processes": snapshot.processes.count,
                "submission_status_counts": Dictionary(grouping: snapshot.processes, by: \.submissionStatus).mapValues(\.count),
                "question_count": snapshot.questions.count
            ]
            let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch {
            fputs("Native read check failed: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}

/// This one-shot diagnostic calls only refresh/instructions and never touches the app's saved drafts.
private struct ProbePersistence: WorkflowPersistence {
    func load() -> SavedWorkflowSession? { nil }
    func save(_ value: SavedWorkflowSession) {} // This one-shot diagnostic has no disk cache.
}
