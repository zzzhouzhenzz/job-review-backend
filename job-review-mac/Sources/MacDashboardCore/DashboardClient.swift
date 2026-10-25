import Foundation
import ReviewCore

/// Endpoint reads are complete but are not one atomic backend transaction.
public struct DashboardSnapshot: Codable, Equatable, Sendable {
    public let feed: WorkflowFeed
    public let health: WorkflowHealth
    public let tasks: [WorkflowTask]
    public let processes: [WorkflowProcess]
    public let questions: [WorkflowQuestion]
    public let fetchedAt: Date

    public init(feed: WorkflowFeed, health: WorkflowHealth, tasks: [WorkflowTask],
                processes: [WorkflowProcess], questions: [WorkflowQuestion], fetchedAt: Date) {
        self.feed = feed
        self.health = health
        self.tasks = tasks
        self.processes = processes
        self.questions = questions
        self.fetchedAt = fetchedAt
    }
}

public struct DashboardClient: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    private let api: WorkflowAPI
    public var description: String { "DashboardClient (authenticated, read-only)" }
    public var debugDescription: String { description }

    public init(settings: ConnectionSettings, transport: (any ReviewTransport)? = nil) throws {
        let credential = try settings.readCredential()
        let safeTransport = DashboardReadTransport(origin: settings.origin, credential: credential,
                                                   underlying: transport ?? DashboardURLSessionTransport())
        api = WorkflowAPI(address: try ServerAddress(settings.origin), transport: safeTransport, accessToken: credential)
    }

    /// A failed endpoint rejects the entire refresh; callers can retain an older snapshot as stale.
    public func fetch() async throws -> DashboardSnapshot {
        do {
            async let feed = api.fetchFeed().value
            async let health = api.fetchHealth()?.value
            async let tasks = api.fetchTasks().value
            async let processes = api.fetchProcesses().value
            async let questions = api.fetchQuestions().value
            let (feedValue, healthValue, taskValues, processValues, questionValues) =
                try await (feed, health, tasks, processes, questions)
            guard let healthValue else { throw DashboardError.invalidResponse }
            try Task.checkCancellation()
            return DashboardSnapshot(feed: feedValue, health: healthValue, tasks: taskValues,
                                     processes: processValues, questions: questionValues, fetchedAt: Date())
        } catch { throw DashboardError.safe(error) }
    }
}

/// Gates every request even when the shared API grows new commands in the future.
struct DashboardReadTransport: ReviewTransport {
    static let paths: Set<String> = ["/api/v1/feed", "/api/v1/health", "/api/v1/tasks", "/api/v1/processes", "/api/v1/questions"]
    let origin: String
    let credential: String
    let underlying: any ReviewTransport

    func send(_ request: URLRequest) async throws -> HTTPResult {
        guard let url = request.url, let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              (request.httpMethod ?? "GET") == "GET", request.httpBody == nil, request.httpBodyStream == nil,
              Self.paths.contains(parts.path), parts.query == nil, parts.fragment == nil,
              url.absoluteString == origin + parts.path,
              request.value(forHTTPHeaderField: "Authorization") == "Bearer \(credential)",
              request.value(forHTTPHeaderField: "X-Review-Token") == nil else { throw DashboardError.configuration }
        let response: HTTPResult
        do { response = try await underlying.send(request) }
        catch { throw DashboardError.safe(error) }
        guard response.status == 200 else {
            if [401, 403].contains(response.status) { throw DashboardError.authentication }
            throw DashboardError.http(response.status)
        }
        guard response.body.count <= 8_000_000 else { throw DashboardError.invalidResponse }
        // The shared decoder validates the full envelope. Replace its ephemeral CSRF token before
        // decoding, and redact known secrets if a backend diagnostic accidentally includes them.
        guard var object = (try? JSONSerialization.jsonObject(with: response.body)) as? [String: Any],
              let token = object["token"] as? String, !token.isEmpty else { throw DashboardError.invalidResponse }
        func scrub(_ value: Any) -> Any {
            if let string = value as? String {
                return string.replacingOccurrences(of: credential, with: "[redacted]")
                    .replacingOccurrences(of: token, with: "[redacted]")
            }
            if let values = value as? [Any] { return values.map(scrub) }
            if let values = value as? [String: Any] { return values.mapValues(scrub) }
            return value
        }
        object = scrub(object) as! [String: Any]
        object["token"] = "discarded-read-only-token"
        guard let body = try? JSONSerialization.data(withJSONObject: object) else { throw DashboardError.invalidResponse }
        return HTTPResult(status: 200, body: body)
    }
}

final class DashboardURLSessionTransport: ReviewTransport, @unchecked Sendable {
    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 20
        configuration.waitsForConnectivity = false
        session = URLSession(configuration: configuration, delegate: DashboardRedirectBlocker(), delegateQueue: nil)
    }

    deinit { session.invalidateAndCancel() }

    func send(_ request: URLRequest) async throws -> HTTPResult {
        var bounded = request
        bounded.timeoutInterval = 15
        let (body, response) = try await session.data(for: bounded)
        guard let http = response as? HTTPURLResponse else { throw DashboardError.invalidResponse }
        return HTTPResult(status: http.statusCode, body: body)
    }
}

final class DashboardRedirectBlocker: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
