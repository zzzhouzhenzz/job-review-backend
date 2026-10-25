import Foundation

public struct SessionView: Sendable {
    public var state: ReviewState?
    public var savedAt: Date?
    public var pending: PendingSave?
    public var online: Bool
}

/// A single writer per profile. Server data is authoritative; the cache never invents success.
public actor ReviewSession {
    private let api: ReviewAPI
    private let persistence: any SessionPersistence
    private var saved: SavedSession
    private var token: String?
    private var online = false
    private var busy = false

    public init(api: ReviewAPI, persistence: any SessionPersistence) throws {
        self.api = api; self.persistence = persistence
        let loaded = try persistence.load() ?? SavedSession(origin: api.address.origin)
        guard loaded.origin == api.address.origin, loaded.pending == nil || loaded.pending?.origin == api.address.origin else {
            throw ReviewError.storage("The saved review belongs to another server. Its pending request was kept.")
        }
        saved = loaded
    }
    public func view() -> SessionView { SessionView(state: saved.state, savedAt: saved.savedAt, pending: saved.pending, online: online) }

    public func refresh() async throws {
        guard !busy else { throw ReviewError.pending }
        busy = true; defer { busy = false }
        do { try accept(try await api.fetch(), clearPending: false) }
        catch { online = false; token = nil; throw error }
    }

    public func decide(card: FlashCard, action: CardAction, selected: Set<String>, note: String = "") async throws {
        try requireWrite()
        guard saved.state?.flashcards.first(where: { $0.companyID == card.companyID }) == card else {
            throw ReviewError.conflict("This card changed. Refresh and review the current jobs.")
        }
        let request = try CardRequest(card: card, action: action, selected: selected, note: note)
        let company = saved.state?.company(card.companyID)?.name ?? "Company"
        let summary = action == .approve ? "Approve \(selected.count) job(s) at \(company)" : action == .research ? "Find more jobs at \(company)" : "Exclude \(company)"
        try prepare(PendingSave(origin: api.address.origin, path: "api/card", payload: request, summary: summary))
        try await commit()
    }

    public func queue(keys: Set<String>, expectedVersions: [JobVersion]? = nil) async throws {
        try requireWrite()
        let jobs = saved.state?.readyJobs.filter { keys.contains($0.key) } ?? []
        guard !keys.isEmpty, jobs.count == keys.count else { throw ReviewError.invalid("Some selected jobs are no longer ready. Check the current list.") }
        if let expectedVersions {
            let current = jobs.map(JobVersion.init).sorted { $0.key < $1.key }
            guard current == expectedVersions.sorted(by: { $0.key < $1.key }) else {
                throw ReviewError.conflict("The selected jobs changed. Close this confirmation and review them again.")
            }
        }
        let request = try QueueRequest(jobs: jobs)
        try prepare(PendingSave(origin: api.address.origin, path: "api/queue", payload: request, summary: "Queue \(jobs.count) selected job(s)"))
        try await commit()
    }

    /// Explicit recovery only. Read fresh credentials, then send the original bytes to the same origin.
    public func retryPending() async throws {
        guard !busy, saved.pending != nil else { throw ReviewError.invalid("There is no pending save to check.") }
        busy = true
        do { try accept(try await api.fetch(), clearPending: false) }
        catch { busy = false; online = false; token = nil; throw error }
        busy = false
        try await commit()
    }

    private func requireWrite() throws {
        guard saved.pending == nil, !busy else { throw ReviewError.pending }
        guard online, token != nil else { throw ReviewError.offline }
    }
    private func prepare(_ request: PendingSave) throws {
        var next = saved; next.pending = request
        try persistence.save(next) // If disk fails, do not send a mutation.
        saved = next
    }
    private func commit() async throws {
        guard !busy, let pending = saved.pending, let token else { throw ReviewError.offline }
        busy = true; defer { busy = false }
        do { try accept(try await api.send(pending, sessionToken: token), clearPending: true) }
        catch let error as ReviewError {
            switch error {
            case .conflict, .rejected:
                // Only operation-level 400/409 errors reach this path. Auth and transient failures keep the request.
                var next = saved; next.pending = nil
                try persistence.save(next); saved = next; online = false; self.token = nil
                if case .conflict = error {
                    do { try accept(try await api.fetch(), clearPending: false) }
                    catch { online = false; self.token = nil }
                }
                throw error
            default:
                online = false; self.token = nil
                throw ReviewError.uncertain("The save outcome is unknown. Check the pending save before continuing. \(error.localizedDescription)")
            }
        } catch {
            online = false; self.token = nil
            throw ReviewError.uncertain("The save outcome is unknown. Check the pending save before continuing. \(error.localizedDescription)")
        }
    }
    private func accept(_ state: ReviewState, clearPending: Bool) throws {
        guard let newToken = state.token, !newToken.isEmpty else { throw ReviewError.uncertain("The server response has no session token.") }
        var next = saved
        // A refresh supplies current credentials but must not advance an unresolved card.
        if clearPending || saved.pending == nil {
            next.state = state.withoutToken; next.savedAt = Date()
        }
        if clearPending { next.pending = nil }
        try persistence.save(next)
        saved = next; token = newToken; online = true
    }
}
