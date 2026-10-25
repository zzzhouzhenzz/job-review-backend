import Foundation
import ReviewCore

/// Mac-only diagnostics. No answers, private paths, credentials, or durable command state.
public struct ApplicationWorkflowObservation: Codable, Equatable, Sendable {
    public let revision: Int
    public let generatedAt: String
    public let poolSize: Int
    public let workers: [ApplicationWorker]
    public let questionPool: ApplicationQuestionPool
    public let questionLock: ApplicationQuestionLock
    public let answerFile: ApplicationAnswerFile
    public let answerHandler: ApplicationAnswerHandler
    public let browserAccess: ApplicationBrowserAccess?

    public static func date(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
    public var generatedDate: Date? { Self.date(generatedAt) }
}

public struct ApplicationWorker: Codable, Equatable, Sendable, Identifiable {
    public let workerId: String
    public let state: String
    public let stage: String
    public let processId: String?
    public let taskId: String?
    public let company: String?
    public let jobTitle: String?
    public let stageStartedAt: String?
    public let updatedAt: String?
    public let questionIds: [String]
    public var id: String { workerId }
    public static let states: Set<String> = ["available", "running", "paused", "ready", "stopped", "error"]
    public static let stages: Set<String> = ["idle", "loading_answers", "waiting_for_browser", "applying", "waiting_for_lock", "checking_hash", "refreshing_answers", "matching_questions", "saving_questions", "paused", "ready", "submitting", "reconciling", "completed", "error", "unknown"]
    public var knownState: Bool { Self.states.contains(state) }
    public var knownStage: Bool { Self.stages.contains(stage) && stage != "unknown" }
}
public struct ApplicationQuestionPool: Codable, Equatable, Sendable {
    public let pendingCount: Int
    public let answeredCount: Int
    public let revision: Int
}
public struct ApplicationQuestionLock: Codable, Equatable, Sendable {
    public let state: String
    public let ownerKind: String?
    public let ownerId: String?
    public let acquiredAt: String?
    public let waitingWorkerIds: [String]
}
public struct ApplicationAnswerFile: Codable, Equatable, Sendable {
    public let revision: Int?
    public let hashAlgorithm: String?
    public let hash: String?
}
public struct ApplicationAnswerHandler: Codable, Equatable, Sendable {
    public let stage: String
    public let updatedAt: String?
    public static let stages: Set<String> = ["idle", "formatting", "waiting_for_lock", "saving_answers", "publishing_answers", "checking_blockers", "error", "unknown"]
}

public enum ApplicationWorkflowRead: Equatable, Sendable {
    case unsupported
    case observed(ApplicationWorkflowObservation)
}
public protocol ApplicationWorkflowServing: Sendable {
    func fetch() async throws -> ApplicationWorkflowRead
}

public struct ApplicationWorkflowClient: ApplicationWorkflowServing, CustomStringConvertible, CustomDebugStringConvertible {
    private let origin: String
    private let credential: String
    private let transport: any ReviewTransport
    public var description: String { "ApplicationWorkflowClient (authenticated, read-only)" }
    public var debugDescription: String { description }
    public init(settings: ConnectionSettings, transport: (any ReviewTransport)? = nil) throws {
        origin = settings.origin
        credential = try settings.readCredential()
        self.transport = transport ?? DashboardURLSessionTransport()
    }

    /// Discover on every read. Cached support never authorizes an endpoint on an old server.
    public func fetch() async throws -> ApplicationWorkflowRead {
        do {
            let discovery = try await read(path: "/api/v1/capabilities")
            if discovery.status == 404 { return .unsupported }
            let object = try envelope(discovery)
            guard let capabilities = object["capabilities"]?.object,
                  capabilities["api_version"] == .number(1) else { throw DashboardError.invalidResponse }
            if let support = capabilities["application_workflow"], support != .bool(false), support != .bool(true) {
                throw DashboardError.invalidResponse
            }
            guard capabilities["application_workflow"] == .bool(true) else { return .unsupported }
            try Task.checkCancellation()
            let response = try envelope(await read(path: "/api/v1/application-workflow"))
            guard let value = response["application_workflow"] else { throw DashboardError.invalidResponse }
            return .observed(try Self.decode(value))
        } catch { throw DashboardError.safe(error) }
    }

    private func read(path: String) async throws -> HTTPResult {
        // These are fixed call sites; no arbitrary URL, query, body, or mutation route is accepted.
        guard ["/api/v1/capabilities", "/api/v1/application-workflow"].contains(path),
              let url = URL(string: origin + path) else { throw DashboardError.configuration }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        request.httpMethod = "GET"
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        return try await transport.send(request)
    }

    private func envelope(_ response: HTTPResult) throws -> [String: JSONValue] {
        guard response.status == 200 else {
            throw [401, 403].contains(response.status) ? DashboardError.authentication : DashboardError.http(response.status)
        }
        guard response.body.count <= 2_000_000 else { throw DashboardError.invalidResponse }
        let object = try JSONDecoder().decode([String: JSONValue].self, from: response.body)
        guard object["schema_version"] == .number(1), object["complete"] == .bool(true),
              object["next_cursor"] == .null, object["revision"]?.text?.isEmpty == false,
              let token = object["token"]?.text, !token.isEmpty else { throw DashboardError.invalidResponse }
        func scrub(_ value: JSONValue) -> JSONValue {
            switch value {
            case .string(let text): return .string(text.replacingOccurrences(of: credential, with: "[redacted]").replacingOccurrences(of: token, with: "[redacted]"))
            case .array(let items): return .array(items.map(scrub))
            case .object(let items): return .object(items.mapValues(scrub))
            default: return value
            }
        }
        return object.filter { $0.key != "token" }.mapValues(scrub)
    }

    /// Strict completeness with forward-compatible enum strings. Missing data is never zero/idle.
    static func decode(_ raw: JSONValue) throws -> ApplicationWorkflowObservation {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(ApplicationWorkflowObservation.self, from: JSONEncoder().encode(raw))
        func nullable(_ object: [String: JSONValue], _ keys: [String]) throws {
            guard keys.allSatisfy({ object[$0] != nil }) else { throw DashboardError.invalidResponse }
        }
        func dates(_ values: [String?]) -> Bool { values.compactMap { $0 }.allSatisfy { ApplicationWorkflowObservation.date($0) != nil } }
        guard value.revision >= 0, (1...100).contains(value.poolSize), value.workers.count == value.poolSize,
              ApplicationWorkflowObservation.date(value.generatedAt) != nil,
              Set(value.workers.map(\.id)).count == value.workers.count,
              value.questionPool.pendingCount >= 0, value.questionPool.answeredCount >= 0, value.questionPool.revision >= 0,
              value.answerFile.revision.map({ $0 >= 0 }) ?? true,
              !value.questionLock.state.isEmpty, !value.answerHandler.stage.isEmpty,
              dates([value.questionLock.acquiredAt, value.answerHandler.updatedAt]) else { throw DashboardError.invalidResponse }
        for (index, worker) in value.workers.enumerated() {
            try nullable(raw.object["workers"]!.array[index].object, ["process_id", "task_id", "company", "job_title", "stage_started_at", "updated_at"])
            guard !worker.id.isEmpty, !worker.state.isEmpty, !worker.stage.isEmpty,
                  dates([worker.stageStartedAt, worker.updatedAt]),
                  Set(worker.questionIds).count == worker.questionIds.count,
                  worker.questionIds.allSatisfy({ !$0.isEmpty }) else { throw DashboardError.invalidResponse }
        }
        try nullable(raw.object["question_lock"]!.object, ["owner_kind", "owner_id", "acquired_at"])
        try nullable(raw.object["answer_file"]!.object, ["revision", "hash_algorithm", "hash"])
        try nullable(raw.object["answer_handler"]!.object, ["updated_at"])
        let waiters = value.questionLock.waitingWorkerIds
        guard Set(waiters).count == waiters.count, Set(waiters).isSubset(of: Set(value.workers.map(\.id))) else { throw DashboardError.invalidResponse }
        if let rawAccess = raw.object["browser_access"] {
            guard let access = value.browserAccess else { throw DashboardError.invalidResponse }
            try access.validate(rawAccess)
        }
        return value
    }
}
