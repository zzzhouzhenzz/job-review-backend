import Foundation
import ReviewCore

/// Additive observation only. Older servers omit this object; absence is not a free connection.
public struct ApplicationBrowserAccess: Codable, Equatable, Sendable {
    public let capacity: Int
    public let policy: String
    public let revision: Int
    public let state: String
    public let owner: ApplicationBrowserTicket?
    public let queue: [ApplicationBrowserTicket]
    public let blockedBy: [ApplicationBrowserBlocker]
    public static let states: Set<String> = ["free", "busy", "blocked", "uncertain"]

    func validate(_ raw: JSONValue) throws {
        guard capacity == 1, revision >= 0, !policy.isEmpty, !state.isEmpty,
              raw.object["owner"] != nil else { throw DashboardError.invalidResponse }
        let tickets = queue + (owner.map { [$0] } ?? [])
        guard Set(tickets.map(\.ticketId)).count == tickets.count else { throw DashboardError.invalidResponse }
        for (index, ticket) in queue.enumerated() { try ticket.validate(raw.object["queue"]!.array[index]) }
        if let owner { try owner.validate(raw.object["owner"]!) }
        guard blockedBy.allSatisfy({ !$0.taskId.isEmpty && !$0.resource.isEmpty }) else { throw DashboardError.invalidResponse }
    }
}

public struct ApplicationBrowserTicket: Codable, Equatable, Sendable, Identifiable {
    public let ticketId: String
    public let taskId: String
    public let workerId: String?
    public let role: String
    public let enqueuedAt: String?
    public let acquiredAt: String?
    public let startedAt: String?
    public let state: String
    public var id: String { ticketId }
    public static let states: Set<String> = ["waiting", "owned", "active", "uncertain"]

    func validate(_ raw: JSONValue) throws {
        guard !ticketId.isEmpty, !taskId.isEmpty, !role.isEmpty, !state.isEmpty,
              workerId.map({ !$0.isEmpty }) ?? true,
              ["worker_id", "enqueued_at", "acquired_at", "started_at"].allSatisfy({ raw.object[$0] != nil }),
              [enqueuedAt, acquiredAt, startedAt].compactMap({ $0 }).allSatisfy({ ApplicationWorkflowObservation.date($0) != nil })
        else { throw DashboardError.invalidResponse }
    }
}

public struct ApplicationBrowserBlocker: Codable, Equatable, Sendable {
    public let taskId: String
    public let resource: String
}
