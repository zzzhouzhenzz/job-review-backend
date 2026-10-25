import Combine
import Foundation
import MacDashboardCore

enum ApplicationWorkflowPhase: Equatable {
    case loading, observed, stale, unsupported, disconnected
    var title: String {
        switch self {
        case .loading: return "Checking support"
        case .observed: return "Live observation"
        case .stale: return "Last observation · stale"
        case .unsupported: return "Not supported by this server"
        case .disconnected: return "Observation unavailable"
        }
    }
}

@MainActor final class ApplicationWorkflowModel: ObservableObject {
    @Published private(set) var observation: ApplicationWorkflowObservation?
    @Published private(set) var receivedAt: Date?
    @Published private(set) var error: String?
    @Published private(set) var unsupported = false
    @Published private(set) var isRefreshing = false
    @Published var selectedWorkerID: String?
    @Published var now = Date()
    let fictional: Bool
    private let injectedService: (any ApplicationWorkflowServing)?
    private var connection: ConnectionSettings?
    init(service: (any ApplicationWorkflowServing)? = nil, fictional: Bool = false) {
        injectedService = service; self.fictional = fictional
    }
    var phase: ApplicationWorkflowPhase {
        if unsupported { return .unsupported }
        guard let observation, let receivedAt, let generatedDate = observation.generatedDate else { return error == nil ? .loading : .disconnected }
        // A recent successful request must not make an old server observation look live.
        let age = now.timeIntervalSince(generatedDate)
        return error != nil || now.timeIntervalSince(receivedAt) > 15 || age > 15 || age < -30 ? .stale : .observed
    }
    var selectedWorker: ApplicationWorker? {
        observation?.workers.first { $0.id == selectedWorkerID } ?? observation?.workers.first
    }
    func monitor() async {
        guard !fictional else { return }
        // Keep freshness advancing even while a network read is waiting for its timeout.
        let ticker = Task {
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                now = Date()
            }
        }
        defer { ticker.cancel() }
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
        }
    }
    func refresh() async {
        guard !isRefreshing, !fictional else { return }
        isRefreshing = true; now = Date()
        defer { isRefreshing = false; now = Date() }
        do {
            let service: any ApplicationWorkflowServing
            if let injectedService { service = injectedService }
            else {
                let settings = try ConnectionSettings.loadDefault()
                if connection != settings { observation = nil; receivedAt = nil; selectedWorkerID = nil; unsupported = false }
                connection = settings
                service = try ApplicationWorkflowClient(settings: settings)
            }
            let result = try await service.fetch()
            try Task.checkCancellation()
            receive(result)
        } catch is CancellationError { return }
        catch { unsupported = false; self.error = error.localizedDescription.safeDiagnostic }
    }
    func receive(_ result: ApplicationWorkflowRead, at date: Date = Date()) {
        error = nil; now = date
        switch result {
        case .unsupported:
            unsupported = true; observation = nil; receivedAt = nil; selectedWorkerID = nil
        case .observed(let value):
            unsupported = false; observation = value; receivedAt = date
            if !value.workers.contains(where: { $0.id == selectedWorkerID }) { selectedWorkerID = value.workers.first?.id }
        }
    }
    func loadDemo(_ scenario: String) {
        guard fictional else { return }
        do {
            if scenario == "workflow-unsupported" { receive(.unsupported); return }
            if scenario == "workflow-disconnected" { error = "The fictional server could not be reached."; return }
            let value = try ApplicationWorkflowDemo.make(scenario: scenario)
            receive(.observed(value))
            if scenario == "workflow-stale" { now = Date().addingTimeInterval(90); error = "The last refresh did not complete." }
        } catch { self.error = "The workflow fixture could not be loaded." }
    }
}

/// Only the reported stage is emphasized; earlier steps are never marked as completed by inference.
enum ApplicationWorkerStep: String, CaseIterable, Identifiable {
    case load = "Load answers", browser = "Chrome", apply = "Apply", pool = "Question pool", wait = "Pause / ready", submit = "Submit", done = "Complete"
    var id: String { rawValue }
    func contains(_ stage: String) -> Bool {
        switch self {
        case .load: return stage == "loading_answers"
        case .browser: return stage == "waiting_for_browser"
        case .apply: return stage == "applying"
        case .pool: return ["waiting_for_lock", "checking_hash", "refreshing_answers", "matching_questions", "saving_questions"].contains(stage)
        case .wait: return ["paused", "ready"].contains(stage)
        case .submit: return ["submitting", "reconciling"].contains(stage)
        case .done: return stage == "completed"
        }
    }
}
