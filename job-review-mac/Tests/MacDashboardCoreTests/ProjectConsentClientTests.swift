import Foundation
import ReviewCore
import XCTest
@testable import MacDashboardCore

final class ProjectConsentClientTests: XCTestCase {
    private var directory: URL!
    private let bearer = String(repeating: "G", count: 43)
    private let csrf = "fictional-consent-session-token"

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: directory) }
    private func settings() throws -> ConnectionSettings {
        let file = directory.appendingPathComponent("credential.txt")
        try bearer.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return try ConnectionSettings(origin: "https://fixture.example", credentialFile: file.path)
    }
    private func envelope(granted: Bool, requestID: String? = nil, edit: ((inout [String: Any]) -> Void)? = nil) throws -> HTTPResult {
        var value: [String: Any] = [
            "policy_id": ProjectConsent.policyID, "policy_version": 1, "required": true,
            "granted": granted, "scopes": ProjectConsent.scopes,
            "grant": granted ? ["id": "fixture-grant", "policy_version": 1, "granted_at": "2026-10-18T12:00:00Z", "source": "dashboard", "source_reference": "fixture"] : NSNull()
        ]
        edit?(&value)
        var object: [String: Any] = ["schema_version": 1, "token": csrf, "project_consent": value]
        if let requestID { object["request_id"] = requestID; object["status"] = "accepted" }
        else { object["complete"] = true; object["revision"] = "fictional-revision"; object["next_cursor"] = NSNull() }
        return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: object))
    }

    func testFirstUseReadAndExplicitAgreeUseOnlyConsentRouteAndRequiredHeaders() async throws {
        let id = UUID().uuidString
        let transport = ConsentFixtureTransport([try envelope(granted: false), try envelope(granted: true, requestID: id)])
        let client = try ProjectConsentClient(settings: settings(), transport: transport)
        let initial = try await client.fetch()
        XCTAssertFalse(initial.granted)
        let confirmed = try await client.agree(requestID: id)
        XCTAssertTrue(confirmed.granted)
        let requests = await transport.requests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests.map { $0.httpMethod }, ["GET", "POST"])
        for request in requests {
            XCTAssertEqual(request.url?.absoluteString, "https://fixture.example/api/v1/project-consent")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(bearer)")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        }
        XCTAssertNil(requests[0].httpBody)
        XCTAssertNil(requests[0].value(forHTTPHeaderField: "X-Review-Token"))
        XCTAssertEqual(requests[1].value(forHTTPHeaderField: "Origin"), "https://fixture.example")
        XCTAssertEqual(requests[1].value(forHTTPHeaderField: "X-Review-Token"), csrf)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(requests[1].httpBody)) as? [String: Any])
        XCTAssertEqual(Set(body.keys), ["request_id", "policy_version", "accepted"])
        XCTAssertEqual(body["request_id"] as? String, id)
        XCTAssertEqual(body["accepted"] as? Bool, true)
        let encoded = String(decoding: try JSONEncoder().encode(confirmed), as: UTF8.self)
        XCTAssertFalse(encoded.contains(bearer)); XCTAssertFalse(encoded.contains(csrf))
        XCTAssertFalse(String(reflecting: client).contains(bearer))
    }

    func testNewClientReadsExistingAgreementWithoutMutation() async throws {
        let transport = ConsentFixtureTransport([try envelope(granted: true), try envelope(granted: true)])
        for _ in 0..<2 {
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            let value = try await client.fetch()
            XCTAssertTrue(value.granted)
        }
        let requests = await transport.requests
        XCTAssertEqual(requests.map { $0.httpMethod }, ["GET", "GET"])
    }

    func testNoCommandWithoutReadOrValidRequestID() async throws {
        let transport = ConsentFixtureTransport([try envelope(granted: false)])
        let client = try ProjectConsentClient(settings: settings(), transport: transport)
        do { _ = try await client.agree(requestID: UUID().uuidString); XCTFail() }
        catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        _ = try await client.fetch()
        do { _ = try await client.agree(requestID: "../commands"); XCTFail() }
        catch { XCTAssertEqual(error as? DashboardError, .configuration) }
        let count = await transport.requests.count
        XCTAssertEqual(count, 1)
    }

    func testRejectsChangedPolicyScopesAndMissingGrant() async throws {
        let patches: [(inout [String: Any]) -> Void] = [
            { $0["policy_version"] = 2 }, { $0["policy_id"] = "other-policy" },
            { $0["policy_version"] = true }, { $0["required"] = 1 }, { $0["granted"] = 1 },
            { $0["scopes"] = ["browse_job_sources", "upload_approved_resume", "submit_any_job"] },
            { $0["required"] = false }, { $0["grant"] = NSNull() },
            { $0["grant"] = ["id": "fixture", "policy_version": 1, "granted_at": "invalid"] }
        ]
        for patch in patches {
            let transport = ConsentFixtureTransport([try envelope(granted: true, edit: patch)])
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            do { _ = try await client.fetch(); XCTFail() }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }

    func testIncompleteEnvelopeIsRejected() async throws {
        for key in ["schema_version", "token", "complete", "next_cursor", "revision"] {
            let response = try envelope(granted: false)
            var object = try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any])
            object.removeValue(forKey: key)
            let transport = ConsentFixtureTransport([HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: object))])
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            do { _ = try await client.fetch(); XCTFail("Missing \(key)") }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
        for field in ["schema_version", "complete"] {
            let response = try envelope(granted: false)
            var object = try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any])
            object[field] = field == "schema_version" ? true : 1
            let transport = ConsentFixtureTransport([HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: object))])
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            do { _ = try await client.fetch(); XCTFail("Swapped type \(field)") }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }

    func testSaveRequiresMatchingAcknowledgementAndGrantedState() async throws {
        let id = UUID().uuidString
        for result in [try envelope(granted: true, requestID: UUID().uuidString), try envelope(granted: false, requestID: id)] {
            let transport = ConsentFixtureTransport([try envelope(granted: false), result])
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            _ = try await client.fetch()
            do { _ = try await client.agree(requestID: id); XCTFail() }
            catch { XCTAssertEqual(error as? DashboardError, .invalidResponse) }
        }
    }

    func testHTTPFailuresDoNotExposeResponseOrFollowRedirects() async throws {
        for status in [302, 401, 403, 409, 500] {
            let transport = ConsentFixtureTransport([try envelope(granted: false), HTTPResult(status: status, body: Data((bearer + csrf).utf8))])
            let client = try ProjectConsentClient(settings: settings(), transport: transport)
            _ = try await client.fetch()
            do { _ = try await client.agree(requestID: UUID().uuidString); XCTFail() }
            catch {
                XCTAssertEqual(error as? DashboardError, [401, 403].contains(status) ? .authentication : .http(status))
                XCTAssertFalse(error.localizedDescription.contains(bearer)); XCTAssertFalse(error.localizedDescription.contains(csrf))
            }
            let count = await transport.requests.count
            XCTAssertEqual(count, 2)
        }
    }
}

private actor ConsentFixtureTransport: ReviewTransport {
    var responses: [HTTPResult]
    var requests: [URLRequest] = []
    init(_ responses: [HTTPResult]) { self.responses = responses }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !responses.isEmpty else { throw DashboardError.invalidResponse }
        return responses.removeFirst()
    }
}
