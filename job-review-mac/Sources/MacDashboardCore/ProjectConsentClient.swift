import Foundation
import CoreFoundation
import ReviewCore

/// Only the reviewed policy is supported. A changed policy must not inherit old consent.
public struct ProjectConsent: Codable, Equatable, Sendable {
    public static let policyID = "job-review-browser-resume"
    public static let policyVersion = 1
    public static let scopes = ["browse_job_sources", "upload_approved_resume"]
    public let granted: Bool
    public let grantedAt: String?

    public init(granted: Bool, grantedAt: String? = nil) {
        self.granted = granted
        self.grantedAt = grantedAt
    }
}

public protocol ProjectConsentServing: Sendable {
    func fetch() async throws -> ProjectConsent
    func agree(requestID: String) async throws -> ProjectConsent
}

/// A separate, narrow command client. The dashboard's observation transport remains GET-only.
public actor ProjectConsentClient: ProjectConsentServing, CustomStringConvertible, CustomDebugStringConvertible {
    private let origin: String
    private let credential: String
    private let transport: any ReviewTransport
    private var token: String?
    public nonisolated var description: String { "ProjectConsentClient (authenticated consent only)" }
    public nonisolated var debugDescription: String { description }

    public init(settings: ConnectionSettings, transport: (any ReviewTransport)? = nil) throws {
        origin = settings.origin
        credential = try settings.readCredential()
        self.transport = transport ?? DashboardURLSessionTransport()
    }

    public func fetch() async throws -> ProjectConsent {
        do {
            let object = try await response(request(method: "GET"))
            guard Self.boolean(object["complete"]) == true,
                  object["next_cursor"] is NSNull,
                  let revision = object["revision"] as? String, !revision.isEmpty else {
                throw DashboardError.invalidResponse
            }
            return try consent(object)
        } catch { throw DashboardError.safe(error) }
    }

    public func agree(requestID: String) async throws -> ProjectConsent {
        do {
            guard UUID(uuidString: requestID) != nil, let token, !token.isEmpty else {
                throw DashboardError.configuration
            }
            var command = request(method: "POST")
            command.setValue(origin, forHTTPHeaderField: "Origin")
            command.setValue(token, forHTTPHeaderField: "X-Review-Token")
            command.setValue("application/json", forHTTPHeaderField: "Content-Type")
            command.httpBody = try JSONSerialization.data(withJSONObject: [
                "request_id": requestID, "policy_version": ProjectConsent.policyVersion, "accepted": true
            ])
            let object = try await response(command)
            guard object["request_id"] as? String == requestID,
                  object["status"] as? String == "accepted" else { throw DashboardError.invalidResponse }
            let saved = try consent(object)
            guard saved.granted else { throw DashboardError.invalidResponse }
            return saved
        } catch { throw DashboardError.safe(error) }
    }

    private func request(method: String) -> URLRequest {
        // Neither a caller-provided path nor a destination can enter this command surface.
        var request = URLRequest(url: URL(string: origin + "/api/v1/project-consent")!)
        request.httpMethod = method
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        return request
    }

    private func response(_ request: URLRequest) async throws -> [String: Any] {
        let result = try await transport.send(request)
        guard result.status == 200 else {
            if [401, 403].contains(result.status) { throw DashboardError.authentication }
            throw DashboardError.http(result.status)
        }
        guard result.body.count <= 65_536,
              let object = try JSONSerialization.jsonObject(with: result.body) as? [String: Any],
              Self.integer(object["schema_version"]) == 1,
              let value = object["token"] as? String, !value.isEmpty else {
            throw DashboardError.invalidResponse
        }
        token = value
        return object
    }

    private func consent(_ object: [String: Any]) throws -> ProjectConsent {
        guard let value = object["project_consent"] as? [String: Any],
              value["policy_id"] as? String == ProjectConsent.policyID,
              Self.integer(value["policy_version"]) == ProjectConsent.policyVersion,
              Self.boolean(value["required"]) == true,
              let scopes = value["scopes"] as? [String], scopes.sorted() == ProjectConsent.scopes.sorted(),
              let granted = Self.boolean(value["granted"]) else { throw DashboardError.invalidResponse }
        if !granted {
            guard value["grant"] is NSNull else { throw DashboardError.invalidResponse }
            return ProjectConsent(granted: false)
        }
        guard let grant = value["grant"] as? [String: Any],
              let id = grant["id"] as? String, !id.isEmpty,
              Self.integer(grant["policy_version"]) == ProjectConsent.policyVersion,
              let date = grant["granted_at"] as? String,
              Self.validDate(date) else { throw DashboardError.invalidResponse }
        // Keep no grant payload, credential, CSRF token, or server-provided prose in UI state.
        return ProjectConsent(granted: true, grantedAt: date)
    }

    private static func validDate(_ value: String) -> Bool {
        let format = ISO8601DateFormatter()
        format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if format.date(from: value) != nil { return true }
        format.formatOptions = [.withInternetDateTime]
        return format.date(from: value) != nil
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue == Double(number.intValue) else { return nil }
        return number.intValue
    }
}
