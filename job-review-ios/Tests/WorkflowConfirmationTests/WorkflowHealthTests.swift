import XCTest
@testable import ReviewCore
@testable import JobReviewPreview

final class WorkflowHealthTests: XCTestCase {
    private let checked = Date(timeIntervalSince1970: 1_792_224_000) // 2026-10-17 08:00 UTC

    private func view(status: String, workerEnabled: Bool = true,
                      readErrors: [String: String] = [:]) -> WorkflowView {
        let capabilities = WorkflowCapabilities(
            apiVersion: 1, workerEnabled: workerEnabled, workerStatus: workerEnabled ? "configured" : "not_configured",
            explicitApply: true, questions: true, feedDelivery: "poll", questionScopes: ["application"], remoteAccess: true)
        let ready = status == "ready"
        let health = WorkflowHealth(
            bootID: "fictional-monitor", status: status, ready: ready,
            checkedAt: "2026-10-17T08:00:00Z", expiresAt: ready ? "2026-10-17T08:05:00Z" : nil,
            retryAt: nil, checks: [
                WorkflowHealthCheck(id: "storage", status: "passed", message: "Saved data is available."),
                WorkflowHealthCheck(id: "chrome_attachment", status: ready ? "passed" : "on_demand", message: "Check when needed."),
                WorkflowHealthCheck(id: "linkedin_auth", status: ready ? "passed" : "on_demand", message: "Check when needed.")
            ])
        return WorkflowView(feed: nil, processes: [], questions: [], tasks: [], capabilities: capabilities,
                            health: health, pending: nil, lastAcknowledgement: nil, savedAt: nil,
                            online: true, readErrors: readErrors)
    }

    func testIdleBrowserDoesNotClaimConfiguredWorkerIsBlocked() {
        for status in ["idle", "on_demand"] {
            let summary = workflowHealthSummary(view(status: status), online: true, at: checked)
            XCTAssertEqual(summary.title, "Browser checks run when needed")
            XCTAssertTrue(summary.detail.contains("Ranking can run without Chrome"))
            XCTAssertTrue(summary.detail.contains("Check each task"))
            XCTAssertNotEqual(summary.symbol, "pause.circle")
        }
    }

    func testReadyBrowserRequiresCurrentOnlineChecks() {
        let ready = view(status: "ready")
        XCTAssertEqual(workflowHealthSummary(ready, online: true, at: checked).title, "Browser checks passed")
        XCTAssertEqual(workflowHealthSummary(ready, online: false, at: checked).title, "Service status unavailable")
        XCTAssertEqual(workflowHealthSummary(ready, online: true, at: checked.addingTimeInterval(300)).title,
                       "Browser checks are not current")
        XCTAssertEqual(workflowHealthSummary(view(status: "ready", readErrors: ["health": "Read failed"]),
                                             online: true, at: checked).title, "Service status unavailable")
    }

    func testCachedIdleStateDoesNotClaimCurrentBrowserStatus() {
        XCTAssertEqual(workflowHealthSummary(view(status: "idle"), online: false, at: checked).title,
                       "Service status unavailable")
        XCTAssertEqual(workflowHealthSummary(view(status: "idle", readErrors: ["health": "Read failed"]),
                                             online: true, at: checked).title, "Service status unavailable")
    }

    func testBrowserFailuresDoNotClaimAllTasksMustWait() {
        for (status, title) in ["checking": "Browser checks running", "blocked": "Browser checks blocked",
                                "not_configured": "Browser not configured", "timed_out": "Browser checks timed out"] {
            let summary = workflowHealthSummary(view(status: status), online: true, at: checked)
            XCTAssertEqual(summary.title, title)
            XCTAssertFalse(summary.title.contains("Work stays queued"))
            XCTAssertTrue(summary.detail.contains("Ranking can run without Chrome"))
            XCTAssertTrue(summary.detail.contains("each task"))
        }
    }

    func testUnconfiguredWorkerRemainsExplicitEvenWithPassedBrowserChecks() {
        let summary = workflowHealthSummary(view(status: "ready", workerEnabled: false), online: true, at: checked)
        XCTAssertEqual(summary.title, "Worker off · Work stays queued")
    }

    func testStorageChecksAreNotLabeledAsBrowserChecks() {
        var checking = view(status: "checking")
        checking.health?.checks[0].status = "checking"
        XCTAssertEqual(workflowHealthSummary(checking, online: true, at: checked).title, "Saved data check: Checking")
        var failed = view(status: "blocked")
        failed.health?.checks[0].status = "failed"
        XCTAssertEqual(workflowHealthSummary(failed, online: true, at: checked).title, "Saved data check: Failed")
    }

    func testOnDemandCheckHasReadableLabelAndUnknownStatesRemainVisible() {
        XCTAssertEqual(workflowCheckStatus("on_demand"), "Checked when needed")
        XCTAssertEqual(workflowCheckStatus("idle"), "Idle")
        XCTAssertEqual(workflowCheckStatus("future_state"), "future_state")
        XCTAssertEqual(workflowHealthSummary(view(status: "future_state"), online: true, at: checked).title,
                       "Browser status: future_state")
    }
}
