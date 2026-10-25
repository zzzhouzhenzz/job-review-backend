import Foundation

public enum DemoData {
    public static let origin = "https://demo.job-review.invalid"
    public static func state() throws -> ReviewState {
        guard let file = Bundle.module.url(forResource: "demo-state", withExtension: "json") else { throw ReviewError.storage("The demo data is missing.") }
        return try JSONDecoder().decode(ReviewState.self, from: Data(contentsOf: file))
    }
}

/// In-memory sample server. It never makes network requests or sends applications.
public actor DemoTransport: ReviewTransport {
    private var state: ReviewState
    private var queueRequests = Set<String>()
    public init(state: ReviewState) { self.state = state }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        guard request.url?.host == "demo.job-review.invalid" else { throw ReviewError.invalid("Invalid demo destination.") }
        if request.httpMethod == "POST" {
            guard let body = request.httpBody else { throw ReviewError.invalid("Empty demo request.") }
            if request.url?.path == "/api/card" {
                let card = try JSONDecoder().decode(CardRequest.self, from: body)
                if !state.cardReviews.contains(where: { $0.id == card.requestID }) {
                    guard let current = state.flashcards.first(where: { $0.companyID == card.companyID }), current.revision == card.revision,
                          current.jobs.map(JobVersion.init) == card.jobs else { return error(409, "This sample card changed. Review it again.") }
                    if card.action == .exclude, state.queue.contains(where: { $0.companyID == card.companyID && ["in_progress", "needs_input"].contains($0.status) }) {
                        return error(409, "Resolve started work before excluding this company.")
                    }
                    for index in state.jobs.indices where card.selected.contains(state.jobs[index].key) {
                        state.jobs[index].decision = "approve"; state.jobs[index].revision += 1
                    }
                    if let index = state.companies.firstIndex(where: { $0.id == card.companyID }) {
                        state.companies[index].revision += 1
                        if card.action == .exclude {
                            state.companies[index].excluded = true
                            for jobIndex in state.jobs.indices where state.jobs[jobIndex].companyID == card.companyID && state.jobs[jobIndex].decision == "approve" {
                                state.jobs[jobIndex].decision = "pending"
                            }
                            for queueIndex in state.queue.indices where state.queue[queueIndex].companyID == card.companyID && state.queue[queueIndex].status == "queued" {
                                state.queue[queueIndex].status = "cancelled"
                            }
                        }
                        if card.action == .research {
                            state.companies[index].researchStatus = "requested"
                            state.queue.insert(WorkItem(id: card.requestID, kind: "research", companyID: card.companyID, jobKey: nil, status: "queued", snapshot: [:], createdAt: stamp, note: card.note), at: 0)
                        }
                    }
                    state.flashcards.removeAll { $0.companyID == card.companyID }
                    state.cardReviews.insert(ReviewRecord(id: card.requestID, companyID: card.companyID, action: card.action.rawValue, selected: card.selected, createdAt: stamp), at: 0)
                }
            } else if request.url?.path == "/api/queue" {
                let queue = try JSONDecoder().decode(QueueRequest.self, from: body)
                // Derive IDs from the request so a sample restart also recognizes a replay.
                if !queueRequests.contains(queue.requestID), !state.queue.contains(where: { $0.id.hasPrefix(queue.requestID + ":") }) {
                    let ready = state.readyJobs
                    guard queue.jobs.allSatisfy({ version in ready.contains { $0.key == version.key && $0.revision == version.revision } }) else { return error(409, "Some sample jobs are no longer ready.") }
                    for (index, version) in queue.jobs.enumerated() {
                        guard let job = ready.first(where: { $0.key == version.key }) else { continue }
                        state.queue.insert(WorkItem(id: queue.requestID + ":\(index)", kind: "application", companyID: job.companyID, jobKey: job.key, status: "queued", snapshot: job.data, createdAt: stamp, note: "Demo only. No application is sent."), at: 0)
                    }
                    queueRequests.insert(queue.requestID)
                }
            } else { return error(404, "Unknown sample action.") }
        }
        state.token = "demo-session"
        state.counts["ready"] = state.readyJobs.count
        state.counts["flashcards"] = state.flashcards.count
        state.counts["reviewed_cards"] = state.cardReviews.count
        state.counts["queue"] = state.activeWork.count
        state.counts["excluded"] = state.companies.filter(\.excluded).count
        return HTTPResult(status: 200, body: try JSONEncoder().encode(state))
    }
    private var stamp: String { ISO8601DateFormatter().string(from: Date()) }
    private func error(_ status: Int, _ message: String) -> HTTPResult {
        HTTPResult(status: status, body: (try? JSONEncoder().encode(["error": message])) ?? Data())
    }
}
