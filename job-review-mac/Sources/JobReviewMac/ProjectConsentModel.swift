import Combine
import Foundation
import MacDashboardCore

@MainActor final class ProjectConsentModel: ObservableObject {
    @Published private(set) var consent: ProjectConsent?
    @Published private(set) var isSaving = false
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published var isPresented = false
    private var client: (any ProjectConsentServing)?
    private var origin: String?
    private var dismissed = false
    private var pendingRequestID: String?
    private var generation = 0
    private let intentStore: any ConsentIntentStoring

    init(intentStore: any ConsentIntentStoring = ConsentIntentFile()) { self.intentStore = intentStore }
    var hasPendingAgreement: Bool { pendingRequestID != nil }

    var status: String {
        if errorMessage != nil { return "Could not verify permissions" }
        guard let consent else { return "Checking permissions" }
        return consent.granted ? "Agreed" : "Agreement required"
    }

    func load(client: any ProjectConsentServing, origin: String) async {
        if self.origin != origin {
            generation += 1
            self.origin = origin
            consent = nil; errorMessage = nil; isPresented = false
            dismissed = false; pendingRequestID = nil; isSaving = false; isLoading = false
        }
        guard !isSaving, !isLoading else { return }
        generation += 1
        let current = generation
        isLoading = true
        defer { if current == generation { isLoading = false } }
        do {
            let value = try await client.fetch()
            guard current == generation, !Task.isCancelled else { return }
            self.client = client
            if let intent = try intentStore.load(), intent.matches(origin) { pendingRequestID = intent.requestID }
            accept(value)
            if value.granted {
                try clearMatchingIntent()
            } else if pendingRequestID != nil {
                isLoading = false
                await agree()
            }
        } catch is CancellationError {
            return
        } catch {
            guard current == generation else { return }
            errorMessage = (error as? DashboardError)?.localizedDescription ?? "The saved permission request could not be read on this Mac."
        }
    }

    func agree() async {
        guard !isSaving, !isLoading, consent?.granted == false, let client, let origin else { return }
        generation += 1
        isSaving = true; errorMessage = nil
        let current = generation
        let requestID = pendingRequestID ?? UUID().uuidString
        defer { if current == generation { isSaving = false } }
        do {
            // Persist the click before sending. A crash can retry the same fixed command safely.
            try intentStore.save(ConsentIntent(origin: origin, policyVersion: ProjectConsent.policyVersion, requestID: requestID))
            pendingRequestID = requestID
            let saved = try await client.agree(requestID: requestID)
            guard current == generation, !Task.isCancelled else { return }
            guard saved.granted else { throw DashboardError.invalidResponse }
            accept(saved)
            try clearMatchingIntent()
        } catch is CancellationError {
            return
        } catch {
            guard current == generation else { return }
            // The same ID is reused after an uncertain save. A later read can confirm it.
            errorMessage = (error as? DashboardError)?.localizedDescription ?? "Your agreement could not be saved on this Mac."
        }
    }

    func dismiss() {
        guard !isSaving else { return }
        dismissed = true; isPresented = false
    }

    private func clearMatchingIntent() throws {
        if let origin, let intent = try intentStore.load(), intent.matches(origin) { try intentStore.clear() }
    }

    func review() { if consent?.granted == false { isPresented = true } }

    private func accept(_ value: ProjectConsent) {
        consent = value; errorMessage = nil
        if value.granted { isPresented = false; pendingRequestID = nil }
        else if !dismissed { isPresented = true }
    }

    /// Fictional previews never load credentials or contact a service.
    func loadDemo(_ scenario: String) {
        let absent = ["consent-first-use", "consent-save-failure", "consent-declined"].contains(scenario)
        consent = ProjectConsent(granted: !absent, grantedAt: absent ? nil : "2026-10-18T12:00:00Z")
        dismissed = scenario == "consent-declined"
        isPresented = absent && !dismissed
        if scenario == "consent-save-failure" { errorMessage = "The service did not respond in time. Your agreement is not confirmed." }
    }
}
