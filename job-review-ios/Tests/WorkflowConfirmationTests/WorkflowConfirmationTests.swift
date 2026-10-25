import XCTest
@testable import JobReviewPreview

final class WorkflowConfirmationTests: XCTestCase {
    @MainActor
    func testAcknowledgedSnapshotChangeStaysQuietThroughDismissal() async {
        let save = WorkflowConfirmationSave()
        var isCurrent = true
        var dismissed = false
        await save.run {
            XCTAssertTrue(save.isSaving)
            // AppModel publishes the acknowledged snapshot before returning to the sheet.
            isCurrent = false
            XCTAssertFalse(save.showsStale(isCurrent: isCurrent, hasPending: false))
            await Task.yield()
            XCTAssertFalse(save.showsStale(isCurrent: isCurrent, hasPending: false))
            return true
        } onConfirmed: {
            dismissed = true
            XCTAssertTrue(save.isSaving)
        }
        XCTAssertTrue(dismissed)
        // SwiftUI may render again while its dismissal animation is still running.
        XCTAssertFalse(save.showsStale(isCurrent: isCurrent, hasPending: false))
        var repeated = false
        await save.run { repeated = true; return true } onConfirmed: { XCTFail("Repeated dismissal") }
        XCTAssertFalse(repeated)
    }

    @MainActor
    func testRepeatTapDuringSaveDoesNotStartAnotherOperation() async {
        let save = WorkflowConfirmationSave()
        var operations = 0
        await save.run {
            operations += 1
            await save.run { operations += 1; return true } onConfirmed: { XCTFail("Repeated dismissal") }
            return true
        } onConfirmed: {}
        XCTAssertEqual(operations, 1)
    }

    @MainActor
    func testDefinitiveFailureRestoresRealStaleFeedback() async {
        let save = WorkflowConfirmationSave()
        XCTAssertTrue(save.showsStale(isCurrent: false, hasPending: false))
        XCTAssertFalse(save.showsStale(isCurrent: true, hasPending: false))
        await save.run { false } onConfirmed: { XCTFail("Failed save dismissed") }
        XCTAssertFalse(save.isSaving)
        XCTAssertTrue(save.showsStale(isCurrent: false, hasPending: false))
        var correctedSaveRan = false
        await save.run { correctedSaveRan = true; return true } onConfirmed: {}
        XCTAssertTrue(correctedSaveRan)
    }

    @MainActor
    func testUnknownOutcomeReleasesSendingStateAndKeepsPendingFeedback() async {
        let save = WorkflowConfirmationSave()
        await save.run { false } onConfirmed: { XCTFail("Unknown outcome dismissed") }
        XCTAssertFalse(save.isSaving)
        // The existing pending-save controls remain available instead of a stale warning.
        XCTAssertFalse(save.showsStale(isCurrent: false, hasPending: true))
        XCTAssertFalse(save.showsStale(isCurrent: true, hasPending: true))
        // A definitive retry rejection may clear pending and must reveal the changed record.
        XCTAssertTrue(save.showsStale(isCurrent: false, hasPending: false))
    }
}
