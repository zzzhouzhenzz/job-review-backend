import SwiftUI
import ReviewCore

/// An overview of the same workspace used by Browse and the Board.
/// Counts describe the latest available snapshot; no trends are inferred.
struct AdminOverviewView: View {
    @ObservedObject var model: DashboardModel

    private var columns: [TaskBoardColumn] {
        TaskBoardColumn.allCases.filter { $0 != .other || count(in: .other) > 0 }
    }
    private var recentProcesses: [WorkflowProcess] {
        Array(model.processes.sorted { $0.updatedAt > $1.updatedAt }.prefix(5))
    }
    private var currentReport: String {
        model.phase == .connected ? "Latest workspace update" : "Last available workspace update"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                heading
                metrics
                workload
                applications
                activity
            }
            .padding(24)
            .frame(maxWidth: 1440, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(AdminTheme.canvas)
    }

    private var heading: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Overview").font(.system(size: 26, weight: .semibold)).foregroundStyle(AdminTheme.text)
                Text("Review opportunities and follow your application progress.")
                    .font(.system(size: 13)).foregroundStyle(AdminTheme.muted)
            }
            Spacer(minLength: 0)
            Button { model.page = .feed } label: {
                Label("Browse jobs", systemImage: "arrow.right")
            }
            .buttonStyle(.borderedProminent)
            .tint(AdminTheme.primary)
            .controlSize(.large)
            .accessibilityIdentifier("overview-browse")
        }
    }

    private var metrics: some View {
        HStack(spacing: 14) {
            AdminMetricCard(title: "Ready jobs", value: model.reviewFeed.map { String($0.counts.readyJobs) } ?? "Unknown",
                            detail: model.reviewFeed.map { "Across \($0.counts.readyCards) companies" } ?? "Feed unavailable",
                            icon: "briefcase", color: AdminTheme.primary) { model.page = .feed }
            AdminMetricCard(title: "Running", value: String(count(in: .running)), detail: "Tasks in progress",
                            icon: "arrow.triangle.2.circlepath", color: AdminTheme.info) { show(.running) }
            AdminMetricCard(title: "Queued", value: String(count(in: .queued)), detail: "Includes scheduled retries",
                            icon: "clock", color: AdminTheme.warning) { show(.queued) }
            AdminMetricCard(title: "Needs attention", value: String(count(in: .attention)), detail: "Review or answers needed",
                            icon: "exclamationmark.bubble", color: AdminTheme.danger) { show(.attention) }
        }
    }

    private var workload: some View {
        VStack(alignment: .leading, spacing: 0) {
            panelHeading("Task status", detail: currentReport, action: "Open Dashboard") {
                model.search = ""; model.taskFilter = "All"; model.page = .tasks
            }
            Divider().overlay(AdminTheme.border)
            HStack(alignment: .top, spacing: 28) {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(model.tasks.count)").font(.system(size: 30, weight: .semibold)).monospacedDigit()
                        Text("tasks in this workspace").font(.system(size: 12)).foregroundStyle(AdminTheme.muted)
                    }
                    ForEach(columns) { column in
                        Button { show(column) } label: {
                            distributionRow(column)
                        }
                        .buttonStyle(.plain)
                        .help("Show \(column.title.lowercased()) tasks")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                VStack(alignment: .leading, spacing: 16) {
                    Text("APPLICATIONS").font(.system(size: 10, weight: .semibold)).tracking(0.8).foregroundStyle(AdminTheme.muted)
                    applicationMetric("Submitted", value: model.processes.filter { $0.submissionStatus == "submitted" }.count,
                                      color: AdminTheme.successText, detail: "Confirmed by submission evidence")
                    applicationMetric("Submission unconfirmed", value: model.processes.filter { $0.submissionStatus == "submission_unknown" }.count,
                                      color: AdminTheme.warningText, detail: "Open the task to review")
                    Text("Finished tasks can include discovery, ranking, or cancelled work. Check submission status for each application.")
                        .font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: 230, alignment: .leading)
                .padding(.leading, 24)
                .overlay(alignment: .leading) { Rectangle().fill(AdminTheme.border).frame(width: 1) }
            }
            .padding(20)
        }
        .adminPanel()
    }

    private func distributionRow(_ column: TaskBoardColumn) -> some View {
        let value = count(in: column)
        let maximum = max(1, columns.map { count(in: $0) }.max() ?? 1)
        return VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Circle().fill(color(for: column)).frame(width: 7, height: 7)
                Text(column.title).font(.system(size: 12))
                Spacer(minLength: 0)
                Text("\(value)").font(.system(size: 12, weight: .semibold)).monospacedDigit()
            }
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2).fill(AdminTheme.canvas)
                    RoundedRectangle(cornerRadius: 2).fill(color(for: column))
                        .frame(width: geometry.size.width * CGFloat(value) / CGFloat(maximum))
                }
            }
            .frame(height: 6)
        }
        .foregroundStyle(AdminTheme.text)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(column.title), \(value) tasks")
    }

    private func applicationMetric(_ title: String, value: Int, color: Color, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("\(value)").font(.system(size: 24, weight: .semibold)).monospacedDigit().foregroundStyle(color)
                Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(AdminTheme.text)
            }
            Text(detail).font(.system(size: 10)).foregroundStyle(AdminTheme.muted)
        }
    }

    private var applications: some View {
        VStack(alignment: .leading, spacing: 0) {
            panelHeading("Recent applications", detail: "Submission status is separate from task status", action: "View Dashboard") {
                model.search = ""; model.taskFilter = "All"; model.page = .tasks
            }
            Divider().overlay(AdminTheme.border)
            if recentProcesses.isEmpty {
                emptyRow("No applications in this update.", detail: "Browse jobs to choose the roles you want to apply for.")
            } else {
                applicationHeader
                ForEach(recentProcesses) { process in
                    Button { model.showTask(process.taskID) } label: {
                        HStack(spacing: 14) {
                            Text(process.title).font(.system(size: 12, weight: .medium)).lineLimit(2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(process.companyName).font(.system(size: 12)).lineLimit(2)
                                .frame(width: 110, alignment: .leading)
                            StatusLabel(status: process.submissionStatus).frame(width: 144, alignment: .leading)
                            Text(displayDate(process.updatedAt)).font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
                                .frame(width: 102, alignment: .trailing)
                        }
                        .foregroundStyle(AdminTheme.text)
                        .padding(.horizontal, 20).padding(.vertical, 15)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Divider().overlay(AdminTheme.border)
                }
            }
        }
        .adminPanel()
    }

    private var applicationHeader: some View {
        HStack(spacing: 14) {
            Text("ROLE").frame(maxWidth: .infinity, alignment: .leading)
            Text("COMPANY").frame(width: 110, alignment: .leading)
            Text("SUBMISSION").frame(width: 144, alignment: .leading)
            Text("UPDATED").frame(width: 102, alignment: .trailing)
        }
        .font(.system(size: 9, weight: .semibold)).tracking(0.5).foregroundStyle(AdminTheme.muted)
        .padding(.horizontal, 20).padding(.vertical, 11)
        .background(AdminTheme.canvas.opacity(0.65))
    }

    private var activity: some View {
        VStack(alignment: .leading, spacing: 0) {
            panelHeading("Recent activity", detail: "Latest recorded application events and discovery updates", action: "View all") {
                model.page = .activity
            }
            Divider().overlay(AdminTheme.border)
            if model.activity.isEmpty {
                emptyRow("No activity recorded.", detail: "Updates will appear when the workspace records work.")
            } else {
                ForEach(Array(model.activity.prefix(4))) { item in
                    Button { model.showTask(item.taskID) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "clock").font(.system(size: 13)).foregroundStyle(AdminTheme.link)
                                .frame(width: 30, height: 30).background(AdminTheme.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 4))
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.title).font(.system(size: 12, weight: .medium)).foregroundStyle(AdminTheme.text)
                                Text(item.detail).font(.system(size: 11)).foregroundStyle(AdminTheme.muted).lineLimit(1)
                            }
                            Spacer(minLength: 12)
                            Text(displayDate(item.date)).font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
                        }
                        .padding(.horizontal, 20).padding(.vertical, 13).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Divider().overlay(AdminTheme.border)
                }
            }
        }
        .adminPanel()
    }

    private func panelHeading(_ title: String, detail: String, action: String, perform: @escaping () -> Void) -> some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 15, weight: .semibold)).foregroundStyle(AdminTheme.text)
                Text(detail).font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
            }
            Spacer(minLength: 8)
            Button(action, action: perform).buttonStyle(.borderless).font(.system(size: 12)).foregroundStyle(AdminTheme.link)
        }
        .padding(20)
    }

    private func emptyRow(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(AdminTheme.text)
            Text(detail).font(.system(size: 11)).foregroundStyle(AdminTheme.muted)
        }
        .padding(20)
    }

    private func count(in column: TaskBoardColumn) -> Int {
        model.tasks.filter { TaskBoardColumn.column(for: $0) == column }.count
    }

    private func show(_ column: TaskBoardColumn) {
        model.search = ""; model.taskFilter = column.filterName; model.page = .tasks
    }

    private func color(for column: TaskBoardColumn) -> Color {
        switch column {
        case .queued: return AdminTheme.warning
        case .running: return AdminTheme.info
        case .attention: return AdminTheme.danger
        case .finished: return AdminTheme.success
        case .other: return AdminTheme.muted
        }
    }

    private func displayDate(_ value: String) -> String {
        timestamp(value)?.formatted(date: .abbreviated, time: .shortened) ?? "Time unavailable"
    }
}
