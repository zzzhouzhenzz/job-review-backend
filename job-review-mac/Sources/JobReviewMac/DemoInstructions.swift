import Foundation
import ReviewCore

extension DemoSnapshot {
    static func instructions(taskID: String, mode suppliedMode: String? = nil) throws -> WorkflowInstructionSnapshot {
        let scenario = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--scenario=") } ?? ""
        let mode = suppliedMode ?? (scenario.contains("instructions-preparation") ? "preparation_retry" : scenario.contains("instructions-uncertain") ? "reconciliation_only" :
            taskID == "demo-task-2" ? "questions_pending" : "browser_recheck")
        let date = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-120))
        let target: [String: Any] = ["task_id": taskID, "task_updated_at": date, "task_status": "waiting_for_user",
            "process_id": "demo-process-2", "process_revision": 1, "block_reason": mode == "questions_pending" ? "questions_pending" : mode == "reconciliation_only" ? "submission_unknown" : "tool_approval_required",
            "can_submit": true, "mode": mode, "unavailable_reason": NSNull()]
        let history: [[String: Any]] = [["id": "demo-instruction", "request_id": "demo-instruction-request", "task_id": taskID,
            "task_updated_at": date, "process_id": "demo-process-2", "process_revision": 1,
            "text": "Please check the existing browser tab after I finish sign-in.", "mode": mode,
            "wake_status": mode == "preparation_retry" ? "retry_scheduled" : mode == "browser_recheck" ? "recheck_scheduled" : "saved_only", "created_at": date, "status": "current"]]
        return try JSONDecoder().decode(WorkflowInstructionSnapshot.self,
            from: JSONSerialization.data(withJSONObject: ["target": target, "instructions": history]))
    }

    static func savedInstructions(snapshot: WorkflowInstructionSnapshot, text: String) throws -> (WorkflowInstructionSnapshot, WorkflowAcknowledgement) {
        var next = snapshot
        for index in next.instructions.indices { next.instructions[index].status = "superseded" }
        let id = UUID().uuidString, request = UUID().uuidString
        let wake = snapshot.target.mode == "preparation_retry" ? "retry_scheduled" : snapshot.target.mode == "browser_recheck" ? "recheck_scheduled" : "saved_only"
        let date = ISO8601DateFormatter().string(from: Date())
        let entry: [String: Any] = ["id": id, "request_id": request, "task_id": snapshot.target.taskID,
            "task_updated_at": snapshot.target.taskUpdatedAt, "process_id": snapshot.target.processID as Any? ?? NSNull(),
            "process_revision": snapshot.target.processRevision as Any? ?? NSNull(), "text": text, "mode": snapshot.target.mode,
            "wake_status": wake, "created_at": date, "status": "current"]
        next.instructions.append(try JSONDecoder().decode(WorkflowInstruction.self, from: JSONSerialization.data(withJSONObject: entry)))
        next.target.taskUpdatedAt = date
        let receipt: [String: Any] = ["schema_version": 1, "accepted": true, "request_id": request, "instruction_id": id,
            "task_ids": [snapshot.target.taskID], "process_ids": snapshot.target.processID.map { [$0] } ?? [],
            "mode": snapshot.target.mode, "wake_status": wake, "task_updated_at": date]
        return (next, try JSONDecoder().decode(WorkflowAcknowledgement.self, from: JSONSerialization.data(withJSONObject: receipt)))
    }
}
