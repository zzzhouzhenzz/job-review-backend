import Foundation
import ReviewCore

/// The Mac uses the same decision and recovery state machine as the iPhone.
public protocol MacWorkflowServing: Sendable {
    func view() async -> WorkflowView
    func refresh() async throws -> WorkflowView
    func decide(card: WorkflowCard, action: String, selected: Set<String>,
                excludeJobKeys: Set<String>, note: String) async throws -> WorkflowView
    func answer(process: WorkflowProcess, questions: [WorkflowQuestion],
                values: [String: JSONValue]) async throws -> WorkflowView
    func cancel(task: WorkflowTask) async throws -> WorkflowView
    func retryPending() async throws -> WorkflowView
    func instructions(taskID: String) async throws -> WorkflowInstructionSnapshot
    func sendInstructions(target: WorkflowInstructionTarget, text: String) async throws -> WorkflowView
}

extension WorkflowSession: MacWorkflowServing {}

public enum MacWorkflowClient {
    public static func make(settings: ConnectionSettings,
                            persistence: (any WorkflowPersistence)? = nil,
                            transport: (any ReviewTransport)? = nil) throws -> any MacWorkflowServing {
        let credential = try settings.readCredential()
        let guarded = MacWorkflowTransport(origin: settings.origin, credential: credential,
                                           underlying: transport ?? DashboardURLSessionTransport())
        let api = WorkflowAPI(address: try ServerAddress(settings.origin), transport: guarded,
                              accessToken: credential)
        return try WorkflowSession(api: api, persistence: persistence ?? PrivateWorkflowPersistence(origin: settings.origin))
    }
}

/// An explicit route boundary prevents future shared API additions from gaining Mac write access.
/// CSRF credentials stay here and in WorkflowSession memory; cached models never receive them.
actor MacWorkflowTransport: ReviewTransport, CustomStringConvertible, CustomDebugStringConvertible {
    static let readPaths: Set<String> = ["/api/v1/capabilities", "/api/v1/feed", "/api/v1/health",
                                         "/api/v1/processes", "/api/v1/questions", "/api/v1/tasks", "/api/v1/question-sync"]
    static let instructionPath = "/api/v1/unblock-instructions"
    static let writePaths: Set<String> = ["/api/v1/decisions", "/api/v1/answers", "/api/v1/tasks/cancel", instructionPath]
    private let origin: String
    private let credential: String
    private let underlying: any ReviewTransport
    private var token: String?
    private var knownTokens: [String] = []
    nonisolated var description: String { "MacWorkflowTransport (authenticated workflow)" }
    nonisolated var debugDescription: String { description }

    init(origin: String, credential: String, underlying: any ReviewTransport) {
        self.origin = origin; self.credential = credential; self.underlying = underlying
    }

    func send(_ request: URLRequest) async throws -> HTTPResult {
        guard let url = request.url, let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.fragment == nil, parts.percentEncodedPath == parts.path,
              URLComponents(string: origin + parts.path).map({ expected in
                  var base = parts; base.query = nil
                  return base == expected
              }) == true,
              request.value(forHTTPHeaderField: "Authorization") == "Bearer \(credential)",
              request.value(forHTTPHeaderField: "Cookie") == nil,
              request.httpBodyStream == nil else { throw DashboardError.configuration }
        switch request.httpMethod ?? "GET" {
        case "GET":
            let instructionRead = parts.path == Self.instructionPath && parts.queryItems?.count == 1 &&
                parts.queryItems?.first?.name == "task_id" &&
                parts.queryItems?.first?.value.map({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0.unicodeScalars.count <= 200 }) == true
            guard (Self.readPaths.contains(parts.path) && parts.query == nil) || instructionRead else {
                throw DashboardError.configuration
            }
            guard request.httpBody == nil,
                  request.value(forHTTPHeaderField: "Origin") == nil,
                  request.value(forHTTPHeaderField: "X-Review-Token") == nil else { throw DashboardError.configuration }
        case "POST":
            guard Self.writePaths.contains(parts.path), parts.query == nil, let token,
                  request.value(forHTTPHeaderField: "Origin") == origin,
                  request.value(forHTTPHeaderField: "X-Review-Token") == token,
                  request.value(forHTTPHeaderField: "Content-Type") == "application/json",
                  let body = request.httpBody, (1...1_000_000).contains(body.count) else {
                throw DashboardError.configuration
            }
        default: throw DashboardError.configuration
        }
        let response: HTTPResult
        do { response = try await underlying.send(request) }
        catch { throw DashboardError.safe(error) }
        guard response.body.count <= 8_000_000 else { throw DashboardError.invalidResponse }

        // The shared API must see status and structured commit guarantees. Converting every
        // rejection into a generic error would break safe recovery after a stale revision.
        guard response.status == 200 else { return try safeFailure(response) }
        guard var object = (try? JSONSerialization.jsonObject(with: response.body)) as? [String: Any],
              let nextToken = object["token"] as? String,
              (1...1_024).contains(nextToken.utf8.count), nextToken != credential,
              nextToken.utf8.allSatisfy({ (33...126).contains($0) }) else {
            throw DashboardError.invalidResponse
        }
        if !knownTokens.contains(nextToken) {
            knownTokens.append(nextToken)
            if knownTokens.count > 64 { knownTokens.removeFirst() }
        }
        // Retain the active token verbatim for the shared API. Redact it from all cached fields,
        // including any server diagnostic that accidentally repeats a credential.
        object.removeValue(forKey: "token")
        object = scrub(object) as! [String: Any]
        object["token"] = nextToken
        guard let body = try? JSONSerialization.data(withJSONObject: object) else { throw DashboardError.invalidResponse }
        token = nextToken
        return HTTPResult(status: response.status, body: body)
    }

    private func safeFailure(_ response: HTTPResult) throws -> HTTPResult {
        let raw = (try? JSONDecoder().decode([String: JSONValue].self, from: response.body)) ?? [:]
        let original = raw["schema_version"] == .number(1) ? raw["error"]?.object ?? [:] : [:]
        func identifier(_ key: String) -> JSONValue {
            guard let value = original[key]?.text, (1...64).contains(value.utf8.count),
                  value.utf8.allSatisfy({ (97...122).contains($0) || (48...57).contains($0) || $0 == 95 }),
                  !value.contains(credential), !knownTokens.contains(where: value.contains) else { return .null }
            return .string(value)
        }
        let safe: [String: JSONValue] = ["schema_version": .number(1), "error": .object([
            "code": identifier("code"), "reason": identifier("reason"), "commit_status": identifier("commit_status"),
            "message": .string("The service returned HTTP \(response.status). A pending request is kept unless its rejection is confirmed.")
        ])]
        return HTTPResult(status: response.status, body: try JSONEncoder().encode(safe))
    }

    private func scrub(_ value: Any) -> Any {
        if var string = value as? String {
            string = string.replacingOccurrences(of: credential, with: "[redacted]")
            for token in knownTokens { string = string.replacingOccurrences(of: token, with: "[redacted]") }
            return string
        }
        if let array = value as? [Any] { return array.map(scrub) }
        if let object = value as? [String: Any] {
            return object.reduce(into: [String: Any]()) { result, entry in
                result[scrub(entry.key) as! String] = scrub(entry.value)
            }
        }
        return value
    }
}
