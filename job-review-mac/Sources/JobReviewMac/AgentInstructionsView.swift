import SwiftUI
import ReviewCore

struct AgentInstructionsView: View {
    @ObservedObject var model: DashboardModel
    @ObservedObject var editor: AgentInstructionsModel
    let selection: AgentInstructionSelection
    private var busy: Bool { model.isSaving || editor.isLoading }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 7) {
                Text("Instructions for agent").font(.title2.weight(.semibold))
                Text(selection.title).font(.headline)
                Text(selection.subtitle).font(.callout).foregroundStyle(AdminTheme.muted)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if editor.isLoading { ProgressView("Loading instructions…").controlSize(.small) }
                    if let message = editor.message {
                        Label(message, systemImage: "checkmark.circle").foregroundStyle(AdminTheme.successText)
                            .accessibilityIdentifier("instruction-confirmation")
                    }
                    if let error = editor.error {
                        Label(error, systemImage: "exclamationmark.circle").foregroundStyle(AdminTheme.warningText)
                            .accessibilityIdentifier("instruction-error")
                    }
                    if let target = editor.snapshot?.target {
                        VStack(alignment: .leading, spacing: 8) {
                            StatusLabel(status: target.taskStatus)
                            Text(AgentInstructionsModel.effect(target.mode))
                            if !target.supportsSubmission {
                                Text(AgentInstructionsModel.unavailable(target.unavailableReason)).foregroundStyle(AdminTheme.warningText)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(14).adminPanel()
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Your instructions").font(.headline)
                        Text("Describe what changed or what the agent should check. A new instruction replaces earlier guidance for this task.")
                            .foregroundStyle(AdminTheme.muted)
                        TextEditor(text: $editor.text)
                            .font(.body).scrollContentBackground(.hidden)
                            .padding(8).frame(minHeight: 130, idealHeight: 160)
                            .background(AdminTheme.surface)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border))
                            .disabled(busy || model.pendingSave != nil)
                            .accessibilityLabel("Your instructions").accessibilityIdentifier("agent-instruction-text")
                        HStack {
                            Text("Guidance does not answer application questions or change your consent.")
                            Spacer(minLength: 8)
                            Text("\(editor.count) / 4,000").monospacedDigit()
                                .foregroundStyle(editor.count > 4_000 ? AdminTheme.dangerText : AdminTheme.muted)
                        }.font(.caption).foregroundStyle(AdminTheme.muted)
                    }
                    history
                }.font(.callout).padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                if model.pendingSave != nil {
                    Text("A save needs confirmation. Check the original request before sending new instructions.")
                        .font(.caption).foregroundStyle(AdminTheme.warningText)
                } else if editor.needsRefresh {
                    Text("Refresh instructions to review the current task before sending.").font(.caption).foregroundStyle(AdminTheme.muted)
                }
                HStack {
                    Button("Close") { model.instructionSelection = nil }.disabled(busy)
                        .keyboardShortcut(.cancelAction)
                    Button("Refresh") { Task { await model.loadInstructions(selection) } }
                        .disabled(busy || model.isRefreshing).accessibilityIdentifier("refresh-agent-instructions")
                    Spacer()
                    if model.pendingSave != nil {
                        Button(model.isSaving ? "Checking…" : "Check saved request") { Task { await model.retryPending() } }
                            .buttonStyle(.borderedProminent).tint(AdminTheme.primary)
                            .disabled(busy || model.isRefreshing).accessibilityIdentifier("check-instruction-request")
                    } else {
                        Button(model.isSaving ? "Sending…" : "Send instructions") { Task { await model.sendInstructions(selection) } }
                            .buttonStyle(.borderedProminent).tint(AdminTheme.primary)
                            .disabled(!model.canSendInstructions(selection)).accessibilityIdentifier("send-agent-instructions")
                    }
                }
            }.padding(20)
        }
        .frame(width: 600, height: 640).background(AdminTheme.canvas)
        .interactiveDismissDisabled(busy)
        .task(id: selection.id) { await model.loadInstructions(selection) }
    }

    private var history: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Saved instructions").font(.headline)
            if let snapshot = editor.snapshot {
                if snapshot.instructions.isEmpty {
                    Text("No instructions have been saved for this task.").foregroundStyle(AdminTheme.muted)
                }
                ForEach(Array(snapshot.instructions.reversed())) { instruction in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(instruction.status.readableStatus).font(.caption.weight(.semibold))
                            Spacer()
                            Text(timestamp(instruction.createdAt)?.formatted(date: .abbreviated, time: .shortened) ?? instruction.createdAt)
                                .font(.caption).foregroundStyle(AdminTheme.muted)
                        }
                        Text(verbatim: instruction.text).textSelection(.enabled)
                        Text(AgentInstructionsModel.confirmation(mode: instruction.mode, wakeStatus: instruction.wakeStatus))
                            .font(.caption).foregroundStyle(AdminTheme.muted)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(14).adminPanel()
                }
            } else if !editor.isLoading {
                Text("Refresh to load this task’s saved history.").foregroundStyle(AdminTheme.muted)
            }
        }
    }
}
