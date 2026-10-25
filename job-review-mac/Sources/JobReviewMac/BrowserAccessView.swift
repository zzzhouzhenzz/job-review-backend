import SwiftUI
import MacDashboardCore

struct BrowserAccessView: View {
    let value: ApplicationWorkflowObservation
    let fresh: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Shared Chrome connection", systemImage: "network").font(.headline)
                Spacer()
                Text(value.browserAccess.map { "\($0.capacity) connection" } ?? "Not reported").font(.caption).foregroundStyle(AdminTheme.muted)
            }
            if let access = value.browserAccess {
                HStack(alignment: .top, spacing: 24) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text((fresh ? "" : "Last reported · ") + title(access.state)).font(.callout.weight(.semibold))
                            .foregroundStyle(fresh && access.state == "free" ? AdminTheme.successText : AdminTheme.warningText)
                        Text(access.owner.map { "Owner: \(name($0))" } ?? "No owner reported")
                            .font(.caption).textSelection(.enabled)
                        if let owner = access.owner {
                            Text("Connection state: \(ApplicationBrowserTicket.states.contains(owner.state) ? owner.state.readableStatus : "Unknown")")
                                .font(.caption).foregroundStyle(AdminTheme.muted)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .leading, spacing: 7) {
                        Text(access.policy == "chrome_fifo_v1" ? "Waiting · first in, first out" : "Waiting · queue policy not recognized")
                            .font(.callout.weight(.semibold))
                        if access.queue.isEmpty { Text("No queued work reported").font(.caption).foregroundStyle(AdminTheme.muted) }
                        ForEach(Array(access.queue.enumerated()), id: \.element.id) { index, ticket in
                            Text("\(index + 1). \(name(ticket))").font(.caption).fixedSize(horizontal: false, vertical: true)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                if !access.blockedBy.isEmpty {
                    Text("Held for review: " + access.blockedBy.map { taskName($0.taskId) }.joined(separator: " · "))
                        .font(.caption.weight(.medium)).foregroundStyle(AdminTheme.warningText)
                }
                if ["blocked", "uncertain"].contains(access.state) {
                    Text("Earlier browser work needs review before queued workers can use Chrome.")
                        .font(.caption).foregroundStyle(AdminTheme.muted)
                }
            } else {
                Text("This server does not report Chrome ownership or its waiting queue.")
                    .font(.callout).foregroundStyle(AdminTheme.muted)
            }
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border, lineWidth: 1))
            .accessibilityElement(children: .combine).accessibilityIdentifier("browser-access")
    }

    private func title(_ state: String) -> String {
        ["free": "Available", "busy": "In use", "blocked": "Needs review", "uncertain": "Outcome unconfirmed"][state] ?? "Unknown connection state"
    }
    private func name(_ ticket: ApplicationBrowserTicket) -> String {
        let role = ticket.workerId?.safeDiagnostic ?? ticket.role.safeDiagnostic.readableStatus
        return "\(role) · \(taskName(ticket.taskId))"
    }
    private func taskName(_ id: String) -> String {
        value.workers.first { $0.taskId == id }?.company?.safeDiagnostic ?? "Task \(id.prefix(8))"
    }
}
