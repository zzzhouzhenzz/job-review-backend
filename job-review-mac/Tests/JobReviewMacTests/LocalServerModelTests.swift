import XCTest
import MacDashboardCore
@testable import JobReviewMac

@MainActor final class LocalServerModelTests: XCTestCase {
    func testStoppedRunningAndRegisteredStatesExposeCorrectControls() async {
        for state in [LocalServerState.stopped, .running, .registered] {
            let service = ServerModelFixture(state: state)
            let model = LocalServerModel(service: service)
            await model.refresh()
            XCTAssertEqual(model.canStart, state == .stopped)
            XCTAssertEqual(model.canStop, state != .stopped)
            XCTAssertEqual(model.title, state == .stopped ? "Stopped" : state == .running ? "Running" : "Starting or waiting")
        }
    }
    func testStartAndStopSuccessUpdateActualState() async {
        let service = ServerModelFixture(state: .stopped)
        let model = LocalServerModel(service: service)
        await model.refresh(); await model.startServer()
        XCTAssertEqual(model.status?.state, .running)
        await model.stopServer()
        XCTAssertEqual(model.status?.state, .stopped)
        XCTAssertNil(model.error)
        let actions = await service.actions
        XCTAssertEqual(actions, ["start", "stop"])
    }
    func testMissingConfigurationDisablesBothButtons() async {
        let service = ServerModelFixture(state: .stopped)
        await service.failReads()
        let model = LocalServerModel(service: service)
        await model.refresh(); await model.startServer(); await model.stopServer()
        XCTAssertFalse(model.canStart); XCTAssertFalse(model.canStop)
        XCTAssertEqual(model.title, "Unavailable"); XCTAssertNotNil(model.error)
        let actions = await service.actions
        XCTAssertTrue(actions.isEmpty)
    }
    func testOperationFailureKeepsObservedStateAndPersistsUntilExplicitRefresh() async {
        for state in [LocalServerState.stopped, .running] {
            let service = ServerModelFixture(state: state)
            await service.failOperations()
            let model = LocalServerModel(service: service)
            await model.refresh()
            if state == .stopped { await model.startServer() } else { await model.stopServer() }
            XCTAssertEqual(model.status?.state, state); XCTAssertNotNil(model.error); XCTAssertFalse(model.isBusy)
            await model.refresh(clearOperationError: false)
            XCTAssertNotNil(model.error)
            await model.refresh()
            XCTAssertNil(model.error)
        }
    }
    func testOperationDisablesBothButtonsAndIgnoresConcurrentRefreshAndClicks() async {
        for state in [LocalServerState.stopped, .running] {
            let service = ServerModelFixture(state: state)
            let model = LocalServerModel(service: service)
            await model.refresh(); await service.hold()
            let operation = Task { if state == .stopped { await model.startServer() } else { await model.stopServer() } }
            await service.waitUntilHeld()
            XCTAssertTrue(model.isBusy); XCTAssertFalse(model.canStart); XCTAssertFalse(model.canStop)
            XCTAssertEqual(model.title, state == .stopped ? "Starting…" : "Stopping…")
            await model.startServer(); await model.stopServer(); await model.refresh()
            let reads = await service.reads, actions = await service.actions
            XCTAssertEqual(reads, 1); XCTAssertEqual(actions.count, 1)
            await service.release(); await operation.value
            XCTAssertFalse(model.isBusy)
        }
    }
    func testRefreshDoesNotInferStoppedFromNetworkFailure() async {
        let service = ServerModelFixture(state: .running)
        let model = DashboardModel(demo: true, serverService: service)
        model.loadDemo(); model.errorMessage = "HTTPS authentication failed"
        await model.localServer.refresh()
        XCTAssertEqual(model.localServer.status?.state, .running)
        XCTAssertTrue(model.localServer.canStop); XCTAssertFalse(model.localServer.canStart)
    }
    func testClosingAppMonitoringDoesNotStopServer() async {
        let service = ServerModelFixture(state: .running)
        let model = DashboardModel(demo: true, serverService: service)
        await model.localServer.refresh(); model.stop()
        let actions = await service.actions
        XCTAssertTrue(actions.isEmpty)
    }
}

private struct ServerModelError: LocalizedError { var errorDescription: String? { "Fictional server command failed" } }
private actor ServerModelFixture: LocalServerServing {
    var state: LocalServerState
    var readFails = false, operationFails = false, held = false
    var gate: CheckedContinuation<Void, Never>?, observer: CheckedContinuation<Void, Never>?
    private(set) var reads = 0
    private(set) var actions: [String] = []
    init(state: LocalServerState) { self.state = state }
    func failReads() { readFails = true }
    func failOperations() { operationFails = true }
    func hold() { held = true }
    func waitUntilHeld() async { if gate == nil { await withCheckedContinuation { observer = $0 } } }
    func release() { gate?.resume(); gate = nil; held = false }
    func status() throws -> LocalServerStatus { reads += 1; if readFails { throw ServerModelError() }; return LocalServerStatus(state) }
    func start() async throws -> LocalServerStatus { try await change("start", to: .running) }
    func stop() async throws -> LocalServerStatus { try await change("stop", to: .stopped) }
    func change(_ action: String, to next: LocalServerState) async throws -> LocalServerStatus {
        actions.append(action)
        if held { await withCheckedContinuation { gate = $0; observer?.resume(); observer = nil } }
        if operationFails { throw ServerModelError() }
        state = next; return LocalServerStatus(state)
    }
}
