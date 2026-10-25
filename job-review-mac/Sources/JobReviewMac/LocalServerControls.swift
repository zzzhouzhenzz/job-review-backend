import SwiftUI

struct LocalServerControls: View {
    @ObservedObject var server: LocalServerModel
    let saving: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 14) {
                Image(systemName: "server.rack").foregroundStyle(AdminTheme.muted)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Local server · \(server.title)").font(.callout.weight(.semibold))
                        .accessibilityIdentifier("local-server-status")
                    Text(server.detail).font(.caption).foregroundStyle(AdminTheme.muted)
                }
                Spacer(minLength: 8)
                if server.isBusy { ProgressView().controlSize(.small) }
                Button("Refresh status") { Task { await server.refresh() } }
                    .disabled(server.isBusy || server.isRefreshing).accessibilityIdentifier("refresh-server-status")
                if server.status?.state == .stopped || server.operation == "start" {
                    Button("Start server") { Task { await server.startServer() } }
                        .disabled(!server.canStart || saving).accessibilityIdentifier("start-local-server")
                } else {
                    Button("Stop server") { Task { await server.stopServer() } }
                        .disabled(!server.canStop || saving).accessibilityIdentifier("stop-local-server")
                }
            }
            if let error = server.error {
                Text(error).font(.caption).foregroundStyle(AdminTheme.warningText).textSelection(.enabled)
                    .accessibilityIdentifier("local-server-error")
            }
        }.padding(.horizontal, 24).padding(.vertical, 12)
            .background(AdminTheme.surface).overlay(alignment: .bottom) { Divider() }
    }
}
