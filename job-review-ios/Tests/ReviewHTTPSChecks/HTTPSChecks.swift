import Foundation
import Security
import ReviewCore

private struct CheckFailure: Error { let message: String }
private func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw CheckFailure(message: message) }
}
private func unwrap<T>(_ value: T?, _ message: String) throws -> T {
    guard let value else { throw CheckFailure(message: message) }
    return value
}

private struct Configuration: Decodable {
    let origin: String
    let credentialFile: String
    let caDER: String
    let leafDER: String
    let cacheDirectory: String
    let resultFile: String
}

/// Test-only trust. This target is not linked into the app. Its CA is never installed.
private func verify(_ trust: SecTrust, anchor: SecCertificate, host: String, date: Date = Date()) -> Bool {
    guard SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString)) == errSecSuccess,
          SecTrustSetAnchorCertificates(trust, [anchor] as CFArray) == errSecSuccess,
          SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
          SecTrustSetNetworkFetchAllowed(trust, false) == errSecSuccess,
          SecTrustSetVerifyDate(trust, date as CFDate) == errSecSuccess else { return false }
    return SecTrustEvaluateWithError(trust, nil)
}

private final class FixtureDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let anchor: SecCertificate
    private let lock = NSLock()
    private var observedProtocols: Set<String> = []
    init(anchor: SecCertificate) { self.anchor = anchor }
    func protocols() -> [String] {
        lock.lock(); defer { lock.unlock() }
        return observedProtocols.sorted()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == "localhost",
              let trust = challenge.protectionSpace.serverTrust,
              verify(trust, anchor: anchor, host: challenge.protectionSpace.host) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        lock.lock(); defer { lock.unlock() }
        observedProtocols.formUnion(metrics.transactionMetrics.compactMap(\.networkProtocolName))
    }
}

private final class FixtureTransport: ReviewTransport, @unchecked Sendable {
    let delegate: FixtureDelegate
    private let session: URLSession
    init(anchor: SecCertificate) {
        delegate = FixtureDelegate(anchor: anchor)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 20
        session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    }
    deinit { session.invalidateAndCancel() }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let (body, response) = try await session.data(for: request)
        let http = try unwrap(response as? HTTPURLResponse, "Fixture did not return HTTP")
        return HTTPResult(status: http.statusCode, body: body)
    }
}

/// Deliberately discard one successful response after the real HTTPS POST commits.
private actor LostAcknowledgementTransport: ReviewTransport {
    let base: FixtureTransport
    private var discardNextDecision = true
    private var decisionBodies: [Data] = []
    init(base: FixtureTransport) { self.base = base }
    func bodies() -> [Data] { decisionBodies }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let decision = request.httpMethod == "POST" && request.url?.path == "/api/v1/decisions"
        if decision { decisionBodies.append(try unwrap(request.httpBody, "Decision has no body")) }
        let result = try await base.send(request)
        if decision && discardNextDecision && result.status == 200 {
            discardNextDecision = false
            throw URLError(.networkConnectionLost)
        }
        return result
    }
}

@main private struct HTTPSChecks {
    static func main() async {
        do {
            try require(CommandLine.arguments.count == 2, "Pass the temporary configuration file")
            let file = URL(fileURLWithPath: CommandLine.arguments[1])
            let configuration = try JSONDecoder().decode(Configuration.self, from: Data(contentsOf: file))
            try await run(configuration)
        } catch let failure as CheckFailure {
            fputs("HTTPS check failed: \(failure.message)\n", stderr)
            exit(1)
        } catch {
            // Do not print HTTP objects, request headers, credentials, or opaque server text.
            let code = (error as NSError).code
            fputs("HTTPS check failed with \(type(of: error)) (code \(code)).\n", stderr)
            exit(1)
        }
    }

    static func run(_ config: Configuration) async throws {
        var passed: [String] = []
        func pass(_ name: String) { passed.append(name); print("PASS \(name)") }
        let address = try ServerAddress(config.origin)
        try require(address.url.scheme == "https" && address.url.host == "localhost", "Use the isolated localhost HTTPS fixture")
        let credential = try String(contentsOfFile: config.credentialFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        let ca = try unwrap(SecCertificateCreateWithData(nil, Data(contentsOf: URL(fileURLWithPath: config.caDER)) as CFData), "Invalid fixture CA")
        let leaf = try unwrap(SecCertificateCreateWithData(nil, Data(contentsOf: URL(fileURLWithPath: config.leafDER)) as CFData), "Invalid fixture leaf")
        var trust: SecTrust?
        try require(SecTrustCreateWithCertificates([leaf, ca] as CFArray, SecPolicyCreateSSL(true, "localhost" as CFString), &trust) == errSecSuccess,
                    "Cannot create fixture certificate trust")
        let certificateTrust = try unwrap(trust, "Missing fixture trust")
        try require(verify(certificateTrust, anchor: ca, host: "localhost"), "Valid fixture certificate rejected")
        try require(!verify(certificateTrust, anchor: ca, host: "wrong.example.invalid"), "Wrong hostname was accepted")
        try require(!verify(certificateTrust, anchor: ca, host: "localhost", date: Date().addingTimeInterval(7 * 86400)), "Expired fixture certificate was accepted")
        pass("test CA still enforces hostname and certificate validity")

        do {
            _ = try await WorkflowAPI(address: address, transport: URLSessionTransport(), accessToken: credential).discover()
            throw CheckFailure(message: "Production transport trusted the temporary CA")
        } catch let error as URLError {
            try require([.secureConnectionFailed, .serverCertificateHasBadDate, .serverCertificateUntrusted,
                         .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid].contains(error.code),
                        "Production connection failed for a reason other than certificate validation")
        }
        pass("production URLSessionTransport rejects the untrusted test CA")

        let transport = FixtureTransport(anchor: ca)
        let api = WorkflowAPI(address: address, transport: transport, accessToken: credential)
        let discovery = try unwrap(try await api.discover(), "Workflow discovery fell back to legacy")
        try require(discovery.remoteAccess && !discovery.workerEnabled, "Fixture must be authenticated with worker disabled")
        let legacy = try await ReviewAPI(address: address, transport: transport, accessToken: credential).fetch()
        try require(legacy.jobs.count == 9 && legacy.companies.count == 3, "Legacy HTTPS read lost fictional jobs")
        pass("authenticated production APIs read capabilities and legacy state over HTTPS")

        let paths = ["api/state", "api/v1/capabilities", "api/v1/health", "api/v1/feed", "api/v1/processes", "api/v1/questions", "api/v1/tasks"]
        for token in [nil, String(repeating: "x", count: 43)] as [String?] {
            for path in paths {
                var request = URLRequest(url: address.url.appendingPathComponent(path))
                if let token { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
                let response = try await transport.send(request)
                try require(response.status == 401, "An unauthenticated read was not rejected")
            }
            do {
                _ = try await WorkflowAPI(address: address, transport: transport, accessToken: token).discover()
                throw CheckFailure(message: "Unauthorized discovery succeeded")
            } catch let error as WorkflowHTTPError {
                try require(error.status == 401 && error.code == "unauthorized" && error.commitStatus == "not_committed",
                            "Unauthorized discovery did not preserve the authentication contract")
            }
        }
        pass("missing and wrong credentials receive 401 on every app read endpoint")

        let lossy = LostAcknowledgementTransport(base: transport)
        let sessionAPI = WorkflowAPI(address: address, transport: lossy, accessToken: credential)
        let directory = URL(fileURLWithPath: config.cacheDirectory)
        let persistence = try WorkflowFilePersistence(directory: directory, origin: address.origin)
        let session = try WorkflowSession(api: sessionAPI, persistence: persistence)
        let initial = try await session.refresh()
        try require(initial.online && initial.readErrors.isEmpty && initial.health != nil, "Authenticated snapshots are incomplete")
        let card = try unwrap(initial.feed?.cards.first(where: { $0.company.id == "canopy" }), "Missing fictional Canopy card")
        let selected = try unwrap(card.jobs.first(where: { $0.jobID == "demo-3" }), "Missing selected fictional job")
        try require(card.jobs.count == 3, "Apply fixture needs three jobs to check exact selection")
        pass("WorkflowSession reads feed, processes, questions, tasks, and health through Caddy")

        do {
            _ = try await session.decide(card: card, action: "apply", selected: [selected.key])
            throw CheckFailure(message: "The intentionally lost acknowledgement did not fail")
        } catch let error as URLError {
            try require(error.code == .networkConnectionLost, "Unexpected loss-injection failure")
        }
        let saved = try unwrap(try persistence.load()?.pending, "Lost acknowledgement did not leave durable pending data")
        let original = try JSONDecoder().decode(WorkflowDecisionRequest.self, from: saved.body)
        try require(original.selected == [selected.key], "Apply body changed the exact selected job")
        let reloadedPersistence = try WorkflowFilePersistence(directory: directory, origin: address.origin)
        let restarted = try WorkflowSession(api: sessionAPI, persistence: reloadedPersistence)
        try require(await restarted.view().pending == saved, "Recreated session did not recover the exact pending command")
        pass("lost Apply acknowledgement survives session recreation with exact selected job")

        let replayed = try await restarted.retryPending()
        let bodies = await lossy.bodies()
        try require(bodies.count == 2 && bodies[0] == saved.body && bodies[1] == saved.body, "Apply retry changed request bytes")
        let ack = try unwrap(replayed.lastAcknowledgement, "Missing replay acknowledgement")
        try require(replayed.pending == nil && ack.requestID == original.requestID && ack.processIDs.count == 1,
                    "Apply replay did not acknowledge the original one-job command")
        let applied = try unwrap(replayed.processes.first(where: { $0.id == ack.processIDs[0] }), "Missing Apply process")
        try require(applied.jobKey == selected.key && applied.taskStatus == "queued" && applied.submissionStatus == "not_started",
                    "Queued Apply was confused with employer submission")
        pass("identical Apply replay creates one queued process and clears pending data")

        let questionProcess = try unwrap(replayed.processes.first(where: { $0.canAnswer }), "Missing seeded question round")
        let questions = replayed.questions.filter { $0.processID == questionProcess.id }
        let values: [String: JSONValue] = ["https-text": .string("Fictional HTTPS answer."),
                                          "https-single": .string("north"),
                                          "https-multi": .array([.string("infra"), .string("tools")])]
        try require(questions.count == 3, "Expected three typed questions")
        let answered = try await restarted.answer(process: questionProcess, questions: questions, values: values)
        let resumed = try unwrap(answered.processes.first(where: { $0.id == questionProcess.id }), "Answer process disappeared")
        try require(resumed.taskID == questionProcess.taskID && resumed.taskStatus == "queued" && resumed.submissionStatus == "preparing",
                    "Typed answers did not resume the same task")
        let answerID = try unwrap(answered.lastAcknowledgement?.requestID, "Missing answer acknowledgement")
        for question in answered.questions.filter({ $0.processID == questionProcess.id }) {
            try require(question.answer == values[question.id], "A saved typed answer changed")
        }
        pass("text, single choice, and multiple choice answers resume the existing task")

        let task = try unwrap(answered.tasks.first(where: { $0.id == applied.taskID }), "Missing cancellable Apply task")
        let cancelled = try await restarted.cancel(task: task)
        let finalProcess = try unwrap(cancelled.processes.first(where: { $0.id == applied.id }), "Cancelled process disappeared")
        try require(finalProcess.taskStatus == "cancelled" && finalProcess.submissionStatus == "not_started", "Cancellation status is wrong")
        let cancelID = try unwrap(cancelled.lastAcknowledgement?.requestID, "Missing cancel acknowledgement")
        pass("HTTPS cancellation stops only the selected queued application task")

        let cache = try Data(contentsOf: persistence.file)
        try require(cache.range(of: Data(credential.utf8)) == nil, "Durable workflow cache contains bearer credential")
        if let token = legacy.token { try require(cache.range(of: Data(token.utf8)) == nil, "Durable workflow cache contains session token") }
        pass("durable workflow cache contains neither bearer nor session credential")
        let protocols = transport.delegate.protocols()
        try require(!protocols.isEmpty, "URLSession did not report network protocol metrics")
        print("Observed URLSession protocols: " + protocols.joined(separator: ", "))
        let result: [String: Any] = ["checks": passed, "protocols": protocols, "selected_job_key": selected.key,
                                   "apply_request_id": original.requestID, "apply_process_id": applied.id,
                                   "answer_request_id": answerID, "question_process_id": questionProcess.id,
                                   "cancel_request_id": cancelID, "apply_body_base64": saved.body.base64EncodedString()]
        try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
            .write(to: URL(fileURLWithPath: config.resultFile), options: .atomic)
        print("\(passed.count)/\(passed.count) Swift HTTPS checks passed")
    }
}
