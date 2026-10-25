import Foundation

/// Preserve source fields even when the research schema grows.
public enum JSONValue: Codable, Equatable, Sendable {
    case string(String), number(Double), bool(Bool), array([JSONValue]), object([String: JSONValue]), null
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let value = try? c.decode(Bool.self) { self = .bool(value) }
        else if let value = try? c.decode(Double.self) { self = .number(value) }
        else if let value = try? c.decode(String.self) { self = .string(value) }
        else if let value = try? c.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    public var text: String? { if case .string(let s) = self { return s }; return nil }
    public var object: [String: JSONValue] { if case .object(let o) = self { return o }; return [:] }
    public var array: [JSONValue] { if case .array(let a) = self { return a }; return [] }
}

public struct Company: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var data: [String: JSONValue]
    public var excluded: Bool
    public var researchStatus: String
    public var note: String
    public var revision: Int
    enum CodingKeys: String, CodingKey {
        case id, name, data, excluded, note, revision
        case researchStatus = "research_status"
    }
}

public struct Job: Codable, Equatable, Identifiable, Sendable {
    public var key: String
    public var companyID: String
    public var jobID: String
    public var data: [String: JSONValue]
    public var revision: Int
    public var decision: String
    public var note: String
    public var changed: Bool
    public var id: String { key }
    public var title: String { field("title", fallback: "Untitled job") }
    public var location: String { field("location", fallback: "Location not stated") }
    public var brief: [String: JSONValue] { data["brief"]?.object ?? [:] }
    public func field(_ key: String, fallback: String = "Not stated") -> String { data[key]?.text ?? fallback }
    public func short(_ key: String, fallback: String = "Not stated") -> String { brief[key]?.text ?? fallback }
    public var officialURL: URL? { Self.safeURL(data["url"]?.text) }
    public static func safeURL(_ text: String?) -> URL? {
        guard let text, let url = URL(string: text), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil else { return nil }
        return url
    }
    enum CodingKeys: String, CodingKey {
        case key, data, revision, decision, note, changed
        case companyID = "company_id", jobID = "job_id"
    }
}

public struct FlashCard: Codable, Equatable, Identifiable, Sendable {
    public var companyID: String
    public var revision: Int
    public var jobs: [Job]
    public var remainingJobs: Int
    /// Versions form the view identity. A refresh must clear picks for a changed cohort.
    public var id: String { companyID + ":\(revision):" + jobs.map { "\($0.key):\($0.revision)" }.joined(separator: "|") }
    enum CodingKeys: String, CodingKey {
        case revision, jobs
        case companyID = "company_id", remainingJobs = "remaining_jobs"
    }
}

public struct WorkItem: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var kind: String
    public var companyID: String
    public var jobKey: String?
    public var status: String
    public var snapshot: [String: JSONValue]
    public var createdAt: String
    public var note: String
    public var blocksNewApplication: Bool { ["queued", "in_progress", "needs_input", "applied"].contains(status) }
    public var displayStatus: String {
        ["queued": "Queued", "in_progress": "In progress", "needs_input": "Needs your input",
         "applied": "Applied", "done": "Done", "failed": "Failed", "stale": "Posting changed",
         "cancelled": "Cancelled"][status] ?? status
    }
    enum CodingKeys: String, CodingKey {
        case id, kind, status, snapshot, note
        case companyID = "company_id", jobKey = "job_key", createdAt = "created_at"
    }
}

public struct ReviewRecord: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var companyID: String
    public var action: String
    public var selected: [String]
    public var createdAt: String
    enum CodingKeys: String, CodingKey {
        case id, action, selected
        case companyID = "company_id", createdAt = "created_at"
    }
}

public struct ReviewState: Codable, Equatable, Sendable {
    public var token: String?
    public var companies: [Company]
    public var jobs: [Job]
    public var queue: [WorkItem]
    public var counts: [String: Int]
    public var flashcards: [FlashCard]
    public var cardReviews: [ReviewRecord]
    enum CodingKeys: String, CodingKey {
        case token, companies, jobs, queue, counts, flashcards
        case cardReviews = "card_reviews"
    }
    public func company(_ id: String) -> Company? { companies.first { $0.id == id } }
    public var readyJobs: [Job] {
        jobs.filter { job in
            job.decision == "approve" && !job.changed && company(job.companyID)?.excluded == false &&
            !queue.contains { $0.jobKey == job.key && $0.blocksNewApplication }
        }
    }
    public var activeWork: [WorkItem] { queue.filter { ["queued", "in_progress", "needs_input"].contains($0.status) } }
    public var withoutToken: ReviewState { var copy = self; copy.token = nil; return copy }
}

public struct JobVersion: Codable, Equatable, Sendable {
    public var key: String
    public var revision: Int
    public init(_ job: Job) { key = job.key; revision = job.revision }
}

public enum CardAction: String, Codable, Sendable { case approve, research, exclude }

public struct CardRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var companyID: String
    public var revision: Int
    public var jobs: [JobVersion]
    public var action: CardAction
    public var selected: [String]
    public var note: String
    public init(card: FlashCard, action: CardAction, selected: Set<String>, note: String = "", requestID: String = UUID().uuidString) throws {
        let keys = Set(card.jobs.map(\.key))
        guard (1...3).contains(card.jobs.count), keys.count == card.jobs.count else { throw ReviewError.invalid("Load the current card first.") }
        guard action == .approve ? (!selected.isEmpty && selected.isSubset(of: keys)) : selected.isEmpty else {
            throw ReviewError.invalid("Select at least one job to approve. Other actions do not approve jobs.")
        }
        self.requestID = requestID; companyID = card.companyID; revision = card.revision
        jobs = card.jobs.map(JobVersion.init); self.action = action
        self.selected = selected.sorted(); self.note = note
    }
    enum CodingKeys: String, CodingKey {
        case revision, jobs, action, selected, note
        case requestID = "request_id", companyID = "company_id"
    }
}

public struct QueueRequest: Codable, Equatable, Sendable {
    public var requestID: String
    public var jobs: [JobVersion]
    public init(jobs: [Job], requestID: String = UUID().uuidString) throws {
        guard !jobs.isEmpty, Set(jobs.map(\.key)).count == jobs.count else { throw ReviewError.invalid("Select jobs to queue.") }
        self.requestID = requestID; self.jobs = jobs.map(JobVersion.init)
    }
    enum CodingKeys: String, CodingKey { case jobs; case requestID = "request_id" }
}

public enum ReviewError: Error, LocalizedError, Equatable {
    case invalid(String), offline, pending, conflict(String), rejected(String), uncertain(String), storage(String)
    public var errorDescription: String? {
        switch self {
        case .invalid(let s), .conflict(let s), .rejected(let s), .uncertain(let s), .storage(let s): return s
        case .offline: return "You are viewing a saved copy. Connect and refresh before making a decision."
        case .pending: return "Check the pending save before making another decision."
        }
    }
}
