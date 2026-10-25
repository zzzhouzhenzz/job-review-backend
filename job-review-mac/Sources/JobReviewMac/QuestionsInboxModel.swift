import Foundation
import ReviewCore

enum QuestionsFilter: String, CaseIterable, Identifiable {
    case pending = "Pending", saved = "Saved answers", issues = "Task issues"
    var id: String { rawValue }
}

struct SharedQuestionRow: Identifiable {
    enum State { case pending, saved, unavailable }
    let id: String
    let question: WorkflowQuestion
    let process: WorkflowProcess
    let processes: [WorkflowProcess]
    let state: State
    let awaitingSyncCount: Int
}

extension DashboardModel {
    var usesCanonicalQuestions: Bool { workflow?.capabilities?.questionSync == true }
    var hasCanonicalQuestionSnapshot: Bool { !usesCanonicalQuestions || workflow?.questionSyncPoolRevision != nil }
    var canonicalQuestions: [WorkflowSharedQuestion] {
        (workflow?.sharedQuestions ?? []).sorted { $0.text == $1.text ? $0.id < $1.id : $0.text < $1.text }
    }
    var visibleCanonicalQuestions: [WorkflowSharedQuestion] {
        canonicalQuestions.filter { question in
            (questionsFilter == .saved ? question.hasAcceptedAnswer : questionsFilter == .pending && question.status == "open") &&
            (questionSearch.isEmpty || ([question.text] + canonicalBindings(question).compactMap { binding in
                processes.first { $0.id == binding.processID }?.companyName
            }).joined(separator: " ").localizedCaseInsensitiveContains(questionSearch))
        }
    }
    func canonicalBindings(_ question: WorkflowSharedQuestion) -> [WorkflowQuestion] {
        questions.filter { question.bindingIDs.contains($0.id) }.sorted { $0.id < $1.id }
    }
    func openCanonicalBinding(_ question: WorkflowQuestion) {
        guard usesCanonicalQuestions, questions.contains(question),
              let process = processes.first(where: { $0.id == question.processID }),
              question.formVersion == process.formVersion,
              tasks.contains(where: { $0.id == process.taskID }) else { return }
        page = .questions; selectedTaskID = process.taskID; focusedQuestionID = question.id
    }
    /// The server establishes shared identity. Similar text never merges scopes or forms.
    var sharedQuestionRows: [SharedQuestionRow] {
        guard !usesCanonicalQuestions else { return [] }
        let current = questions.filter { question in
            processes.contains { $0.id == question.processID && $0.formVersion == question.formVersion }
        }
        let groups = Dictionary(grouping: current) { question in
            question.poolQuestionID.map { "pool:\(question.scope):\($0)" } ?? "local:\(question.id)"
        }
        return groups.compactMap { id, bindings in
            @MainActor func priority(_ question: WorkflowQuestion) -> Int {
                guard !question.hasAcceptedAnswer else { return 0 }
                return processes.first { $0.id == question.processID }.map {
                    question.isSupported && acceptsQuestionAnswers($0)
                } == true ? 1 : 2
            }
            guard let question = bindings.sorted(by: {
                priority($0) == priority($1) ? $0.id < $1.id : priority($0) < priority($1)
            }).first, let process = processes.first(where: { $0.id == question.processID }) else { return nil }
            let linked = processes.filter { process in bindings.contains { $0.processID == process.id } }
                .sorted { $0.companyName == $1.companyName ? $0.id < $1.id : $0.companyName < $1.companyName }
            return SharedQuestionRow(id: id, question: question, process: process, processes: linked,
                state: question.hasAcceptedAnswer ? .saved : priority(question) == 1 ? .pending : .unavailable,
                awaitingSyncCount: question.hasAcceptedAnswer ? Set(bindings.filter { !$0.hasAcceptedAnswer }.map(\.processID)).count : 0)
        }.sorted { $0.question.text == $1.question.text ? $0.id < $1.id : $0.question.text < $1.question.text }
    }

    var pendingQuestionCount: Int { usesCanonicalQuestions ? canonicalQuestions.filter { $0.status == "open" }.count : sharedQuestionRows.filter { $0.state == .pending }.count }
    var savedQuestionCount: Int { usesCanonicalQuestions ? canonicalQuestions.filter(\.hasAcceptedAnswer).count : sharedQuestionRows.filter { $0.state == .saved }.count }

    /// A server-accepted shared identity is not a new prompt on another local form.
    /// Do not copy its value: local choice IDs and revision bindings can differ.
    func acceptedSharedQuestion(for question: WorkflowQuestion) -> WorkflowQuestion? {
        guard !usesCanonicalQuestions, !question.hasAcceptedAnswer, let poolID = question.poolQuestionID else { return nil }
        return questions.filter { candidate in
            candidate.hasAcceptedAnswer && candidate.poolQuestionID == poolID && candidate.scope == question.scope &&
                processes.contains { $0.id == candidate.processID && $0.formVersion == candidate.formVersion }
        }.sorted { $0.id < $1.id }.first
    }

    func hasDeferredSharedAnswers(questions: [WorkflowQuestion], values: [String: JSONValue]) -> Bool {
        questions.contains { values[$0.id] != nil && acceptedSharedQuestion(for: $0) != nil }
    }

    /// This is eligibility, not permission to write. Fresh reads and pending-save gates still apply.
    func acceptsQuestionAnswers(_ process: WorkflowProcess) -> Bool {
        guard process.canAnswer, process.blockReason == "questions_pending",
              let task = tasks.first(where: { $0.id == process.taskID }),
              task.kind == "apply", task.status == "waiting_for_user", !task.resourceQuarantined else { return false }
        return true
    }

    var questionIssueTasks: [WorkflowTask] {
        let unavailable = usesCanonicalQuestions ? Set<String>() : Set(sharedQuestionRows.filter { $0.state == .unavailable }.flatMap { $0.processes.map(\.taskID) })
        return tasks.filter { task in
            guard Self.needsAttention(task) else { return false }
            let process = process(for: task)
            return unavailable.contains(task.id) || task.resourceQuarantined ||
                (process?.blockReason ?? task.error) != "questions_pending"
        }.sorted { taskTitle($0) == taskTitle($1) ? $0.id < $1.id : taskTitle($0) < taskTitle($1) }
    }

    var visibleQuestionRows: [SharedQuestionRow] {
        sharedQuestionRows.filter { row in
            (questionsFilter == .pending ? row.state == .pending : questionsFilter == .saved && row.state == .saved) &&
            (questionSearch.isEmpty || ([row.question.text] + row.processes.map(\.companyName)).joined(separator: " ").localizedCaseInsensitiveContains(questionSearch))
        }
    }

    func showQuestions() {
        page = .questions
        selectedTaskID = nil
        focusedQuestionID = nil
    }

    func openQuestion(_ row: SharedQuestionRow) {
        guard let current = sharedQuestionRows.first(where: { $0.id == row.id }),
              tasks.contains(where: { $0.id == current.process.taskID }) else { return }
        page = .questions
        selectedTaskID = current.process.taskID
        focusedQuestionID = current.question.id
    }
}
