import Foundation
import MacDashboardCore
import ReviewCore

extension DashboardModel {
    /// Fictional native rendering only. This path cannot contact a service or use live persistence.
    func loadQuestionSyncDemo(_ scenario: String) async throws {
        let fixture = try DemoSnapshot.make(scenario: "questions-inbox")
        var caps = fixture.feed.capabilities; caps.questionSync = true
        var questions = fixture.questions
        for index in questions.indices {
            questions[index].poolQuestionID = questions[index].poolQuestionID ?? "pool-\(questions[index].id)"
            questions[index].poolRevision = 4; questions[index].snapshotPoolRevision = 4
            questions[index].poolAnswerRevision = questions[index].hasAcceptedAnswer ? 1 : 0
            if !questions[index].hasAcceptedAnswer { questions[index].answer = nil }
        }
        var records: [[String: Any]] = []
        for id in Set(questions.compactMap(\.poolQuestionID)).sorted() {
            let bindings = questions.filter { $0.poolQuestionID == id }
            let q = bindings[0]
            records.append(["id":id,"text":q.text,"type":q.type,
                            "choices":q.choices.map { ["id":$0.id,"label":$0.label] },
                            "required":q.required,"scope":["kind":"personal","key":""],
                            "status":q.hasAcceptedAnswer ? "answered" : "open",
                            "answer":try q.answer.map { try JSONSerialization.jsonObject(with: JSONEncoder().encode($0), options: [.fragmentsAllowed]) } ?? NSNull(),
                            "answer_revision":q.hasAcceptedAnswer ? 1 : 0,
                            "answered_at":q.answeredAt as Any? ?? NSNull(),"binding_ids":bindings.map(\.id)])
        }
        records.append(["id":"saved-unbound","text":"What name do you prefer to use?","type":"text","choices":[],
                        "required":false,"scope":["kind":"personal","key":""],"status":"answered","answer":"Alex",
                        "answer_revision":2,"answered_at":"2026-10-25T12:00:00Z","binding_ids":[]])
        let shared = try JSONDecoder().decode([WorkflowSharedQuestion].self, from: JSONSerialization.data(withJSONObject: records))
        var saved = SavedWorkflowSession(origin: "https://fictional.example.test", feed: fixture.feed,
            processes: fixture.processes, questions: questions, tasks: fixture.tasks, capabilities: caps,
            health: fixture.health, savedAt: Date())
        saved.sharedQuestions = shared; saved.questionSyncPoolRevision = 4
        saved.questionSync = WorkflowQuestionSync(state: "ready", revision: 15, poolRevision: 4)
        saved.questionSyncReadAt = Date()
        let session = try WorkflowSession(api: WorkflowAPI(address: ServerAddress(saved.origin), transport: QuestionSyncDemoTransport()),
                                          persistence: QuestionSyncDemoCache(value: saved))
        var view = await session.view(); view.online = true
        if scenario.hasSuffix("loading") {
            view.questionSync = nil; view.questionSyncReadAt = nil; view.questionSyncPoolRevision = nil
            view.sharedQuestions = []
        }
        if scenario.hasSuffix("updating") || scenario.hasSuffix("pending") {
            view.questionSync?.state = scenario.hasSuffix("pending") ? "pending" : "updating"
            view.questionSync?.revision = 16
        }
        if scenario.hasSuffix("failed") { view.readErrors["question_sync"] = "Fictional sync read failed." }
        receiveWorkflow(view)
        page = .questions
        if scenario.hasSuffix("saved") { questionsFilter = .saved }
        if scenario.hasSuffix("retained"), let q = questions.first(where: { $0.type == "text" && !$0.hasAcceptedAnswer }),
           let process = processes.first(where: { $0.id == q.processID }) {
            let form = questions.filter { $0.processID == process.id }
            let editor = questionEditor(process: process, questions: form)
            editor.draft.setText("My exact unsent draft", for: q)
            var updated = view
            let index = updated.questions.firstIndex { $0.id == q.id }!
            updated.questionSync = WorkflowQuestionSync(state: "ready", revision: 17, poolRevision: 5)
            updated.questionSyncPoolRevision = 5
            for i in updated.questions.indices {
                updated.questions[i].snapshotPoolRevision = 5; updated.questions[i].poolRevision = 5
                if updated.questions[i].poolQuestionID == q.poolQuestionID {
                    updated.questions[i].answer = .string("Server answer from another device")
                    updated.questions[i].answeredAt = "2026-10-25T12:00:00Z"
                    updated.questions[i].poolAnswerRevision = 3; updated.questions[i].revision += 1
                }
            }
            if let i = updated.sharedQuestions.firstIndex(where: { $0.id == q.poolQuestionID }) {
                updated.sharedQuestions[i].status = "answered"
                updated.sharedQuestions[i].answer = .string("Server answer from another device")
                updated.sharedQuestions[i].answerRevision = 3
                updated.sharedQuestions[i].answeredAt = "2026-10-25T12:00:00Z"
            }
            receiveWorkflow(updated)
            openCanonicalBinding(updated.questions[index])
        }
    }
}

private struct QuestionSyncDemoCache: WorkflowPersistence {
    let value: SavedWorkflowSession
    func load() throws -> SavedWorkflowSession? { value }
    func save(_ value: SavedWorkflowSession) throws { throw ReviewError.invalid("Fictional preview has no persistent writes.") }
}
private struct QuestionSyncDemoTransport: ReviewTransport {
    func send(_ request: URLRequest) async throws -> HTTPResult { throw ReviewError.offline }
}
