import Foundation
import MacDashboardCore
import ReviewCore

/// Fictional UI states. No connection configuration or credential is read in demo mode.
enum DemoSnapshot {
    static func make(scenario suppliedScenario: String? = nil) throws -> DashboardSnapshot {
        func decode<T: Decodable>(_ type: T.Type, _ object: Any) throws -> T {
            try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: object))
        }
        let now = Date()
        let format = ISO8601DateFormatter()
        func time(_ seconds: Double) -> String { format.string(from: now.addingTimeInterval(seconds)) }
        let scenario = suppliedScenario ?? ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--scenario=") }?.split(separator: "=").last.map(String.init) ?? "active"
        let inboxScenario = ["questions-inbox", "questions-inbox-mixed"].contains(scenario)
        let disabledWorkers = ["workers-paused", "workers-not-configured"].contains(scenario)
        let idle = scenario == "idle" || disabledWorkers
        let workerStatus = scenario == "workers-paused" ? "paused" : scenario == "workers-not-configured" ? "not_configured" : "idle"
        let capabilities: [String: Any] = ["api_version": 1, "worker_enabled": !disabledWorkers, "worker_status": workerStatus, "explicit_apply": true, "questions": true, "feed_delivery": "poll", "question_scopes": ["application"], "remote_access": true]
        let health = try decode(WorkflowHealth.self, ["boot_id": "fictional-boot", "status": "idle", "ready": false, "checked_at": time(-60), "expires_at": NSNull(), "retry_at": NSNull(), "checks": [
            ["id": "storage", "status": "passed", "message": "The local database can be read and written."],
            ["id": "chrome_attachment", "status": "on_demand", "message": "Checked when a browser task starts."],
            ["id": "linkedin_auth", "status": "on_demand", "message": "Checked when a browser task starts."]]])
        var cards: [[String: Any]] = []
        for (index, name) in ["Canopy", "Northstar", "Fieldwork"].enumerated() {
            cards.append(["id": "demo-card-\(index)", "revision": "fictional-revision", "created_at": time(-3600), "content": NSNull(),
                "company": ["id": name.lowercased(), "name": name, "data": [:], "excluded": false, "research_status": "complete", "note": "", "revision": 1],
                "jobs": [["key": "demo-job-\(index)", "company_id": name.lowercased(), "job_id": "DEMO-\(index)", "revision": 1, "decision": "inbox", "note": "", "changed": false,
                          "data": ["title": "Senior Software Engineer, Platform", "location": "San Francisco, CA", "url": "https://example.com/jobs/\(index)", "brief": ["work": "Build reliable infrastructure for product teams.", "fit": "Backend and infrastructure experience.", "gap": "Confirm team scope."]]]]])
        }
        if scenario == "browse-multiple", var first = cards.first, let jobs = first["jobs"] as? [[String: Any]], let base = jobs.first {
            first["jobs"] = ["Senior Software Engineer, Platform", "Staff Software Engineer, Developer Infrastructure", "Senior Software Engineer, Data Systems"].enumerated().map { index, title in
                var job = base
                job["key"] = "demo-multiple-\(index)"
                var data = job["data"] as! [String: Any]
                data["title"] = title
                data["salary"] = "$190,000–$260,000"
                data["arrangement"] = "Hybrid"
                data["requirements"] = ["Design and operate reliable production services.", "Work across infrastructure and product engineering teams."]
                job["data"] = data
                return job
            }
            cards[0] = first
        }
        let feed = try decode(WorkflowFeed.self, ["revision": "demo-feed", "cards": cards, "counts": ["ready_cards": 3, "ready_jobs": scenario == "browse-multiple" ? 5 : 3], "refill": ["low_watermark": 10, "target_cards": 30, "next_allowed_at": NSNull()], "capabilities": capabilities])
        var taskObjects: [[String: Any]] = []
        let definitions = idle ? [("rank", "succeeded")] : [("rank", "running"), ("apply", "running"), ("apply", "waiting_for_user"), ("apply", "queued"), ("explore", "retry_wait"), ("apply", "failed"), ("explore", "succeeded")]
        for (index, entry) in definitions.enumerated() {
            taskObjects.append(["id": "demo-task-\(index)", "kind": entry.0, "status": entry.1, "attempts": entry.1 == "queued" ? 0 : 1, "failure_count": entry.1 == "failed" ? 3 : 0, "max_attempts": 3, "available_at": time(entry.1 == "retry_wait" ? 120 : -1000), "resource": entry.0 == "rank" ? NSNull() : "browser", "resource_quarantined": entry.1 == "failed", "error": entry.1 == "waiting_for_user" ? "questions_pending" : entry.1 == "failed" ? "browser_work_uncertain" as Any : NSNull(), "created_at": time(-3600), "updated_at": time(-Double(index * 90))])
        }
        let instructionScenario = ["instructions-preparation", "instructions-uncertain"].contains(scenario)
        if instructionScenario { taskObjects[2]["error"] = scenario == "instructions-uncertain" ? "submission_unknown" : "tool_approval_required" }
        if scenario == "questions-accepted-blocked" { taskObjects[2]["error"] = "tool_approval_required" }
        if inboxScenario { taskObjects[1]["status"] = "waiting_for_user"; taskObjects[1]["error"] = "questions_pending" }
        let tasks = try decode([WorkflowTask].self, taskObjects)
        var processObjects: [[String: Any]] = []
        if !idle {
            for index in [1, 2, 3, 5] {
                let status = definitions[index].1
                processObjects.append(["id": "demo-process-\(index)", "job_key": "demo-job-\(index)", "task_id": "demo-task-\(index)", "decision_id": "demo-decision-\(index)", "snapshot": ["title": "Senior Software Engineer, Platform", "company_name": [1: "Canopy", 2: "Northstar", 3: "Fieldwork", 5: "Juniper Labs"][index]!, "url": "https://example.com/jobs/\(index)"], "submission_status": index == 3 ? "not_started" : "preparing", "recruiting_status": "awaiting_response", "task_status": status, "revision": 1, "receipt": NSNull(), "block_reason": index == 2 ? "questions_pending" : NSNull(), "form_version": index == 2 ? "demo-form" : NSNull(), "created_at": time(-3600), "updated_at": time(-Double(index * 90)), "events": [["id": index, "process_id": "demo-process-\(index)", "type": index == 2 ? "questions_requested" : "authorized", "actor": "worker", "data": [:], "created_at": time(-Double(index * 90))]]])
            }
        }
        if instructionScenario, let index = processObjects.firstIndex(where: { $0["id"] as? String == "demo-process-2" }) {
            processObjects[index]["block_reason"] = taskObjects[2]["error"]
            if scenario == "instructions-uncertain" { processObjects[index]["submission_status"] = "submission_unknown" }
        }
        if scenario == "questions-accepted-blocked", let index = processObjects.firstIndex(where: { $0["id"] as? String == "demo-process-2" }) {
            processObjects[index]["block_reason"] = "tool_approval_required"
        }
        if inboxScenario {
            processObjects[0]["task_status"] = "waiting_for_user"
            processObjects[0]["form_version"] = "demo-form"
            processObjects[0]["block_reason"] = "questions_pending"
            processObjects[3]["form_version"] = "held-form"
            processObjects[3]["block_reason"] = "browser_work_uncertain"
        }
        let processes = try decode([WorkflowProcess].self, processObjects)
        var questionObjects: [[String: Any]] = idle ? [] : [["id": "demo-question", "process_id": "demo-process-2", "form_version": "demo-form", "revision": 1, "text": "Which office location do you prefer?", "type": "text", "choices": [], "required": true, "scope": "application", "reason": "The current form requires a location.", "answer": NSNull(), "answered_at": NSNull(), "created_at": time(-180), "updated_at": time(-180)]]
        if (["questions", "questions-saved"].contains(scenario) || inboxScenario), let base = questionObjects.first {
            for (id, type, text) in [("demo-single", "single_choice", "Which work arrangement do you prefer?"), ("demo-multi", "multi_choice", "Which teams interest you?")] {
                var question = base
                question["id"] = id; question["type"] = type; question["text"] = text
                question["choices"] = type == "single_choice" ? [["id": "remote", "label": "Remote"], ["id": "hybrid", "label": "Hybrid"]] : [["id": "platform", "label": "Platform"], ["id": "data", "label": "Data infrastructure"]]
                questionObjects.append(question)
            }
            if scenario == "questions-saved" {
                questionObjects[0]["answer"] = "San Francisco"
                questionObjects[0]["answered_at"] = time(-120)
                var unanswered = base
                unanswered["id"] = "demo-unanswered"
                unanswered["text"] = "What is your preferred start date?"
                questionObjects.append(unanswered)
            }
        }
        if instructionScenario { questionObjects = [] }
        if scenario == "questions-accepted-blocked" {
            questionObjects[0]["answer"] = "San Francisco"
            questionObjects[0]["answered_at"] = time(-120)
        }
        if inboxScenario, let base = questionObjects.first {
            questionObjects[0]["pool_question_id"] = "shared-location"
            questionObjects[0]["answer"] = "San Francisco"
            var shared = questionObjects[0]; shared["id"] = "demo-shared-location"; shared["process_id"] = "demo-process-1"
            questionObjects.append(shared)
            questionObjects[1]["pool_question_id"] = "shared-arrangement"
            questionObjects[1]["answer"] = "hybrid"; questionObjects[1]["answered_at"] = time(-120)
            var held = base; held["id"] = "demo-held-question"; held["process_id"] = "demo-process-5"
            held["form_version"] = "held-form"; held["text"] = "What is your preferred start date?"
            questionObjects.append(held)
            if scenario == "questions-inbox-mixed", let index = questionObjects.firstIndex(where: { $0["id"] as? String == "demo-shared-location" }) {
                questionObjects[index]["answered_at"] = time(-60)
            }
        }
        let questions = try decode([WorkflowQuestion].self, questionObjects)
        return DashboardSnapshot(feed: feed, health: health, tasks: tasks, processes: processes, questions: questions, fetchedAt: now.addingTimeInterval(scenario == "stale" ? -180 : 0))
    }
}
