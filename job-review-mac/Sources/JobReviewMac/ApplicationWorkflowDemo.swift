import Foundation
import MacDashboardCore

/// Fixed examples. These never contact a server or advance on a timer.
enum ApplicationWorkflowDemo {
    static func make(scenario: String = "workflow", date: Date = Date()) throws -> ApplicationWorkflowObservation {
        let stamp = ISO8601DateFormatter().string(from: date)
        let null = NSNull()
        var workers: [[String: Any]] = [
            ["worker_id": "apply-1", "state": "running", "stage": "applying", "process_id": "sample-process-1", "task_id": "sample-task-1", "company": "Northstar Labs", "job_title": "Senior Infrastructure Engineer", "stage_started_at": stamp, "updated_at": stamp, "question_ids": []],
            ["worker_id": "apply-2", "state": "paused", "stage": "paused", "process_id": "sample-process-2", "task_id": "sample-task-2", "company": "Cedar Systems", "job_title": "Staff Backend Engineer", "stage_started_at": stamp, "updated_at": stamp, "question_ids": ["question-7", "question-9"]],
            ["worker_id": "apply-3", "state": "running", "stage": "checking_hash", "process_id": "sample-process-3", "task_id": "sample-task-3", "company": "Orbit Research", "job_title": "Platform Engineer", "stage_started_at": stamp, "updated_at": stamp, "question_ids": ["question-7"]]
        ]
        var lock: [String: Any] = ["state": "held", "owner_kind": "worker", "owner_id": "apply-3", "acquired_at": stamp, "waiting_worker_ids": []]
        var handler = "formatting"
        if scenario == "workflow-idle" {
            workers = (1...3).map { ["worker_id": "apply-\($0)", "state": "available", "stage": "idle", "process_id": null, "task_id": null, "company": null, "job_title": null, "stage_started_at": null, "updated_at": stamp, "question_ids": []] }
            lock = ["state": "free", "owner_kind": null, "owner_id": null, "acquired_at": null, "waiting_worker_ids": []]; handler = "idle"
        } else if scenario == "workflow-publishing" {
            workers[0]["stage"] = "waiting_for_lock"
            workers[2]["stage"] = "waiting_for_lock"
            lock = ["state": "held", "owner_kind": "answer_handler", "owner_id": "answer-handler", "acquired_at": stamp, "waiting_worker_ids": ["apply-1", "apply-3"]]; handler = "publishing_answers"
        } else if scenario == "workflow-unknown" {
            workers[0]["state"] = "future_state"; workers[0]["stage"] = "future_stage"
            lock["state"] = "unknown"; handler = "unknown"
        }
        var object: [String: Any] = ["revision": 42, "generated_at": stamp, "pool_size": 3, "workers": workers,
            "question_pool": ["pending_count": 2, "answered_count": 18, "revision": 12], "question_lock": lock,
            "answer_file": ["revision": 8, "hash_algorithm": "blake3", "hash": String(repeating: "a1b2c3d4", count: 8)],
            "answer_handler": ["stage": handler, "updated_at": stamp]]
        if scenario.hasPrefix("workflow-browser") {
            func ticket(_ index: Int, _ state: String) -> [String: Any] {
                ["ticket_id": "sample-ticket-\(index)", "task_id": "sample-task-\(index)", "worker_id": "apply-\(index)", "role": "apply",
                 "enqueued_at": stamp, "acquired_at": state == "waiting" ? null : stamp, "started_at": state == "active" ? stamp : null, "state": state]
            }
            var access: [String: Any] = ["capacity": 1, "policy": "chrome_fifo_v1", "revision": 4, "state": "busy",
                "owner": ticket(1, "active"), "queue": [ticket(2, "waiting"), ticket(3, "waiting")], "blocked_by": []]
            for index in [1, 2] { workers[index]["state"] = "running"; workers[index]["stage"] = "waiting_for_browser" }
            if scenario == "workflow-browser-blocked" {
                access["state"] = "blocked"; access["owner"] = null; access["queue"] = []
                access["blocked_by"] = [["task_id": "sample-task-2", "resource": "browser"]]
                for index in workers.indices { workers[index]["state"] = "paused"; workers[index]["stage"] = "reconciling" }
            } else if scenario == "workflow-browser-unknown" {
                access["state"] = "future_state"; access["policy"] = "future_policy"
            }
            object["browser_access"] = access; object["workers"] = workers
        }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ApplicationWorkflowObservation.self, from: JSONSerialization.data(withJSONObject: object))
    }
}
