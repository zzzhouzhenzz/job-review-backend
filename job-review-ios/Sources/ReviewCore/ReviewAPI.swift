import Foundation

public struct ServerAddress: Equatable, Sendable {
    public let url: URL
    public var origin: String { url.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) }
    public init(_ text: String) throws {
        guard var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(), let host = parts.host?.lowercased(), !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/", parts.url != nil,
              scheme == "https" || (scheme == "http" && ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host)) else {
            throw ReviewError.invalid("Use an HTTPS server address with no path, password, or query. HTTP is allowed only for local development.")
        }
        parts.scheme = scheme; parts.host = host; parts.path = ""
        if (scheme == "https" && parts.port == 443) || (scheme == "http" && parts.port == 80) { parts.port = nil }
        guard let normalized = parts.url else { throw ReviewError.invalid("Invalid server address.") }
        self.url = normalized
    }
}

public struct HTTPResult: Sendable {
    public var status: Int
    public var body: Data
    public init(status: Int, body: Data) { self.status = status; self.body = body }
}

public protocol ReviewTransport: Sendable {
    func send(_ request: URLRequest) async throws -> HTTPResult
}

/// No cookies, disk response cache, or redirects. A redirect must not carry a token to another host.
public final class URLSessionTransport: ReviewTransport, @unchecked Sendable {
    private let session: URLSession
    public init() {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 30
        session = URLSession(configuration: config, delegate: RedirectBlocker(), delegateQueue: nil)
    }
    deinit { session.invalidateAndCancel() }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        let (body, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ReviewError.uncertain("The server did not return an HTTP response.") }
        return HTTPResult(status: http.statusCode, body: body)
    }
}

private final class RedirectBlocker: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                           willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                           completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

public struct ReviewAPI: Sendable {
    public let address: ServerAddress
    private let transport: any ReviewTransport
    private let accessToken: String?
    public init(address: ServerAddress, transport: any ReviewTransport, accessToken: String? = nil) {
        self.address = address; self.transport = transport; self.accessToken = accessToken
    }
    public func fetch() async throws -> ReviewState {
        let result = try await transport.send(request(path: "api/state"))
        return try decode(result)
    }
    public func send(_ pending: PendingSave, sessionToken: String) async throws -> ReviewState {
        guard pending.origin == address.origin else { throw ReviewError.invalid("This save belongs to another server.") }
        var req = request(path: pending.path)
        req.httpMethod = "POST"
        req.httpBody = pending.body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(address.origin, forHTTPHeaderField: "Origin")
        req.setValue(sessionToken, forHTTPHeaderField: "X-Review-Token")
        return try decode(await transport.send(req))
    }
    private func request(path: String) -> URLRequest {
        var req = URLRequest(url: address.url.appendingPathComponent(path), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let accessToken, !accessToken.isEmpty { req.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization") }
        return req
    }
    private func decode(_ result: HTTPResult) throws -> ReviewState {
        if (200...299).contains(result.status) { return try JSONDecoder().decode(ReviewState.self, from: result.body) }
        let message = (try? JSONDecoder().decode([String: String].self, from: result.body)["error"]) ?? "The server returned HTTP \(result.status)."
        if result.status == 409 { throw ReviewError.conflict(message) }
        // An auth/rate-limit failure can reject a retry after the original request committed.
        // Only a definitive operation rejection may clear that original pending request.
        if result.status == 400 { throw ReviewError.rejected(message) }
        if result.status == 401 || result.status == 403 {
            throw ReviewError.uncertain("Refresh the connection or update its access token in Settings. Your pending request is kept. \(message)")
        }
        throw ReviewError.uncertain(message)
    }
}
