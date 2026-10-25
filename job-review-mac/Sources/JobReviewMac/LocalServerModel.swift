import Foundation
import Combine
import MacDashboardCore

@MainActor final class LocalServerModel: ObservableObject {
    @Published private(set) var status: LocalServerStatus?
    @Published private(set) var operation: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var error: String?
    var onChange: (() -> Void)?
    private let service: any LocalServerServing
    private var monitor: Task<Void, Never>?
    private var operationError: String?
    var isBusy: Bool { operation != nil }
    var canStart: Bool { !isBusy && !isRefreshing && status?.state == .stopped }
    var canStop: Bool { !isBusy && !isRefreshing && status != nil && status?.state != .stopped }
    var title: String {
        if let operation { return operation == "start" ? "Starting…" : "Stopping…" }
        switch status?.state {
        case .stopped: return "Stopped"
        case .registered: return "Starting or waiting"
        case .running: return "Running"
        case nil: return error == nil ? "Checking…" : "Unavailable"
        }
    }
    var detail: String {
        if operation == "stop" { return "Waiting for the server to finish graceful shutdown." }
        if operation == "start" { return "Loading the existing server. Queued applications can resume." }
        switch status?.state {
        case .stopped: return "Starting resumes queued work, including approved applications."
        case .registered: return "The agent is loaded, but its listener is not ready. Refresh status or stop the server."
        case .running: return "Local process and listener verified. Quitting this app leaves the server running."
        case nil: return "Local service status is separate from the HTTPS connection."
        }
    }

    init(service: any LocalServerServing = LocalServerController()) { self.service = service }
    func beginMonitoring() {
        guard monitor == nil else { return }
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh(clearOperationError: false)
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
            }
        }
    }
    func endMonitoring() { monitor?.cancel(); monitor = nil }
    func refresh(clearOperationError: Bool = true) async {
        guard !isBusy, !isRefreshing else { return }
        if clearOperationError { operationError = nil }
        isRefreshing = true; onChange?()
        defer { isRefreshing = false; onChange?() }
        do { status = try await service.status(); error = operationError }
        catch { status = nil; self.error = error.localizedDescription.safeDiagnostic }
    }
    func startServer() async { guard canStart else { return }; await change("start") }
    func stopServer() async { guard canStop else { return }; await change("stop") }
    private func change(_ action: String) async {
        operation = action; error = nil; operationError = nil; onChange?()
        defer { operation = nil; onChange?() }
        do { status = try await (action == "start" ? service.start() : service.stop()) }
        catch {
            let failure = error.localizedDescription.safeDiagnostic
            operationError = failure
            do { status = try await service.status(); self.error = failure }
            catch { status = nil; self.error = failure + " " + error.localizedDescription.safeDiagnostic }
        }
    }
}

/// Demo controls change sample state only. They never construct a production controller.
actor DemoLocalServer: LocalServerServing {
    private var state: LocalServerState
    private let fails: Bool
    init(scenario: String) { state = scenario == "server-stopped" || scenario == "server-start-error" ? .stopped : .running; fails = scenario == "server-start-error" }
    func status() -> LocalServerStatus { LocalServerStatus(state) }
    func start() throws -> LocalServerStatus {
        if fails { throw DemoServerError() }
        state = .running; return LocalServerStatus(state)
    }
    func stop() -> LocalServerStatus { state = .stopped; return LocalServerStatus(state) }
}
private struct DemoServerError: LocalizedError {
    var errorDescription: String? { "Fictional start failure. The server remains stopped." }
}
