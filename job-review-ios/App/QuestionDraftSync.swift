import Foundation
import ReviewCore

/// Reconcile device-local drafts with an authoritative question refresh.
/// Application revision changes alone do not invalidate unrelated field drafts.
struct QuestionDraftSync {
    var values: [String: JSONValue]
    var remotelyAnsweredIDs: Set<String>
    var invalidatedIDs: Set<String>

    static func shouldPollQuickly(_ workflow: WorkflowView?) -> Bool {
        guard let workflow else { return false }
        return workflow.questions.contains { !$0.hasAcceptedAnswer } ||
            (workflow.capabilities?.questionSync == true && !workflow.questionSyncAllowsAnswers)
    }

    /// Confirmation keeps the revision the user reviewed. A newer pool snapshot
    /// requires a fresh review, but must not remove the editor's draft text.
    static func confirmationHasCurrentSnapshot(_ questions: [WorkflowQuestion], workflow: WorkflowView?) -> Bool {
        guard let workflow, workflow.capabilities?.questionSync == true else { return true }
        guard workflow.questionSyncAllowsAnswers, let revision = workflow.questionSyncPoolRevision else { return false }
        return questions.allSatisfy { $0.snapshotPoolRevision == revision }
    }

    /// Select only an editor route. Canonical status and answer always come from
    /// the shared record, never from a comparison between application bindings.
    static func editorBinding(for shared: WorkflowSharedQuestion, workflow: WorkflowView) -> WorkflowQuestion? {
        let bindings = shared.bindingIDs.compactMap { id in
            workflow.questions.first { $0.id == id && $0.poolQuestionID == shared.id }
        }
        return bindings.first { question in
            workflow.processes.contains { $0.id == question.processID && $0.canAnswer && $0.formVersion == question.formVersion }
        } ?? bindings.first
    }

    /// A shared pool question is presented once, using an editable local binding
    /// when available. The server, not the client, establishes equivalence.
    static func displayQuestions(_ questions: [WorkflowQuestion], answerableProcessIDs: Set<String>) -> [WorkflowQuestion] {
        let ordered = questions.sorted {
            let lhs = answerableProcessIDs.contains($0.processID)
            let rhs = answerableProcessIDs.contains($1.processID)
            return lhs == rhs ? $0.id < $1.id : lhs
        }
        var seen: Set<String> = []
        return ordered.filter { question in
            let identity = question.poolQuestionID.map { "pool:" + $0 } ?? "local:" + question.id
            return seen.insert(identity).inserted
        }
    }

    static func reconcile(values: [String: JSONValue], previous: [WorkflowQuestion],
                          current: [WorkflowQuestion], snapshotReady: Bool = true) -> QuestionDraftSync {
        guard snapshotReady else {
            return QuestionDraftSync(values: values, remotelyAnsweredIDs: [], invalidatedIDs: [])
        }
        let old = Dictionary(uniqueKeysWithValues: previous.map { ($0.id, $0) })
        let next = Dictionary(uniqueKeysWithValues: current.map { ($0.id, $0) })
        var result = QuestionDraftSync(values: values, remotelyAnsweredIDs: [], invalidatedIDs: [])
        for question in current {
            if let before = old[question.id], question.hasAcceptedAnswer,
               !before.hasAcceptedAnswer || before.answer != question.answer || before.revision != question.revision {
                result.remotelyAnsweredIDs.insert(question.id)
            }
        }
        for id in values.keys {
            if result.remotelyAnsweredIDs.contains(id) {
                result.values.removeValue(forKey: id)
            } else if !sameBinding(old[id], next[id]) {
                result.values.removeValue(forKey: id)
                result.invalidatedIDs.insert(id)
            }
        }
        return result
    }

    private static func sameBinding(_ before: WorkflowQuestion?, _ after: WorkflowQuestion?) -> Bool {
        guard let before, let after else { return false }
        // Pool revisions change when other applications are answered. They do not
        // invalidate a draft bound to an unchanged local question revision.
        return before.hasSameAnswerBinding(as: after)
    }
}
