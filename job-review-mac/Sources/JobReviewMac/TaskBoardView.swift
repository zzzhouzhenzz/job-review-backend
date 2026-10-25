import SwiftUI
import ReviewCore

/// The board projects backend state. Selecting a card never changes its task.
struct TaskBoardView: View {
    @ObservedObject var model: DashboardModel

    private var columns: [TaskBoardColumn] {
        TaskBoardColumn.allCases.filter { column in
            column != .other || model.filteredTasks.contains { TaskBoardColumn.column(for: $0) == .other }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Dashboard").font(.system(size: 23, weight: .semibold)).foregroundStyle(AdminTheme.text)
                    Text("Follow each task. Select a card for details.").font(.callout).foregroundStyle(AdminTheme.muted)
                }
                Spacer(minLength: 8)
                Text("\(model.filteredTasks.count) tasks").font(.callout.monospacedDigit()).foregroundStyle(AdminTheme.muted)
            }
            HStack(spacing: 10) { metricCards }
            HStack(spacing: 12) {
                TextField("Search company, role, or task", text: $model.search)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 330)
                    .accessibilityIdentifier("board-search")
                Spacer(minLength: 0)
                Picker("Show", selection: $model.taskFilter) {
                    ForEach(["All", "Active", "Queued", "Attention", "Finished"], id: \.self) { value in
                        Text(value == "All" ? "All tasks" : value == "Active" ? "Running" : value).tag(value)
                    }
                }.pickerStyle(.menu).fixedSize().accessibilityIdentifier("board-filter")
            }
            GeometryReader { geometry in
                let width = max(220, (geometry.size.width - CGFloat(columns.count - 1) * 12) / CGFloat(columns.count))
                ScrollViewReader { proxy in
                    ScrollView(.horizontal) {
                        HStack(alignment: .top, spacing: 12) {
                            ForEach(columns) { column in
                                boardColumn(column)
                                    .frame(width: width, height: max(120, geometry.size.height - 12))
                                    .id(column.id)
                            }
                        }.padding(.bottom, 12)
                    }
                    .onAppear { showSelectedColumn(using: proxy) }
                    .onChange(of: model.selectedTaskID) { _, _ in showSelectedColumn(using: proxy) }
                    .onChange(of: geometry.size.width) { _, _ in showSelectedColumn(using: proxy) }
                }
            }
            Text(model.phase == .stale ? "Last complete snapshot · Current task activity is unknown" : "Task status updates automatically")
                .font(.caption2).foregroundStyle(AdminTheme.muted)
        }.padding(20)
        .frame(minWidth: 440, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(AdminTheme.canvas)
    }

    private var metricCards: some View {
        ForEach([TaskBoardColumn.queued, .running, .attention, .finished]) { column in
            AdminMetricCard(
                title: column == .attention ? "Attention" : column.title,
                value: String(model.tasks.filter { TaskBoardColumn.column(for: $0) == column }.count),
                detail: metricDetail(column), icon: metricIcon(column), color: color(column)
            ) { model.taskFilter = column.filterName }
        }
    }

    private func metricDetail(_ column: TaskBoardColumn) -> String {
        switch column {
        case .queued: return "Waiting or scheduled"
        case .running: return "In progress"
        case .attention: return "Review required"
        case .finished: return "Completed or cancelled"
        case .other: return "Other task states"
        }
    }

    private func metricIcon(_ column: TaskBoardColumn) -> String {
        switch column {
        case .queued: return "clock"
        case .running: return "arrow.triangle.2.circlepath"
        case .attention: return "exclamationmark.circle"
        case .finished: return "checkmark.circle"
        case .other: return "ellipsis.circle"
        }
    }

    private func showSelectedColumn(using proxy: ScrollViewProxy) {
        guard let task = model.filteredTasks.first(where: { $0.id == model.selectedTaskID }) else { return }
        // Let the inspector and column frames finish their layout before scrolling.
        DispatchQueue.main.async {
            proxy.scrollTo(TaskBoardColumn.column(for: task).id, anchor: .center)
        }
    }

    private func boardColumn(_ column: TaskBoardColumn) -> some View {
        let tasks = model.filteredTasks.filter { TaskBoardColumn.column(for: $0) == column }
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 7) {
                Circle().fill(color(column)).frame(width: 7, height: 7)
                Text(column.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(AdminTheme.text)
                Spacer(minLength: 4)
                Text("\(tasks.count)").font(.system(size: 11, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(AdminTheme.muted)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(AdminTheme.canvas, in: RoundedRectangle(cornerRadius: 4))
            }.padding(13)
            Rectangle().fill(AdminTheme.border).frame(height: 1)
            ScrollView(.vertical) {
                LazyVStack(spacing: 10) {
                    if tasks.isEmpty {
                        VStack(spacing: 7) {
                            Image(systemName: "minus").font(.system(size: 18, weight: .light)).foregroundStyle(AdminTheme.muted)
                            Text(emptyText(column)).font(.caption).foregroundStyle(AdminTheme.muted)
                        }.frame(maxWidth: .infinity).padding(.vertical, 20)
                    }
                    ForEach(tasks) { task in boardCard(task) }
                }.padding(10)
            }
        }
        .background(AdminTheme.canvas.opacity(0.65), in: RoundedRectangle(cornerRadius: AdminTheme.radius))
        .adminPanel()
        .accessibilityIdentifier("board-column-\(column.rawValue)")
    }

    private func boardCard(_ task: WorkflowTask) -> some View {
        let selected = model.selectedTaskID == task.id
        let process = model.process(for: task)
        let questions = model.unansweredQuestions.filter { $0.processID == process?.id }.count
        return Button { model.selectedTaskID = task.id } label: {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 5) {
                    Image(systemName: task.kind == "apply" ? "paperplane" : task.kind == "rank" ? "line.3.horizontal.decrease" : "magnifyingglass")
                    Text(task.kind.readableStatus)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
                }.font(.system(size: 10, weight: .medium)).foregroundStyle(AdminTheme.muted)
                VStack(alignment: .leading, spacing: 6) {
                    Text(model.taskTitle(task)).font(.system(size: 14, weight: .semibold)).foregroundStyle(AdminTheme.text).lineLimit(2)
                    Text(model.taskSubtitle(task)).font(.system(size: 12)).foregroundStyle(AdminTheme.muted).lineLimit(3)
                }
                StatusLabel(status: task.status)
                if task.resourceQuarantined {
                    Label("Browser work held", systemImage: "exclamationmark.shield")
                        .font(.caption).foregroundStyle(AdminTheme.warningText)
                } else if questions > 0 {
                    Label("\(questions) answer\(questions == 1 ? "" : "s") needed", systemImage: "bubble.left")
                        .font(.caption).foregroundStyle(AdminTheme.warningText)
                } else if let reason = process?.blockReason ?? task.error, !reason.isEmpty {
                    Text(reason.contains(" ") ? reason.safeDiagnostic : reason.safeDiagnostic.readableStatus)
                        .font(.caption).foregroundStyle(AdminTheme.muted).lineLimit(2)
                }
                if let process, ["submitted", "submission_unknown"].contains(process.submissionStatus) {
                    Label(process.submissionStatus.readableStatus, systemImage: process.submissionStatus == "submitted" ? "checkmark.seal" : "exclamationmark.circle")
                        .font(.caption).foregroundStyle(statusColor(process.submissionStatus))
                }
                Divider()
                if task.status == "retry_wait", let date = timestamp(task.availableAt) {
                    Text("Retry after \(date.formatted(date: .abbreviated, time: .shortened))")
                        .font(.system(size: 10)).foregroundStyle(AdminTheme.muted)
                } else if let date = timestamp(task.updatedAt) {
                    Text("Updated \(date.formatted(date: .abbreviated, time: .shortened))")
                        .font(.system(size: 10)).foregroundStyle(AdminTheme.muted)
                }
            }
            .padding(13).frame(maxWidth: .infinity, alignment: .leading)
            .background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: AdminTheme.radius))
            .overlay(RoundedRectangle(cornerRadius: AdminTheme.radius).stroke(selected ? AdminTheme.primary : AdminTheme.border, lineWidth: selected ? 2 : 1))
            .contentShape(RoundedRectangle(cornerRadius: AdminTheme.radius))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("board-card-\(task.id)")
        .accessibilityLabel("\(model.taskTitle(task)), \(model.taskSubtitle(task)), \(task.status.readableStatus)")
        .accessibilityHint("Show task details")
    }

    private func color(_ column: TaskBoardColumn) -> Color {
        switch column {
        case .queued: return AdminTheme.warning
        case .running: return AdminTheme.info
        case .attention: return AdminTheme.danger
        case .finished: return AdminTheme.success
        case .other: return AdminTheme.muted
        }
    }
    private func emptyText(_ column: TaskBoardColumn) -> String {
        if !model.search.isEmpty || model.taskFilter != "All" { return "No matching tasks" }
        switch column {
        case .queued: return "Nothing queued"
        case .running: return "No active tasks"
        case .attention: return "Nothing needs attention"
        case .finished: return "No finished tasks"
        case .other: return "No other tasks"
        }
    }
}
