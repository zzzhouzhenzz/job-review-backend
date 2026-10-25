import SwiftUI
import ReviewCore

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var demo = true
    @State private var address = ""
    @State private var replaceToken = false
    @State private var token = ""
    @State private var showError: String?
    @State private var connecting = false
    var body: some View {
        NavigationStack {
            Form {
                if let showError { Section { Text(showError).foregroundStyle(Palette.rust) } }
                Section {
                    Toggle("Use sample feed", isOn: $demo).disabled(model.pending != nil)
                    Text("Sample choices stay on this device. They do not change your real jobs.")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                }
                if !demo {
                    Section("Review server") {
                        TextField("https://your-server.example", text: $address).URLInput().disabled(model.pending != nil)
                        Text("Enter your private HTTPS server address. Use localhost only in the simulator on this Mac.")
                            .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                        Toggle("Replace access token", isOn: $replaceToken)
                        if replaceToken {
                            SecureField("Access token", text: $token).URLInput()
                            Text("Required for private device access. Leave blank to remove the saved token. Tokens stay in this device's Keychain.")
                                .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                        }
                    }
                }
                Section("How decisions work") {
                    Label("Swipe left or right to browse the company's jobs.", systemImage: "rectangle.on.rectangle")
                    Label(demo ? "Select exact jobs, then tap the checkmark to approve." : "Select exact jobs, then tap the checkmark to apply.", systemImage: "checkmark.circle")
                    Label("Tap the X to exclude a company.", systemImage: "xmark.circle")
                    Label("Find more requests more research.", systemImage: "magnifyingglass")
                    Text(demo ? "Approval does not send an application. Queue the jobs you want the agent to process." : "Each decision saves and opens the next company. Apply authorizes work for the selected job versions. A configured worker may submit them after required questions are answered. Follow the results in Progress. Older servers keep the separate Approve and manual Queue flow.")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                }
                Section("Saved on this device") {
                    Text("You can read the latest saved feed offline. Connect before making choices. Pending saves keep their exact request until the outcome is known.")
                        .font(AppTypography.font(.footnote))
                    if model.pending != nil { Text("You can update the current server's access token. Check the pending save before changing servers.").foregroundStyle(Palette.rust) }
                }
            }
            .disabled(connecting)
            .scrollContentBackground(.hidden).background(Palette.paper)
            .navigationTitle("Settings").inlineTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(connecting ? "Connecting…" : demo ? "Use sample feed" : "Connect") {
                        do {
                            if !demo { _ = try ServerAddress(address) }
                            showError = nil
                            connecting = true
                            Task {
                                let connected = await model.connect(demo: demo, address: address, accessToken: replaceToken ? token : nil)
                                token = ""
                                // The new credential may be saved even when the server cannot be reached.
                                // A second tap must not replace it with the cleared text field.
                                replaceToken = false
                                connecting = false
                                if connected { dismiss() }
                                else { showError = model.error ?? "Connection did not finish. Try again." }
                            }
                        } catch { showError = error.localizedDescription }
                    }.disabled(model.busy || connecting)
                }
            }
            .onAppear { demo = model.demo; address = model.serverText }
            .interactiveDismissDisabled(connecting)
        }
    }
}
