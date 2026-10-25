import Combine
import Foundation
import ReviewCore

extension WorkflowQuestion {
    func matchesAccepted(_ value: JSONValue) -> Bool {
        guard hasAcceptedAnswer, let answer else { return false }
        if type == "multi_choice", (try? validate(value)) != nil, (try? validate(answer)) != nil {
            return Set(value.array.compactMap(\.text)) == Set(answer.array.compactMap(\.text))
        }
        return answer == value
    }
    func sameAnswerField(as other: WorkflowQuestion) -> Bool {
        id == other.id && processID == other.processID && formVersion == other.formVersion &&
        text == other.text && type == other.type && choices == other.choices && required == other.required && scope == other.scope
    }
}

/// In-memory editor state stays outside the view lifetime. The backend remains authoritative.
@MainActor final class ApplicationQuestionEditor: ObservableObject {
    struct RetainedDraft: Identifiable {
        let question: WorkflowQuestion
        let value: JSONValue
        var id: String { question.id }
    }
    @Published var draft = ApplicationQuestionDraft()
    @Published var review: ApplicationAnswerReview?
    @Published var notice: String?
    @Published var correctionIDs = Set<String>()
    @Published var retainedDrafts: [String: RetainedDraft] = [:]
    private var previous: [WorkflowQuestion] = []
    private var identity: ApplicationQuestionIdentity?

    func synchronize(process: WorkflowProcess, questions: [WorkflowQuestion], preservingDrafts: Bool = false) {
        let next = ApplicationQuestionIdentity(process: process, questions: questions)
        guard identity != next || previous != questions else { return }
        let oldValues = draft.values
        var accepted = false, conflict = false, changed = false
        for (id, value) in oldValues {
            if preservingDrafts, let old = previous.first(where: { $0.id == id }) {
                guard let current = questions.first(where: { $0.id == id }), current.sameAnswerField(as: old) else {
                    retainedDrafts[id] = RetainedDraft(question: old, value: value)
                    draft.removeID(id); correctionIDs.remove(id); changed = true; continue
                }
                if current.hasAcceptedAnswer, !(correctionIDs.contains(id) && current.hasSameAnswerBinding(as: old)) {
                    if !current.matchesAccepted(value) {
                        retainedDrafts[id] = RetainedDraft(question: old, value: value); conflict = true
                    }
                    draft.removeID(id); correctionIDs.remove(id); accepted = true
                }
                continue
            }
            guard let old = previous.first(where: { $0.id == id }),
                  let current = questions.first(where: { $0.id == id }),
                  current.sameAnswerField(as: old), old.revision == current.revision || current.hasAcceptedAnswer else {
                draft.removeID(id); correctionIDs.remove(id); changed = true; continue
            }
            if current.hasAcceptedAnswer {
                if correctionIDs.contains(id) && current.hasSameAnswerBinding(as: old) { continue }
                correctionIDs.remove(id)
                draft.removeID(id); accepted = true
                if !current.matchesAccepted(value) { conflict = true }
            }
        }
        // Review authorization is tied to exact revisions, even when unaffected drafts survive.
        if let captured = review {
            let unchangedBindings = previous.count == questions.count && previous.allSatisfy { old in
                questions.contains { $0.sameAnswerField(as: old) && $0.revision == old.revision && $0.answer == old.answer && $0.answeredAt == old.answeredAt }
            }
            if identity != next || !unchangedBindings { review = nil }
            else {
                // Only global pool metadata changed. Keep the reviewed local bindings current.
                review = ApplicationAnswerReview(process: process, questions: questions, values: captured.values, correctionIDs: captured.correctionIDs)
            }
        }
        if conflict { notice = preservingDrafts ? "The server has a different answer. Your draft is kept below for review." : "A different answer was accepted on another device. The current server answer is shown. Other drafts were kept." }
        else if accepted { notice = "Answers accepted by the server are now shown. Other drafts were kept." }
        else if changed { notice = preservingDrafts ? "Changed questions need review. Their drafts are kept below; unchanged drafts remain in the form." : "Changed questions need review. Drafts for unchanged questions were kept." }
        previous = questions; identity = next
    }
}

extension DashboardModel {
    func questionEditor(process: WorkflowProcess, questions: [WorkflowQuestion]) -> ApplicationQuestionEditor {
        let key = "\(settings?.origin ?? (demo ? "fictional" : "unconfigured"))|\(process.id)|\(process.formVersion ?? "")"
        if let editor = questionEditors[key] { return editor }
        let editor = ApplicationQuestionEditor()
        editor.synchronize(process: process, questions: questions, preservingDrafts: usesCanonicalQuestions)
        questionEditors[key] = editor
        return editor
    }

    func synchronizeQuestionEditors() {
        guard !usesCanonicalQuestions || workflow?.questionSyncAllowsAnswers == true else { return }
        for process in processes {
            let key = "\(settings?.origin ?? (demo ? "fictional" : "unconfigured"))|\(process.id)|\(process.formVersion ?? "")"
            questionEditors[key]?.synchronize(process: process, questions: questions.filter { $0.processID == process.id && $0.formVersion == process.formVersion }, preservingDrafts: usesCanonicalQuestions)
        }
    }

    /// Equal fresh server answers confirm outcome only when no uncertain command remains.
    func reconcileAcceptedAnswers(process: WorkflowProcess, questions original: [WorkflowQuestion], values: [String: JSONValue]) -> Bool {
        guard !values.isEmpty, pendingSave == nil, workflow?.online == true,
              workflow?.questionSyncAllowsAnswers == true,
              workflow?.readErrors["questions"] == nil, workflow?.readErrors["processes"] == nil,
              let workflowReadAt, Date().timeIntervalSince(workflowReadAt) <= 45,
              processes.contains(where: { $0.id == process.id && $0.formVersion == process.formVersion }) else { return false }
        let current = values.keys.compactMap { id in
            questions.first { $0.id == id && $0.processID == process.id && $0.formVersion == process.formVersion }
        }
        guard current.count == values.count, current.allSatisfy({ q in
            q.hasAcceptedAnswer && original.first(where: { $0.id == q.id }).map(q.sameAnswerField) == true
        }) else { return false }
        if current.allSatisfy({ $0.matchesAccepted(values[$0.id]!) }) {
            commandError = nil; commandMessage = "These answers are already accepted by the server. No answers were sent again."
        } else {
            commandMessage = nil; commandError = "Different answers were accepted on another device. Review the current server answers. Nothing was overwritten."
        }
        synchronizeQuestionEditors()
        return true
    }
}
