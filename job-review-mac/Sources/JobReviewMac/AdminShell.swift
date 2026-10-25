import SwiftUI

struct AdminShell<Content: View>: View {
    @ObservedObject var model: DashboardModel
    @ViewBuilder let content: Content
    @State private var sidebarExpanded = !ProcessInfo.processInfo.arguments.contains("--collapsed-sidebar")
    @AppStorage("adminAppearance") private var appearance = "system"
    @State private var demoAppearance = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--appearance=") }?.split(separator: "=").last.map(String.init) ?? "light"
    private var mode: String { model.demo ? demoAppearance : appearance }
    private var scheme: ColorScheme? { mode == "dark" ? .dark : mode == "light" ? .light : nil }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: sidebarExpanded ? 224 : 68)
            VStack(spacing: 0) {
                header
                breadcrumb
                content.frame(maxWidth: .infinity, maxHeight: .infinity)
            }.background(AdminTheme.canvas)
        }
        .font(.system(size: 13)).foregroundStyle(AdminTheme.text)
        .tint(AdminTheme.primary).preferredColorScheme(scheme)
        .ignoresSafeArea(.container, edges: .top)
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "square.stack.3d.up").font(.system(size: 25, weight: .medium))
                if sidebarExpanded {
                    Text("JOB REVIEW").font(.system(size: 14, weight: .semibold)).tracking(1.0)
                }
            }.foregroundStyle(.white).frame(maxWidth: .infinity, alignment: sidebarExpanded ? .leading : .center)
                .padding(.horizontal, sidebarExpanded ? 20 : 0).frame(height: 64).padding(.top, 28)
            Rectangle().fill(.white.opacity(0.07)).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 5) {
                    navigation(.overview)
                    if sidebarExpanded { sectionLabel("WORKSPACE") }
                    navigation(.tasks)
                    navigation(.feed)
                    navigation(.questions)
                    navigation(.applicationWorkflow)
                    navigation(.activity)
                    if sidebarExpanded { sectionLabel("SETTINGS") }
                    navigation(.connection)
                }.padding(.horizontal, 8).padding(.top, 12)
            }.scrollIndicators(.hidden)
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 7) {
                    Circle().fill(model.phase == .connected ? AdminTheme.success : AdminTheme.warning).frame(width: 7, height: 7)
                    if sidebarExpanded { Text(model.phase.title).font(.system(size: 12, weight: .medium)) }
                }.foregroundStyle(.white.opacity(0.9))
                if sidebarExpanded {
                    Text(model.demo ? "Fictional workspace" : "Mac + iPhone workspace").font(.system(size: 10)).foregroundStyle(AdminTheme.sidebarMuted)
                    Text("Refreshes every 15 seconds").font(.system(size: 10)).foregroundStyle(AdminTheme.sidebarMuted)
                }
            }.padding(18).frame(maxWidth: .infinity, alignment: sidebarExpanded ? .leading : .center)
            Rectangle().fill(.white.opacity(0.07)).frame(height: 1)
            Button { sidebarExpanded.toggle() } label: {
                HStack {
                    if sidebarExpanded { Text("Collapse sidebar").font(.system(size: 11)); Spacer() }
                    Image(systemName: sidebarExpanded ? "sidebar.left" : "sidebar.right")
                }.foregroundStyle(AdminTheme.sidebarMuted).padding(17).frame(maxWidth: .infinity)
            }.buttonStyle(.plain).help(sidebarExpanded ? "Collapse sidebar" : "Expand sidebar")
                .accessibilityIdentifier("sidebar-collapse")
        }.background(AdminTheme.sidebar)
    }

    private func sectionLabel(_ title: String) -> some View {
        Text(title).font(.system(size: 10, weight: .semibold)).tracking(0.5)
            .foregroundStyle(AdminTheme.sidebarMuted).padding(.horizontal, 16).padding(.top, 22).padding(.bottom, 8)
    }

    private func navigation(_ page: DashboardPage) -> some View {
        let selected = model.page == page
        let count = page == .tasks ? model.attention.count : page == .questions ? model.pendingQuestionCount : page == .feed ? model.reviewFeed?.counts.readyCards : nil
        return Button { if page == .questions { model.showQuestions() } else { model.page = page } } label: {
            HStack(spacing: 12) {
                Image(systemName: page.icon).font(.system(size: 17, weight: .regular)).frame(width: 22)
                if sidebarExpanded {
                    Text(label(page)).font(.system(size: 13, weight: selected ? .semibold : .regular))
                    Spacer(minLength: 4)
                    if let count, count > 0 {
                        Text("\(count)").font(.system(size: 10, weight: .semibold)).monospacedDigit()
                            .foregroundStyle(.white).padding(.horizontal, 6).padding(.vertical, 3)
                            .background(page == .tasks ? AdminTheme.danger : AdminTheme.primary, in: RoundedRectangle(cornerRadius: 4))
                    }
                }
            }.foregroundStyle(selected ? .white : Color.white.opacity(0.75))
                .padding(.horizontal, sidebarExpanded ? 15 : 0).frame(maxWidth: .infinity).frame(height: 44)
                .background(selected ? AdminTheme.sidebarSelected : Color.clear, in: RoundedRectangle(cornerRadius: 6))
                .contentShape(RoundedRectangle(cornerRadius: 6))
        }.buttonStyle(.plain).help(page == .questions ? (model.hasCanonicalQuestionSnapshot ? "\(model.pendingQuestionCount) shared questions need an answer" : "Waiting for sync…") : label(page)).accessibilityLabel(label(page))
            .accessibilityValue(page == .questions ? (model.hasCanonicalQuestionSnapshot ? "\(model.pendingQuestionCount) pending" : "Waiting for sync…") : "")
            .accessibilityAddTraits(selected ? .isSelected : [])
            .accessibilityIdentifier("navigate-\(page.rawValue.lowercased())")
    }

    private var header: some View {
        HStack(spacing: 18) {
            Button { sidebarExpanded.toggle() } label: { Image(systemName: "line.3.horizontal").font(.system(size: 17)) }
                .buttonStyle(.plain).help("Toggle sidebar").accessibilityLabel("Toggle sidebar")
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(AdminTheme.muted)
                TextField("Search jobs", text: $model.browseSearch).textFieldStyle(.plain)
                    .onSubmit { model.page = .feed }.accessibilityIdentifier("workspace-job-search")
                Text("↵").foregroundStyle(AdminTheme.muted).font(.system(size: 12))
            }.padding(.horizontal, 10).frame(width: 220, height: 34).background(AdminTheme.canvas, in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(AdminTheme.border, lineWidth: 1))
            Spacer(minLength: 0)
            Button { model.taskFilter = "Attention"; model.search = ""; model.page = .tasks } label: {
                Image(systemName: model.attention.isEmpty ? "bell" : "bell.badge").font(.system(size: 17))
            }.buttonStyle(.plain).help("\(model.attention.count) tasks need attention").accessibilityLabel("Show tasks that need attention")
            Button { Task { await model.refresh() } } label: {
                if model.isRefreshing { ProgressView().controlSize(.small).frame(width: 17, height: 17) }
                else { Image(systemName: "arrow.clockwise").font(.system(size: 16)) }
            }.buttonStyle(.plain).disabled(model.isRefreshing || model.isSaving || model.demo)
                .help("Refresh now (⌘R)").accessibilityLabel("Refresh workspace").accessibilityIdentifier("refresh")
            Divider().frame(height: 24)
            Menu {
                appearanceButton("Light", value: "light")
                appearanceButton("Dark", value: "dark")
                appearanceButton("System", value: "system")
            } label: { Image(systemName: mode == "dark" ? "moon" : "sun.max").font(.system(size: 17)) }
                .menuStyle(.borderlessButton).tint(AdminTheme.link).fixedSize().help("Appearance").accessibilityLabel("Appearance")
            Button { model.page = .connection } label: {
                Text("JR").font(.system(size: 11, weight: .semibold)).foregroundStyle(AdminTheme.link)
                    .frame(width: 32, height: 32).background(AdminTheme.primary.opacity(0.12), in: Circle())
            }.buttonStyle(.plain).help("Workspace connection").accessibilityLabel("Workspace connection")
        }.foregroundStyle(AdminTheme.muted).padding(.horizontal, 24).frame(height: 64).padding(.top, 28)
            .background(AdminTheme.surface)
    }

    private func appearanceButton(_ title: String, value: String) -> some View {
        Button {
            if model.demo { demoAppearance = value } else { appearance = value }
        } label: {
            if mode == value { Label(title, systemImage: "checkmark") } else { Text(title) }
        }
    }

    private var breadcrumb: some View {
        HStack(spacing: 8) {
            Button("Workspace") { model.page = .overview }.buttonStyle(.plain).foregroundStyle(AdminTheme.link)
            Text("/").foregroundStyle(AdminTheme.muted)
            Text(label(model.page)).foregroundStyle(AdminTheme.muted)
            Spacer()
            if let date = model.workflow?.savedAt ?? model.snapshot?.fetchedAt {
                Text("Updated \(date.formatted(date: .omitted, time: .shortened))").font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
            }
        }.font(.system(size: 12)).padding(.horizontal, 24).frame(height: 38)
            .background(AdminTheme.surface).overlay(alignment: .top) { AdminTheme.border.frame(height: 1) }
            .overlay(alignment: .bottom) { AdminTheme.border.frame(height: 1) }
    }

    private func label(_ page: DashboardPage) -> String {
        switch page {
        case .overview: return "Overview"
        case .tasks: return "Dashboard"
        case .feed: return "Browse jobs"
        case .questions: return "Questions"
        case .activity: return "Activity"
        case .applicationWorkflow: return "Workflow"
        case .connection: return "Connection"
        }
    }
}
