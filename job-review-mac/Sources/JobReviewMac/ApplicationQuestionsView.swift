import SwiftUI
import ReviewCore

/// Identifies the exact application form shown to the user. Question order is not a revision.
struct ApplicationQuestionIdentity: Hashable {
    struct QuestionVersion: Hashable {
        let id: String
        let processID: String
        let revision: Int
        let formVersion: String
    }
    let processID: String
    let processRevision: Int
    let formVersion: String?
    let snapshotPoolRevision: Int?
    let questions: [QuestionVersion]

    init(process: WorkflowProcess, questions: [WorkflowQuestion]) {
        processID = process.id
        processRevision = process.revision
        formVersion = process.formVersion
        snapshotPoolRevision = questions.first?.snapshotPoolRevision
        self.questions = questions.sorted { $0.id < $1.id }.map {
            QuestionVersion(id: $0.id, processID: $0.processID, revision: $0.revision, formVersion: $0.formVersion)
        }
    }
}

/// Stores only answers entered in this editor. Server answers never populate a new draft.
struct ApplicationQuestionDraft {
    private(set) var values: [String: JSONValue] = [:]

    init() {}
    init(questions: [WorkflowQuestion]) { self.init() }

    mutating func setText(_ text: String, for question: WorkflowQuestion) {
        if question.required && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            values.removeValue(forKey: question.id)
            return
        }
        values[question.id] = .string(text)
    }

    mutating func useSavedAnswer(for question: WorkflowQuestion) throws {
        guard let answer = question.answer, answer != .null else {
            throw ReviewError.invalid("There is no saved answer to use for this question.")
        }
        try question.validate(answer)
        values[question.id] = answer
    }

    mutating func selectChoice(_ id: String, for question: WorkflowQuestion) throws {
        guard question.isSupported, question.type == "single_choice", question.choices.contains(where: { $0.id == id }) else {
            throw ReviewError.invalid("Select one of the listed choices.")
        }
        values[question.id] = .string(id)
    }

    mutating func toggleChoice(_ id: String, for question: WorkflowQuestion) throws {
        guard question.isSupported, question.type == "multi_choice", question.choices.contains(where: { $0.id == id }) else {
            throw ReviewError.invalid("Select one of the listed choices.")
        }
        var selected = Set(values[question.id]?.array.compactMap(\.text) ?? [])
        if selected.contains(id) { selected.remove(id) } else { selected.insert(id) }
        values[question.id] = .array(selected.sorted().map(JSONValue.string))
    }

    mutating func removeID(_ id: String) { values.removeValue(forKey: id) }

    mutating func remove(_ question: WorkflowQuestion) { values.removeValue(forKey: question.id) }

    func request(process: WorkflowProcess, questions: [WorkflowQuestion]) throws -> WorkflowAnswerRequest {
        try WorkflowAnswerRequest(process: process, questions: questions, values: values)
    }

    func validationMessage(process: WorkflowProcess, questions: [WorkflowQuestion]) -> String? {
        guard !values.isEmpty else { return nil }
        do { _ = try request(process: process, questions: questions); return nil }
        catch { return error.localizedDescription }
    }

    func validationMessage(for question: WorkflowQuestion) -> String? {
        guard let value = values[question.id] else { return nil }
        do { try question.validate(value); return nil }
        catch { return error.localizedDescription }
    }
}

struct ApplicationAnswerReview {
    let process: WorkflowProcess
    let questions: [WorkflowQuestion]
    let values: [String: JSONValue]
    var correctionIDs: Set<String> = []
    var identity: ApplicationQuestionIdentity { ApplicationQuestionIdentity(process: process, questions: questions) }
}

struct ApplicationQuestionsView: View {
    @ObservedObject var model: DashboardModel
    let process: WorkflowProcess
    let questions: [WorkflowQuestion]
    var context: AnyView = AnyView(EmptyView())
    @ObservedObject var editor: ApplicationQuestionEditor
    private var draft: ApplicationQuestionDraft { get { editor.draft } nonmutating set { editor.draft = newValue } }
    private var review: ApplicationAnswerReview? { get { editor.review } nonmutating set { editor.review = newValue } }
    private var notice: String? { get { editor.notice } nonmutating set { editor.notice = newValue } }

    static func identity(process: WorkflowProcess, questions: [WorkflowQuestion]) -> ApplicationQuestionIdentity {
        ApplicationQuestionIdentity(process: process, questions: questions)
    }

    static func distinctQuestions(_ questions: [WorkflowQuestion]) -> [WorkflowQuestion] {
        var seen = Set<String>()
        return questions.sorted {
            $0.hasAcceptedAnswer == $1.hasAcceptedAnswer ? $0.id < $1.id : $0.hasAcceptedAnswer
        }.filter { question in
            let key = question.poolQuestionID.map { "pool:\(question.scope):\($0)" } ?? "local:\(question.id)"
            return seen.insert(key).inserted
        }
    }
    private var displayedQuestions: [WorkflowQuestion] { model.usesCanonicalQuestions ? questions.sorted { $0.id < $1.id } : Self.distinctQuestions(questions) }
    private var version: ApplicationQuestionIdentity { Self.identity(process: process, questions: questions) }
    private var canEdit: Bool {
        model.canAnswer(process) && !model.isSaving &&
        questions.allSatisfy { $0.processID == process.id && $0.formVersion == process.formVersion }
    }
    private var validation: String? { draft.validationMessage(process: process, questions: questions) }
    private var acceptedBlockerMessage: String? { model.acceptedAnswersBlockerMessage(process: process, questions: questions) }
    private var allAccepted: Bool { !questions.isEmpty && questions.allSatisfy(\.hasAcceptedAnswer) }
    private var hasDeferredDraft: Bool { model.hasDeferredSharedAnswers(questions: questions, values: review?.values ?? draft.values) }
    private var unavailableReason: String? {
        if let message = model.workflow?.questionSyncMessage { return message }
        if model.isSaving { return "Wait for the current save to finish." }
        if model.isRefreshing { return "Wait for the current refresh to finish." }
        if model.pendingSave != nil { return "Confirm the pending save before submitting new answers." }
        if hasDeferredDraft { return "A shared answer is already accepted. Remove its local draft or wait for this form to sync. Other drafts are kept." }
        if let acceptedBlockerMessage { return acceptedBlockerMessage }
        if allAccepted { return "All answers for this form are accepted. Application status: \(process.taskStatus.readableStatus)." }
        if model.tasks.first(where: { $0.id == process.taskID })?.resourceQuarantined == true {
            return "Browser work needs review. Answers cannot be sent while this application is held. See Task details."
        }
        if process.submissionStatus == "submission_unknown" { return "Confirm the earlier submission outcome in Task details before continuing." }
        if process.submissionStatus == "submitted" { return "This application was submitted. These questions are read only." }
        if let reason = process.blockReason, reason != "questions_pending", !reason.isEmpty {
            return "This task needs review: \(reason.safeDiagnostic.readableStatus). See Task details."
        }
        if !canEdit { return "Refresh this application and its current form before submitting answers." }
        return nil
    }

    var body: some View {
        VStack(spacing: 0) {
            if model.page == .questions {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(process.companyName).font(.headline)
                        Text(process.title).font(.caption).foregroundStyle(AdminTheme.muted).lineLimit(2)
                    }
                    Spacer(minLength: 0)
                    Button { model.selectedTaskID = nil; model.focusedQuestionID = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain).accessibilityLabel("Close application answers")
                }.padding(16).overlay(alignment: .bottom) { Divider() }
            }
            ScrollViewReader { reader in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    context
                    VStack(alignment: .leading, spacing: 6) {
                        Label("Application questions", systemImage: "questionmark.bubble").font(.headline)
                        Text(process.companyName).font(.title3.weight(.semibold))
                        Text(process.title).font(.callout).foregroundStyle(.secondary)
                        Text(allAccepted ? "Review your accepted answers and application status below." : !canEdit ? "This form is read only in the current application state." : acceptedBlockerMessage == nil
                             ? "Enter your answers or choose Use saved answer. Review your selections before submitting them."
                             : "Review your accepted answers and the remaining task issue below.")
                            .font(.caption).foregroundStyle(.secondary)
                        if let notice { Text(notice).font(.caption).foregroundStyle(AdminTheme.warningText) }
                    }
                    if questions.isEmpty {
                        Text("There are no questions for this form.").font(.callout).foregroundStyle(.secondary)
                    } else if let review {
                        reviewPanel(review)
                    } else {
                        ForEach(displayedQuestions) { question in
                            questionEditor(question).id(question.id)
                            Divider()
                        }
                    }
                    if !editor.retainedDrafts.isEmpty {
                        DisclosureGroup("Retained drafts · \(editor.retainedDrafts.count)") {
                            ForEach(editor.retainedDrafts.values.sorted { $0.id < $1.id }) { retained in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(retained.question.text).font(.callout.weight(.medium))
                                    Text(answerLabel(retained.value, question: retained.question)).font(.callout).textSelection(.enabled)
                                    Text("Kept for your review. This draft is not included in a submission.").font(.caption).foregroundStyle(.secondary)
                                    Button("Remove retained draft") { editor.retainedDrafts.removeValue(forKey: retained.id) }
                                        .font(.caption).disabled(model.isSaving || model.pendingSave != nil)
                                }.padding(.vertical, 6)
                            }
                        }.accessibilityIdentifier("retained-question-drafts")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
            .onAppear { if let id = model.focusedQuestionID { reader.scrollTo(id, anchor: .top) } }
            .onChange(of: model.focusedQuestionID) { _, id in
                if let id { reader.scrollTo(id, anchor: .top) }
            }
            }
            answerFooter
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(AdminTheme.surface)
        .onAppear { synchronizeEditor() }
        .onChange(of: version) { _, _ in synchronizeEditor() }
        .onChange(of: questions) { _, _ in synchronizeEditor() }
    }

    private func synchronizeEditor() {
        guard !model.usesCanonicalQuestions || model.workflow?.questionSyncAllowsAnswers == true else { return }
        editor.synchronize(process: process, questions: questions, preservingDrafts: model.usesCanonicalQuestions)
    }

    private var answerFooter: some View {
        VStack(alignment: .leading, spacing: 8) {
            let count = review?.values.count ?? draft.values.count
            Text("\(count) draft answer\(count == 1 ? "" : "s")")
                .font(.callout.weight(.semibold))
            if !editor.retainedDrafts.isEmpty {
                Text("\(editor.retainedDrafts.count) retained draft\(editor.retainedDrafts.count == 1 ? "" : "s") · Open Retained drafts in the form to review.")
                    .font(.caption).foregroundStyle(AdminTheme.warningText)
            }
            if let review {
                let current = review.identity == version && canEdit && !hasDeferredDraft
                if let unavailableReason {
                    Text(unavailableReason).font(.caption).foregroundStyle(.secondary)
                } else if !current {
                    Text("This form changed. Return to the current questions before submitting answers.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Back to answers") { self.review = nil }.disabled(model.isSaving)
                    Spacer(minLength: 8)
                    Button(model.isSaving ? "Submitting…" : "Submit answers") {
                        guard current else { return }
                        Task { await model.answer(process: review.process, questions: review.questions, values: review.values, correctionIDs: review.correctionIDs) }
                    }
                    .buttonStyle(.borderedProminent).tint(AdminTheme.primary)
                    .disabled(!current || model.isSaving)
                    .accessibilityIdentifier("save-application-answers")
                }
            } else {
                if let unavailableReason {
                    Text(unavailableReason).font(.caption).foregroundStyle(.secondary)
                } else if let validation {
                    Text(validation).font(.caption).foregroundStyle(AdminTheme.dangerText)
                } else if draft.values.isEmpty {
                    Text("Enter an answer or use a saved answer to continue.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button("Review answers") {
                    do {
                        _ = try draft.request(process: process, questions: questions)
                        review = ApplicationAnswerReview(process: process, questions: questions, values: draft.values, correctionIDs: editor.correctionIDs)
                        notice = nil
                    } catch { notice = error.localizedDescription }
                }
                .buttonStyle(.borderedProminent).tint(AdminTheme.primary)
                .disabled(!canEdit || draft.values.isEmpty || validation != nil || hasDeferredDraft)
                .accessibilityIdentifier("review-application-answers")
            }
            if acceptedBlockerMessage == nil && !allAccepted && canEdit {
                Text("Only these draft answers will be saved. You can submit a partial form; missing required answers keep the application paused.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AdminTheme.surface)
        .overlay(alignment: .top) { Divider() }
    }

    private func questionEditor(_ question: WorkflowQuestion) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(question.text).font(.callout.weight(.medium)).textSelection(.enabled)
            Text("\(question.required ? "Required" : "Optional") · \(typeName(question.type))")
                .font(.caption).foregroundStyle(.secondary)
            if let poolID = question.poolQuestionID {
                let bindings = questions.filter { $0.poolQuestionID == poolID && $0.scope == question.scope }.count
                Text("Shared question \(poolID)" + (bindings > 1 ? " · \(bindings) form bindings" : ""))
                    .font(.caption).foregroundStyle(AdminTheme.muted).textSelection(.enabled)
            }
            if !question.reason.isEmpty { Text(question.reason).font(.caption).foregroundStyle(.secondary) }
            if let accepted = model.acceptedSharedQuestion(for: question) {
                Label("Shared answer accepted · this form is waiting to sync", systemImage: "checkmark.circle.fill")
                    .font(.caption).foregroundStyle(AdminTheme.successText)
                Button("View saved shared answer") {
                    if let row = model.sharedQuestionRows.first(where: { $0.question.id == accepted.id }) {
                        model.questionsFilter = .saved; model.openQuestion(row)
                    }
                }.font(.caption).accessibilityIdentifier("view-shared-answer-\(question.id)")
                if draft.values[question.id] != nil {
                    Button("Remove local draft") { draft.remove(question); notice = nil }
                        .font(.caption).disabled(model.isSaving || model.pendingSave != nil)
                }
            } else if let answer = question.answer, answer != .null {
                Text("Saved answer: \(answerLabel(answer, question: question))")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                if question.hasAcceptedAnswer && !editor.correctionIDs.contains(question.id) {
                    Label("Accepted by server · synced across devices", systemImage: "checkmark.circle.fill")
                        .font(.caption).foregroundStyle(AdminTheme.successText)
                    Button("Review a correction") {
                        editor.correctionIDs.insert(question.id)
                        notice = "Review a correction against this saved answer before submitting."
                    }.disabled(!canEdit).font(.caption)
                        .accessibilityIdentifier("correct-answer-\(question.id)")
                } else if (try? question.validate(answer)) != nil {
                    Button("Use saved answer") {
                        do { try draft.useSavedAnswer(for: question); notice = nil }
                        catch { notice = error.localizedDescription }
                    }
                    .buttonStyle(.bordered).disabled(!canEdit)
                    .accessibilityIdentifier("use-saved-answer-\(question.id)")
                } else if question.isSupported {
                    Text("The saved answer does not match this question. Enter an answer below.")
                        .font(.caption).foregroundStyle(AdminTheme.warningText)
                }
            }
            if model.acceptedSharedQuestion(for: question) != nil || (question.hasAcceptedAnswer && !editor.correctionIDs.contains(question.id)) {
                EmptyView()
            } else if question.isSupported {
                answerField(question).disabled(!canEdit)
                if let message = draft.validationMessage(for: question) {
                    Text(message).font(.caption).foregroundStyle(AdminTheme.dangerText)
                }
                if draft.values[question.id] != nil {
                    Button("Remove draft answer") { draft.remove(question); notice = nil }
                        .font(.caption).buttonStyle(.borderless).disabled(!canEdit)
                }
            } else {
                Text("This question type or scope is not supported. Its answer is read only.")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
    }

    @ViewBuilder private func answerField(_ question: WorkflowQuestion) -> some View {
        if question.type == "text" {
            TextField("Your answer", text: Binding(
                get: { draft.values[question.id]?.text ?? "" },
                set: { draft.setText($0, for: question); notice = nil }
            ), axis: .vertical)
            .textFieldStyle(.roundedBorder).lineLimit(2...8).accessibilityLabel(question.text)
            .accessibilityIdentifier("answer-\(question.id)")
        } else {
            ForEach(question.choices) { choice in
                let selected = question.type == "single_choice"
                    ? draft.values[question.id]?.text == choice.id
                    : draft.values[question.id]?.array.compactMap(\.text).contains(choice.id) == true
                Button {
                    do {
                        if question.type == "single_choice" { try draft.selectChoice(choice.id, for: question) }
                        else { try draft.toggleChoice(choice.id, for: question) }
                        notice = nil
                    } catch { notice = error.localizedDescription }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: question.type == "single_choice"
                              ? (selected ? "largecircle.fill.circle" : "circle")
                              : (selected ? "checkmark.square.fill" : "square"))
                            .foregroundStyle(selected ? AdminTheme.link : Color.secondary)
                        Text(choice.label).multilineTextAlignment(.leading)
                        Spacer(minLength: 0)
                    }.padding(.vertical, 4).contentShape(Rectangle())
                }
                .buttonStyle(.plain).font(.callout)
                .accessibilityLabel(choice.label).accessibilityValue(selected ? "Selected" : "Not selected")
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityIdentifier("choice-\(question.id)-\(choice.id)")
            }
        }
    }

    private func reviewPanel(_ captured: ApplicationAnswerReview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Review your answers").font(.headline)
            Text("Submit these answers for \(captured.process.companyName), \(captured.process.title). When required answers are complete, the existing application task returns to the queue.")
                .font(.caption).foregroundStyle(.secondary)
            ForEach(captured.questions.filter { captured.values[$0.id] != nil }) { question in
                VStack(alignment: .leading, spacing: 5) {
                    Text(question.text).font(.callout.weight(.medium))
                    if let value = captured.values[question.id] {
                        Text(answerLabel(value, question: question)).font(.callout).textSelection(.enabled)
                    }
                }
                Divider()
            }
        }
    }

    private func typeName(_ type: String) -> String {
        ["text": "Written answer", "single_choice": "Choose one", "multi_choice": "Choose any"][type] ?? type.readableStatus
    }

    private func answerLabel(_ value: JSONValue, question: WorkflowQuestion) -> String {
        func label(_ id: String) -> String { question.choices.first { $0.id == id }?.label ?? id }
        switch value {
        case .string(let text): return question.type == "single_choice" ? label(text) : (text.isEmpty ? "Empty answer" : text)
        case .array(let items):
            let names = items.compactMap(\.text).map(label)
            if names.count == items.count { return names.isEmpty ? "No choices selected" : names.joined(separator: ", ") }
        default: break
        }
        guard let data = try? JSONEncoder().encode(value), let text = String(data: data, encoding: .utf8) else { return "Answer unavailable" }
        return text
    }
}
