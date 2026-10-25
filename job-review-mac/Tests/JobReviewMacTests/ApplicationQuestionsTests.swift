import Foundation
import ReviewCore
import XCTest
@testable import JobReviewMac

final class ApplicationQuestionsTests: XCTestCase {
    private func process() throws -> WorkflowProcess {
        try XCTUnwrap(DemoSnapshot.make(scenario: "active").processes.first { $0.canAnswer })
    }

    private func question(id: String = "question-text", type: String = "text", required: Bool = true,
                          answer: JSONValue? = nil) throws -> WorkflowQuestion {
        var question = try XCTUnwrap(DemoSnapshot.make(scenario: "active").questions.first)
        question.id = id
        question.type = type
        question.required = required
        question.answer = answer
        if type != "text" {
            question.choices = try JSONDecoder().decode([WorkflowChoice].self, from: Data("""
            [{"id":"office-sf","label":"San Francisco"},{"id":"office-ny","label":"New York"}]
            """.utf8))
        }
        return question
    }

    func testNewDraftNeverPrefillsSavedAnswers() throws {
        let questions = [try question(answer: .string("Existing saved answer")),
                         try question(id: "question-choice", type: "single_choice", answer: .string("office-sf"))]
        let draft = ApplicationQuestionDraft(questions: questions)
        XCTAssertTrue(draft.values.isEmpty)
        XCTAssertThrowsError(try draft.request(process: process(), questions: questions))
    }

    func testUsingSavedTextCopiesOnlyExactSelectedAnswerAndCurrentRevisions() throws {
        let saved = try question(answer: .string("  Saved answer\nwith formatting.  "))
        let untouched = try question(id: "question-untouched", answer: .string("Do not include"))
        let process = try process()
        var draft = ApplicationQuestionDraft(questions: [saved, untouched])
        try draft.useSavedAnswer(for: saved)
        XCTAssertEqual(draft.values, [saved.id: saved.answer!])
        let request = try draft.request(process: process, questions: [saved, untouched])
        XCTAssertEqual(request.processID, process.id)
        XCTAssertEqual(request.processRevision, process.revision)
        XCTAssertEqual(request.formVersion, process.formVersion)
        XCTAssertEqual(request.answers.map(\.questionID), [saved.id])
        XCTAssertEqual(request.answers.first?.questionRevision, saved.revision)
        XCTAssertEqual(request.answers.first?.value, saved.answer)
    }

    func testUsingSavedChoicesCopiesExactIDsAndPreservesSavedOrder() throws {
        let single = try question(type: "single_choice", answer: .string("office-sf"))
        let multi = try question(id: "question-multi", type: "multi_choice",
                                 answer: .array([.string("office-sf"), .string("office-ny")]))
        var draft = ApplicationQuestionDraft()
        try draft.useSavedAnswer(for: single)
        try draft.useSavedAnswer(for: multi)
        XCTAssertEqual(draft.values[single.id], .string("office-sf"))
        XCTAssertEqual(draft.values[multi.id], .array([.string("office-sf"), .string("office-ny")]))
        XCTAssertEqual(try draft.request(process: process(), questions: [single, multi]).answers.count, 2)
    }

    func testInvalidSavedAnswerCannotReplaceAnExistingDraft() throws {
        var question = try question(type: "single_choice", answer: .string("San Francisco"))
        var draft = ApplicationQuestionDraft()
        try draft.selectChoice("office-ny", for: question)
        for answer in [JSONValue.string("San Francisco"), .string("removed-choice"), .null] {
            question.answer = answer
            XCTAssertThrowsError(try draft.useSavedAnswer(for: question))
            XCTAssertEqual(draft.values[question.id], .string("office-ny"))
        }
        question.answer = nil
        XCTAssertThrowsError(try draft.useSavedAnswer(for: question))
        question.answer = .string("office-sf")
        question.scope = "profile"
        XCTAssertThrowsError(try draft.useSavedAnswer(for: question))
        question.scope = "application"
        question.type = "file_upload"
        XCTAssertThrowsError(try draft.useSavedAnswer(for: question))
        XCTAssertEqual(draft.values[question.id], .string("office-ny"))
    }

    func testSavedMultipleChoiceRejectsDuplicateAndUnknownIDs() throws {
        var question = try question(type: "multi_choice")
        var draft = ApplicationQuestionDraft()
        for answer in [JSONValue.array([.string("office-sf"), .string("office-sf")]),
                       .array([.string("San Francisco")]), .array([])] {
            question.answer = answer
            XCTAssertThrowsError(try draft.useSavedAnswer(for: question))
            XCTAssertTrue(draft.values.isEmpty)
        }
    }

    func testSingleChoiceSendsExactIDAndRejectsDisplayLabel() throws {
        let question = try question(type: "single_choice")
        var draft = ApplicationQuestionDraft()
        XCTAssertThrowsError(try draft.selectChoice("San Francisco", for: question))
        XCTAssertTrue(draft.values.isEmpty)
        try draft.selectChoice("office-sf", for: question)
        let request = try draft.request(process: process(), questions: [question])
        XCTAssertEqual(request.answers.first?.value, .string("office-sf"))
        XCTAssertEqual(request.answers.first?.questionRevision, question.revision)
    }

    func testMultipleChoiceUsesUniqueIDsAndExplicitEmptySelection() throws {
        let question = try question(type: "multi_choice", required: false)
        var draft = ApplicationQuestionDraft()
        try draft.toggleChoice("office-sf", for: question)
        try draft.toggleChoice("office-ny", for: question)
        XCTAssertEqual(draft.values[question.id], .array([.string("office-ny"), .string("office-sf")]))
        try draft.toggleChoice("office-sf", for: question)
        try draft.toggleChoice("office-ny", for: question)
        XCTAssertEqual(try draft.request(process: process(), questions: [question]).answers.first?.value, .array([]))
        XCTAssertThrowsError(try draft.toggleChoice("New York", for: question))
    }

    func testUntouchedOptionalAndSavedRequiredAnswersAreOmitted() throws {
        let supplied = try question()
        let optional = try question(id: "question-optional", required: false)
        let saved = try question(id: "question-saved", answer: .string("Saved required answer"))
        var draft = ApplicationQuestionDraft(questions: [supplied, optional, saved])
        draft.setText("My new answer", for: supplied)
        let request = try draft.request(process: process(), questions: [supplied, optional, saved])
        XCTAssertEqual(request.answers.map(\.questionID), [supplied.id])
        XCTAssertEqual(request.answers.first?.value, .string("My new answer"))
        draft.remove(supplied)
        XCTAssertTrue(draft.values.isEmpty)
    }

    func testRequiredBlankTextIsOmittedAndDoesNotBlockPartialChoiceSave() throws {
        let required = try question()
        let selected = try question(id: "question-choice", type: "single_choice")
        var draft = ApplicationQuestionDraft()
        try draft.selectChoice("office-sf", for: selected)
        for text in ["", " ", "\n  "] {
            draft.setText("Replaced answer", for: required)
            draft.setText(text, for: required)
            XCTAssertNil(draft.values[required.id])
            XCTAssertNil(draft.validationMessage(for: required))
            let request = try draft.request(process: process(), questions: [required, selected])
            XCTAssertEqual(request.answers.map(\.questionID), [selected.id])
            XCTAssertEqual(request.answers.first?.value, .string("office-sf"))
        }
    }

    func testOptionalExplicitBlankTextRemainsAnAnswer() throws {
        let optional = try question(required: false)
        var draft = ApplicationQuestionDraft()
        draft.setText("", for: optional)
        XCTAssertEqual(try draft.request(process: process(), questions: [optional]).answers.first?.value, .string(""))
        XCTAssertNil(draft.validationMessage(for: optional))
    }

    func testTextValidationUsesReviewCoreLengthLimits() throws {
        let question = try question()
        var draft = ApplicationQuestionDraft()
        draft.setText(String(repeating: "a", count: 20_001), for: question)
        XCTAssertThrowsError(try draft.request(process: process(), questions: [question]))
        XCTAssertNotNil(draft.validationMessage(process: try process(), questions: [question]))
        XCTAssertNotNil(draft.validationMessage(for: question))
        draft.setText("A complete answer", for: question)
        XCTAssertNil(draft.validationMessage(process: try process(), questions: [question]))
        XCTAssertNil(draft.validationMessage(for: question))
    }

    func testRequiredMultipleChoiceCannotSaveAnEmptyDraft() throws {
        let question = try question(type: "multi_choice")
        var draft = ApplicationQuestionDraft()
        try draft.toggleChoice("office-sf", for: question)
        try draft.toggleChoice("office-sf", for: question)
        XCTAssertThrowsError(try draft.request(process: process(), questions: [question]))
    }

    func testUnsupportedQuestionCannotBeSaved() throws {
        var question = try question()
        question.scope = "profile"
        var draft = ApplicationQuestionDraft()
        draft.setText("Must not change profile", for: question)
        XCTAssertThrowsError(try draft.request(process: process(), questions: [question]))
        question.scope = "application"
        question.type = "file_upload"
        XCTAssertThrowsError(try draft.request(process: process(), questions: [question]))
    }

    func testVersionIdentityChangesForEveryProcessAndFormRevision() throws {
        let original = try process()
        let question = try question()
        let identity = ApplicationQuestionIdentity(process: original, questions: [question])
        var changedProcess = original
        changedProcess.revision += 1
        XCTAssertNotEqual(identity, ApplicationQuestionIdentity(process: changedProcess, questions: [question]))
        changedProcess = original
        changedProcess.formVersion = "new-form"
        XCTAssertNotEqual(identity, ApplicationQuestionIdentity(process: changedProcess, questions: [question]))
        var changedQuestion = question
        changedQuestion.revision += 1
        XCTAssertNotEqual(identity, ApplicationQuestionIdentity(process: original, questions: [changedQuestion]))
        changedQuestion = question
        changedQuestion.formVersion = "new-form"
        XCTAssertNotEqual(identity, ApplicationQuestionIdentity(process: original, questions: [changedQuestion]))
        XCTAssertNotEqual(identity, ApplicationQuestionIdentity(process: original, questions: []))
    }

    func testQuestionOrderDoesNotInvalidateDraftButMismatchedFormDoes() throws {
        let process = try process()
        let first = try question()
        var second = try question(id: "question-second", required: false)
        XCTAssertEqual(ApplicationQuestionIdentity(process: process, questions: [first, second]),
                       ApplicationQuestionIdentity(process: process, questions: [second, first]))
        var draft = ApplicationQuestionDraft()
        draft.setText("An answer", for: first)
        second.formVersion = "older-form"
        XCTAssertThrowsError(try draft.request(process: process, questions: [first, second]))
    }
}
