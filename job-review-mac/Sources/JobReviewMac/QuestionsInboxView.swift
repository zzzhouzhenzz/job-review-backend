import SwiftUI
import ReviewCore

struct QuestionsInboxView: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Questions").font(.system(size: 24, weight: .semibold))
                Text(model.usesCanonicalQuestions ? "Shared questions and saved answers. Answers sync with your iPhone." : "Shared questions from your current application forms. Answers sync with your iPhone.")
                    .foregroundStyle(AdminTheme.muted).fixedSize(horizontal: false, vertical: true)
            }
            if let message = model.workflow?.questionSyncMessage {
                Label(message, systemImage: "arrow.triangle.2.circlepath")
                    .font(.callout).foregroundStyle(AdminTheme.warningText)
                    .accessibilityIdentifier("question-sync-status")
            }
            HStack(spacing: 8) {
                ForEach(QuestionsFilter.allCases) { filter in
                    Button {
                        model.questionsFilter = filter
                        model.selectedTaskID = nil
                        model.focusedQuestionID = nil
                    } label: {
                        Text(filter.rawValue + (filter == .issues || model.hasCanonicalQuestionSnapshot ? " · \(count(filter))" : ""))
                            .font(.system(size: 12, weight: model.questionsFilter == filter ? .semibold : .regular))
                            .padding(.horizontal, 12).padding(.vertical, 9)
                            .background(model.questionsFilter == filter ? AdminTheme.primary.opacity(0.12) : AdminTheme.surface,
                                        in: RoundedRectangle(cornerRadius: 5))
                            .foregroundStyle(model.questionsFilter == filter ? AdminTheme.link : AdminTheme.muted)
                    }.buttonStyle(.plain).accessibilityAddTraits(model.questionsFilter == filter ? .isSelected : [])
                        .accessibilityIdentifier("questions-filter-\(filter.id)")
                }
                Spacer(minLength: 0)
            }
            if model.questionsFilter == .issues {
                issues
            } else {
                TextField("Search questions or companies", text: $model.questionSearch)
                    .textFieldStyle(.roundedBorder).accessibilityIdentifier("questions-search")
                if model.usesCanonicalQuestions {
                    if model.visibleCanonicalQuestions.isEmpty { empty }
                    else {
                        ScrollView {
                            LazyVStack(spacing: 0) {
                                ForEach(model.visibleCanonicalQuestions) { question in
                                    canonicalRow(question)
                                    Divider()
                                }
                            }.background(AdminTheme.surface)
                        }
                    }
                } else if model.visibleQuestionRows.isEmpty { empty }
                else {
                    ScrollView {
                        LazyVStack(spacing: 0) {
                            ForEach(model.visibleQuestionRows) { row in
                                questionRow(row)
                                Divider()
                            }
                        }.background(AdminTheme.surface)
                    }
                }
            }
            Spacer(minLength: 0)
        }.padding(24).frame(minWidth: 430, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(AdminTheme.canvas).accessibilityIdentifier("questions-inbox")
    }

    private func canonicalRow(_ question: WorkflowSharedQuestion) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(question.text, systemImage: question.hasAcceptedAnswer ? "checkmark.circle.fill" : "questionmark.circle")
                .font(.callout.weight(.medium)).textSelection(.enabled)
            if let answer = question.answerDisplayText {
                Text(answer).font(.callout).textSelection(.enabled)
            }
            Text(question.hasAcceptedAnswer ? "Accepted by server" : "Answer needed")
                .font(.caption).foregroundStyle(question.hasAcceptedAnswer ? AdminTheme.successText : AdminTheme.muted)
            let bindings = model.canonicalBindings(question)
            if bindings.isEmpty {
                Text("Shared question · No current application form")
                    .font(.caption).foregroundStyle(AdminTheme.muted)
            } else {
                ForEach(bindings) { binding in
                    if let process = model.processes.first(where: { $0.id == binding.processID }) {
                        if binding.formVersion == process.formVersion {
                            Button("Open form · \(process.companyName) · \(process.title)") { model.openCanonicalBinding(binding) }
                                .font(.caption).buttonStyle(.plain).foregroundStyle(AdminTheme.link)
                                .accessibilityIdentifier("open-canonical-binding-\(binding.id)")
                        } else {
                            Text("Earlier form · \(process.companyName)").font(.caption).foregroundStyle(AdminTheme.muted)
                        }
                    }
                }
            }
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityIdentifier("canonical-question-\(question.id)")
    }

    private func count(_ filter: QuestionsFilter) -> Int {
        switch filter {
        case .pending: return model.pendingQuestionCount
        case .saved: return model.savedQuestionCount
        case .issues: return model.questionIssueTasks.count
        }
    }

    private func questionRow(_ row: SharedQuestionRow) -> some View {
        Button { model.openQuestion(row) } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: row.state == .saved ? "checkmark.circle.fill" : "questionmark.circle")
                    .foregroundStyle(row.state == .saved ? AdminTheme.successText : AdminTheme.warningText).padding(.top, 2)
                VStack(alignment: .leading, spacing: 7) {
                    Text(row.question.text).font(.callout.weight(.medium)).multilineTextAlignment(.leading)
                    Text(row.processes.map(\.companyName).joined(separator: " · "))
                        .font(.caption).foregroundStyle(AdminTheme.muted)
                    Text((row.state == .saved ? "Accepted by server" : row.question.answer != nil && row.question.answer != .null ? "Saved suggestion available · review required" : "Answer needed") +
                         (row.processes.count > 1 ? " · Shared by \(row.processes.count) applications" : ""))
                        .font(.caption).foregroundStyle(row.state == .saved ? AdminTheme.successText : AdminTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    if row.awaitingSyncCount > 0 {
                        Text("\(row.awaitingSyncCount) linked form\(row.awaitingSyncCount == 1 ? " is" : "s are") waiting for this saved answer to sync.")
                            .font(.caption).foregroundStyle(AdminTheme.warningText).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(AdminTheme.muted).padding(.top, 3)
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                .background(model.focusedQuestionID == row.question.id ? AdminTheme.primary.opacity(0.05) : .clear)
        }.buttonStyle(.plain).accessibilityIdentifier("shared-question-\(row.question.id)")
            .help("Open application answers")
    }

    private var empty: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.workflow?.questionSyncMessage != nil ? "Waiting for a complete snapshot" : model.questionSearch.isEmpty ? (model.questionsFilter == .pending ? "No answers needed" : "No saved answers yet") : "No matching questions")
                .font(.headline)
            Text(model.questionsFilter == .pending
                 ? "Questions appear here when an application can accept your answer. Other blockers are listed under Task issues."
                 : model.usesCanonicalQuestions ? "Your saved shared answers appear here, including answers without a current application." : "Accepted answers from current forms appear here after the workspace refreshes.")
                .font(.callout).foregroundStyle(AdminTheme.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading).background(AdminTheme.surface)
    }

    private var issues: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                Text("These tasks need review outside the answer form. Saved answers do not clear these issues.")
                    .font(.callout).foregroundStyle(AdminTheme.muted).padding(.bottom, 16)
                if model.questionIssueTasks.isEmpty {
                    Text("No other task issues").font(.headline).padding(20)
                }
                ForEach(model.questionIssueTasks) { task in
                    Button {
                        model.selectedTaskID = task.id; model.focusedQuestionID = nil
                    } label: {
                        VStack(alignment: .leading, spacing: 7) {
                            Text(model.taskTitle(task)).font(.callout.weight(.semibold))
                            Text(model.taskSubtitle(task)).font(.caption).foregroundStyle(AdminTheme.muted)
                            Text((task.resourceQuarantined ? "browser_work_uncertain" : model.process(for: task)?.blockReason ?? task.error ?? "Review required").safeDiagnostic.readableStatus)
                                .font(.caption).foregroundStyle(AdminTheme.warningText)
                            Text("Open task details").font(.caption).foregroundStyle(AdminTheme.link)
                        }.padding(16).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }.buttonStyle(.plain).background(AdminTheme.surface).accessibilityIdentifier("question-issue-\(task.id)")
                    Divider()
                }
            }
        }
    }
}
