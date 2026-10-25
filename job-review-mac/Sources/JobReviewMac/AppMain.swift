import AppKit
import SwiftUI

@main
struct JobReviewMac {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = DashboardModel(demo: ProcessInfo.processInfo.arguments.contains("--demo"))
    private(set) var dashboardWindow: NSWindow?
    private(set) var statusItem: NSStatusItem?
    private var summaryItem: NSMenuItem?
    private var serverStatusItem: NSMenuItem?
    private var serverStartItem: NSMenuItem?
    private var serverStopItem: NSMenuItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        createMainMenu()
        createStatusItem()
        model.onChange = { [weak self] in self?.updateStatus() }
        model.start()
        showDashboard()
        if model.demo {
            if let pageArgument = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--page=") }),
               let page = DashboardPage.allCases.first(where: { $0.rawValue.lowercased() == String(pageArgument.dropFirst(7)).lowercased() || ($0 == .tasks && pageArgument == "--page=tasks") || ($0 == .feed && pageArgument == "--page=feed") }) {
                model.page = page
                if page == .tasks && ProcessInfo.processInfo.arguments.contains("--inspect-task") { model.selectedTaskID = "demo-task-2" }
            }
        }
        if model.demo, ProcessInfo.processInfo.arguments.contains("--ui-smoke") {
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                await self.runWindowSmoke()
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showDashboard(); return false }
    func applicationWillTerminate(_ notification: Notification) { model.stop() }
    func applicationDidBecomeActive(_ notification: Notification) { Task { await model.refresh() } }

    @objc func showDashboard() {
        if dashboardWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1220, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
            window.title = model.demo ? "Job Review — Fictional Demo" : "Job Review"
            window.titleVisibility = .hidden
            window.titlebarAppearsTransparent = true
            window.identifier = NSUserInterfaceItemIdentifier("job-review-dashboard")
            window.minSize = NSSize(width: 1000, height: 680)
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.contentView = NSHostingView(rootView: DashboardView(model: model))
            window.setFrameAutosaveName(model.demo ? "JobReviewDemoWindow" : "JobReviewDashboardWindow")
            window.center()
            dashboardWindow = window
        }
        guard let window = dashboardWindow else { return }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    @objc private func refresh() { Task { await model.refresh() } }
    @objc private func showConnection() { model.page = .connection; showDashboard() }
    @objc private func showWorkflow() { model.page = .applicationWorkflow; showDashboard() }
    @objc private func showQuestions() { model.showQuestions(); showDashboard() }
    @objc private func showBrowse() { model.page = .feed; showDashboard() }
    @objc private func quit() { NSApplication.shared.terminate(nil) }
    @objc private func startServer() {
        guard !model.isSaving, !model.projectConsent.isSaving else { return }
        Task { await model.localServer.startServer() }
    }
    @objc private func stopServer() {
        guard !model.isSaving, !model.projectConsent.isSaving else { return }
        Task { await model.localServer.stopServer() }
    }
    private func menuItem(_ title: String, action: Selector?, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }
    private func createMainMenu() {
        let root = NSMenu()
        let appItem = NSMenuItem()
        let app = NSMenu()
        app.addItem(menuItem("Open Dashboard", action: #selector(showDashboard), key: "1"))
        app.addItem(menuItem("Browse Jobs", action: #selector(showBrowse), key: "2"))
        app.addItem(menuItem("Application Workflow", action: #selector(showWorkflow), key: "3"))
        app.addItem(menuItem("Questions", action: #selector(showQuestions), key: "4"))
        app.addItem(menuItem("Connection…", action: #selector(showConnection), key: ","))
        app.addItem(.separator())
        app.addItem(menuItem("Quit Job Review", action: #selector(quit), key: "q"))
        appItem.submenu = app; root.addItem(appItem)
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "Edit")
        for (title, selector, key) in [("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(NSMenuItem(title: title, action: Selector(selector), keyEquivalent: key))
        }
        editItem.submenu = edit; root.addItem(editItem)
        let windowItem = NSMenuItem(title: "Window", action: nil, keyEquivalent: "")
        let window = NSMenu(title: "Window")
        window.addItem(menuItem("Open Dashboard", action: #selector(showDashboard), key: "1"))
        window.addItem(menuItem("Refresh", action: #selector(refresh), key: "r"))
        window.addItem(NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        window.addItem(NSMenuItem(title: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        windowItem.submenu = window; root.addItem(windowItem)
        NSApplication.shared.mainMenu = root
        NSApplication.shared.windowsMenu = window
    }
    private func createStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "square.stack.3d.up", accessibilityDescription: "Job Review")
        item.button?.imagePosition = .imageLeading
        item.button?.setAccessibilityLabel("Job Review status")
        let menu = NSMenu()
        menu.autoenablesItems = false
        let summary = menuItem("Connecting…", action: nil)
        summary.isEnabled = false
        menu.addItem(summary); summaryItem = summary
        menu.addItem(.separator())
        menu.addItem(menuItem("Open Dashboard", action: #selector(showDashboard)))
        menu.addItem(menuItem("Browse Jobs", action: #selector(showBrowse)))
        menu.addItem(menuItem("Questions", action: #selector(showQuestions)))
        menu.addItem(menuItem("Refresh", action: #selector(refresh)))
        menu.addItem(menuItem("Connection…", action: #selector(showConnection)))
        menu.addItem(.separator())
        let serverStatus = menuItem("Local server: Checking…", action: nil)
        serverStatus.isEnabled = false; menu.addItem(serverStatus); serverStatusItem = serverStatus
        let start = menuItem("Start server — resumes queued work", action: #selector(startServer))
        menu.addItem(start); serverStartItem = start
        let stop = menuItem("Stop server", action: #selector(stopServer))
        menu.addItem(stop); serverStopItem = stop
        menu.addItem(.separator())
        menu.addItem(menuItem("Quit Job Review", action: #selector(quit)))
        item.menu = menu; statusItem = item
        updateStatus()
    }
    private func updateStatus() {
        statusItem?.button?.title = " " + model.menuTitle
        statusItem?.button?.toolTip = "Job Review · \(model.phase.title)"
        summaryItem?.title = model.phase == .connected ? "\(model.running.count) active · \(model.queued.count) queued · \(model.attention.count) need attention" : model.phase.title
        serverStatusItem?.title = "Local server: \(model.localServer.title)"
        serverStartItem?.isEnabled = model.localServer.canStart && !model.isSaving && !model.projectConsent.isSaving
        serverStopItem?.isEnabled = model.localServer.canStop && !model.isSaving && !model.projectConsent.isSaving
    }
    /// Exercises the real NSWindow and menu target without a backend connection.
    private func runWindowSmoke() async {
        let original = dashboardWindow
        var serverControlsCorrect = true
        if ProcessInfo.processInfo.arguments.contains("--verify-server-controls") {
            await model.localServer.refresh()
            if ProcessInfo.processInfo.arguments.contains("--scenario=server-start-error") {
                await model.localServer.startServer()
                serverControlsCorrect = model.localServer.status?.state == .stopped && model.localServer.error != nil
            } else {
                await model.localServer.stopServer()
                updateStatus()
                serverControlsCorrect = model.localServer.canStart && serverStartItem?.isEnabled == true && serverStopItem?.isEnabled == false
                await model.localServer.startServer()
                updateStatus()
                serverControlsCorrect = serverControlsCorrect && model.localServer.status?.state == .running &&
                    serverStartItem?.isEnabled == false && serverStopItem?.isEnabled == true
                await model.localServer.stopServer()
                serverControlsCorrect = serverControlsCorrect && model.localServer.status?.state == .stopped
            }
        }
        var searchSelectionCorrect = true
        if ProcessInfo.processInfo.arguments.contains("--verify-browse-search") {
            model.page = .feed
            if let target = model.reviewFeed?.cards.last {
                model.browseSearch = target.company.name
                try? await Task.sleep(nanoseconds: 200_000_000)
                searchSelectionCorrect = model.selectedCardID == target.id
                model.browseSearch = "fictional-no-matching-company-7492"
                try? await Task.sleep(nanoseconds: 200_000_000)
                searchSelectionCorrect = searchSelectionCorrect && model.selectedCardID == nil
                model.browseSearch = ""
                try? await Task.sleep(nanoseconds: 200_000_000)
                searchSelectionCorrect = searchSelectionCorrect && model.selectedCardID == model.reviewFeed?.cards.first?.id
            } else { searchSelectionCorrect = false }
        }
        var questionSyncCorrect = true
        if ProcessInfo.processInfo.arguments.contains("--verify-question-sync") {
            model.page = .tasks; model.selectedTaskID = "demo-task-2"
            if let process = model.processes.first(where: { $0.taskID == "demo-task-2" }) {
                let questions = model.questions.filter { $0.processID == process.id }
                if questions.count >= 2 {
                    let editor = model.questionEditor(process: process, questions: questions)
                    editor.draft.setText("Fictional accepted answer", for: questions[0])
                    if let choice = questions[1].choices.first { try? editor.draft.selectChoice(choice.id, for: questions[1]) }
                    let remaining = editor.draft.values[questions[1].id]
                    model.page = .applicationWorkflow
                    try? await Task.sleep(nanoseconds: 150_000_000)
                    model.page = .tasks
                    await model.answer(process: process, questions: questions, values: [questions[0].id: .string("Fictional accepted answer")])
                    questionSyncCorrect = model.questionEditor(process: process, questions: model.questions) === editor &&
                        editor.draft.values[questions[0].id] == nil && remaining != nil && editor.draft.values[questions[1].id] == remaining &&
                        model.questions.first(where: { $0.id == questions[0].id })?.hasAcceptedAnswer == true
                    try? await Task.sleep(nanoseconds: 150_000_000)
                } else { questionSyncCorrect = false }
            } else { questionSyncCorrect = false }
        }
        var questionInboxCorrect = true
        if model.page == .questions {
            if let filter = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--questions-filter=") }) {
                model.questionsFilter = filter.hasSuffix("saved") ? .saved : filter.hasSuffix("issues") ? .issues : .pending
            }
            if ProcessInfo.processInfo.arguments.contains("--inspect-question") {
                if model.questionsFilter == .issues { model.selectedTaskID = model.questionIssueTasks.first?.id }
                else if let row = model.visibleQuestionRows.first { model.openQuestion(row) }
            }
            if ProcessInfo.processInfo.arguments.contains("--verify-questions-inbox") {
                questionInboxCorrect = model.pendingQuestionCount == 2 && model.savedQuestionCount == 1 && model.questionIssueTasks.count == 1
                if let row = model.sharedQuestionRows.first(where: { $0.question.poolQuestionID == "shared-location" }) {
                    model.openQuestion(row)
                    let questions = model.questions.filter { $0.processID == row.process.id && $0.formVersion == row.process.formVersion }
                    let editor = model.questionEditor(process: row.process, questions: questions)
                    do { try editor.draft.useSavedAnswer(for: row.question) }
                    catch { questionInboxCorrect = false }
                    let before = editor.draft.values
                    model.showTask(row.process.taskID); model.showQuestions(); model.openQuestion(row)
                    questionInboxCorrect = questionInboxCorrect && !before.isEmpty && model.selectedTaskID == row.process.taskID &&
                        model.questionEditor(process: row.process, questions: questions) === editor && editor.draft.values == before
                } else { questionInboxCorrect = false }
            }
        }
        let workflowObservationCorrect = model.page != .applicationWorkflow ||
            (model.applicationWorkflow.observation?.workers.count == 3 ||
             [.unsupported, .disconnected].contains(model.applicationWorkflow.phase))
        let expectedConsent = model.projectConsent.isPresented
        let consentPresentationCorrect = (original?.attachedSheet != nil) == expectedConsent
        if expectedConsent, let view = original?.attachedSheet?.contentView {
            renderSmokeView(view)
            model.projectConsent.dismiss()
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
        if ProcessInfo.processInfo.arguments.contains("--tall") {
            original?.setContentSize(NSSize(width: 1220, height: 1180))
        } else if ProcessInfo.processInfo.arguments.contains("--compact") {
            original?.setContentSize(NSSize(width: 1000, height: 680))
        } else {
            original?.setContentSize(NSSize(width: 1220, height: 820))
        }
        let initiallyVisible = original?.isVisible == true
        showDashboard(); showDashboard()
        let reused = dashboardWindow === original
        original?.performClose(nil)
        let closed = original?.isVisible == false
        let staysAlive = !applicationShouldTerminateAfterLastWindowClosed(NSApplication.shared)
        let menuAvailable = statusItem?.button != nil
        if let item = statusItem?.menu?.items.first(where: { $0.title == "Open Dashboard" }), let action = item.action {
            NSApplication.shared.sendAction(action, to: item.target, from: item)
        }
        let reopened = dashboardWindow === original && original?.isVisible == true
        original?.miniaturize(nil)
        showDashboard()
        let restored = original?.isMiniaturized == false && original?.isVisible == true
        let count = NSApplication.shared.windows.filter { $0.identifier?.rawValue == "job-review-dashboard" }.count
        var instructionFlowCorrect = true
        let verifiesInstructions = ProcessInfo.processInfo.arguments.contains("--verify-instructions")
        if verifiesInstructions, let task = model.tasks.first(where: { $0.id == "demo-task-2" }) {
            model.openInstructions(task)
            try? await Task.sleep(nanoseconds: 300_000_000)
            model.agentInstructions.text = "I finished sign-in in the existing browser tab. Please check it before continuing."
            instructionFlowCorrect = original?.attachedSheet != nil && model.agentInstructions.snapshot != nil
            if ProcessInfo.processInfo.arguments.contains("--submit-demo-instructions"), let selection = model.instructionSelection {
                await model.sendInstructions(selection)
                instructionFlowCorrect = instructionFlowCorrect && model.agentInstructions.text.isEmpty &&
                    model.agentInstructions.snapshot?.instructions.count == 2 && model.agentInstructions.message != nil
            }
            if ProcessInfo.processInfo.arguments.contains("--scenario=instructions-error") {
                model.agentInstructions.error = "The task changed. Your draft was kept. Refresh instructions and review the current status."
                model.agentInstructions.needsRefresh = true
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
            if let view = original?.attachedSheet?.contentView { renderSmokeView(view) }
        } else if verifiesInstructions { instructionFlowCorrect = false }
        // Let SwiftUI finish the appearance and window-restoration layout before rendering.
        try? await Task.sleep(nanoseconds: 300_000_000)
        if !expectedConsent && !verifiesInstructions, let view = original?.contentView { renderSmokeView(view) }
        let report: [String: Any] = ["fictional_data": true, "initial_visible": initiallyVisible, "one_window_reused": reused && count == 1, "close_hides_window": closed, "close_keeps_app": staysAlive, "status_item_retained": menuAvailable, "menu_reopens_same_window": reopened, "minimized_window_restored": restored, "dashboard_has_focus": original?.isKeyWindow == true, "consent_presentation_correct": consentPresentationCorrect, "search_selection_correct": searchSelectionCorrect, "instruction_flow_correct": instructionFlowCorrect, "server_controls_correct": serverControlsCorrect, "workflow_observation_correct": workflowObservationCorrect, "question_sync_correct": questionSyncCorrect, "question_inbox_correct": questionInboxCorrect]
        if let arg = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--smoke-report=") }) {
            let path = String(arg.dropFirst("--smoke-report=".count))
            do {
                let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: URL(fileURLWithPath: path), options: [.atomic])
            } catch { fputs("Window verification report could not be saved.\n", stderr) }
        }
        if ProcessInfo.processInfo.arguments.contains("--exit-after-smoke") {
            if verifiesInstructions {
                model.instructionSelection = nil
                try? await Task.sleep(nanoseconds: 300_000_000)
            }
            NSApplication.shared.terminate(nil)
        }
    }

    private func renderSmokeView(_ view: NSView) {
        guard let argument = ProcessInfo.processInfo.arguments.first(where: { $0.hasPrefix("--render=") }) else { return }
        view.layoutSubtreeIfNeeded()
        if let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                do { try data.write(to: URL(fileURLWithPath: String(argument.dropFirst(9))), options: [.atomic]) }
                catch { fputs("Fictional view render could not be saved.\n", stderr) }
            }
        }
    }
}
