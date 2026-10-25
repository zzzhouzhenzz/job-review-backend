import SwiftUI
import ReviewCore

struct SavedJobsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var showAll = false
    @State private var search = ""
    @State private var selection = JobSelection()
    @State private var selectionChange: String?
    @State private var confirmation: QueueSelection?

    private var selected: Set<String> { selection.keys }
    private var query: String { search.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var availableJobs: [Job] { showAll ? model.state?.jobs ?? [] : model.ready }
    private var jobs: [Job] {
        availableJobs.filter { job in
            query.isEmpty || job.title.localizedCaseInsensitiveContains(query) || companyName(job.companyID).localizedCaseInsensitiveContains(query)
        }
    }
    private var companyIDs: [String] {
        Array(Set(jobs.map(\.companyID))).sorted {
            companyName($0).localizedStandardCompare(companyName($1)) == .orderedAscending
        }
    }
    private var hiddenSelectionCount: Int { selected.subtracting(Set(jobs.map(\.key))).count }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Saved view", selection: $showAll) { Text("Ready").tag(false); Text("All jobs").tag(true) }
                .pickerStyle(.segmented).padding(.horizontal, 20).padding(.vertical, 12)
            List {
                if let error = model.legacyReadError {
                    Text("Saved jobs could not refresh: \(error)")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust).listRowBackground(Color.clear)
                }
                if model.usesWorkflow {
                    Text("Historical saved choices. Queueing here saves a manual handoff; it does not authorize v1 Apply.")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted).listRowBackground(Color.clear)
                }
                if jobs.isEmpty { emptyState.listRowBackground(Color.clear) }
                ForEach(companyIDs, id: \.self) { companyID in
                    Section(companyName(companyID)) {
                        ForEach(jobs.filter { $0.companyID == companyID }) { job in
                            savedRow(job).listRowBackground(Palette.surface)
                        }
                    }
                }
                if !showAll && dynamicTypeSize.isAccessibilitySize {
                    // Large controls scroll after the jobs instead of hiding the list.
                    Section {
                        selectionBar
                            .buttonStyle(.borderless)
                            .listRowInsets(EdgeInsets())
                            .listRowBackground(Color.clear)
                    }
                }
            }.scrollContentBackground(.hidden)
                .searchable(text: $search, prompt: "Company or job title")
                .refreshable { await model.refresh() }
        }
        .safeAreaInset(edge: .bottom) {
            if !showAll && !dynamicTypeSize.isAccessibilitySize { selectionBar }
        }
        .sheet(item: $confirmation) { captured in
            QueueConfirmation(keys: captured.keys, selectedJobs: captured.jobs)
        }
        .onChange(of: model.ready.map(JobVersion.init)) { _, _ in
            let removed = selection.reconcile(with: model.ready)
            if removed > 0 {
                selectionChange = "Selection updated: \(removed) \(removed == 1 ? "job changed or is" : "jobs changed or are") no longer ready."
            }
        }
        .onChange(of: showAll) { _, _ in clearSelectionState() }
    }

    @ViewBuilder private var emptyState: some View {
        if !query.isEmpty {
            ContentUnavailableView {
                Label("No matching jobs", systemImage: "magnifyingglass")
            } description: {
                Text("Try another company or job title. Your selections stay saved while you search.")
            } actions: {
                Button("Clear search") { search = "" }.frame(minHeight: 44)
            }
        } else if showAll {
            ContentUnavailableView {
                Label("No jobs yet", systemImage: "bookmark")
            } description: {
                Text("Jobs will appear here when a feed is available.")
            }
        } else {
            ContentUnavailableView {
                Label("No jobs ready to queue", systemImage: "bookmark")
            } description: {
                Text("Approve jobs in Review. Jobs already in the queue are shown in Queue.")
            }
        }
    }

    private func savedRow(_ job: Job) -> some View {
        HStack(alignment: .top, spacing: 10) {
            if !showAll {
                Button {
                    selection.toggle(job)
                    selectionChange = nil
                } label: {
                    Image(systemName: selected.contains(job.key) ? "checkmark.circle.fill" : "circle")
                        .font(AppTypography.font(.title2)).frame(width: 44, height: 44)
                }.buttonStyle(.borderless).disabled(!model.canSaveLegacy)
                    .accessibilityLabel(selected.contains(job.key) ? "Remove \(job.title) from queue selection" : "Select \(job.title) for the queue")
                    .accessibilityValue(selected.contains(job.key) ? "Selected" : "Not selected")
            }
            NavigationLink {
                JobDetailView(job: job, companyName: companyName(job.companyID))
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(job.title).font(AppTypography.font(.headline)).fixedSize(horizontal: false, vertical: true)
                    Text(job.location).font(AppTypography.font(.subheadline)).foregroundStyle(Palette.muted)
                    if showAll { Text(status(job)).font(AppTypography.font(.caption, weight: .medium)).foregroundStyle(Palette.muted) }
                }.padding(.vertical, 8).frame(minHeight: 44, alignment: .leading)
            }
        }
    }

    private var selectionBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let selectionChange {
                Text(selectionChange).font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                    .lineLimit(nil)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !selected.isEmpty {
                ViewThatFits(in: .horizontal) {
                    HStack {
                        Text(selectionCount).font(AppTypography.font(.subheadline, weight: .semibold))
                            .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 16)
                        clearSelection
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        Text(selectionCount).font(AppTypography.font(.subheadline, weight: .semibold))
                            .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                        clearSelection
                    }
                }
                if hiddenSelectionCount > 0 {
                    Text("\(hiddenSelectionCount) selected \(hiddenSelectionCount == 1 ? "job is" : "jobs are") outside this search.")
                        .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                        .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                }
            }
            Button {
                confirmation = QueueSelection(keys: selected, jobs: model.ready.filter { selected.contains($0.key) })
            } label: {
                Text(selected.isEmpty ? "Select jobs to queue" : "Review \(selected.count) selected \(selected.count == 1 ? "job" : "jobs")")
                    .foregroundStyle(selected.isEmpty || !model.canSaveLegacy ? Palette.muted : Palette.surface)
                    .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity).frame(minHeight: 48)
            }.buttonStyle(.borderedProminent).disabled(selected.isEmpty || !model.canSaveLegacy)
            Text("Queue work here. Start it from the chat.")
                .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity)
        }.padding(16).background(Palette.paper)
    }

    private var selectionCount: String { "\(selected.count) \(selected.count == 1 ? "job" : "jobs") selected" }
    private var clearSelection: some View {
        Button { clearSelectionState() } label: {
            Text("Clear selection")
                .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
        }.frame(minHeight: 44)
    }
    private func clearSelectionState() { selection.clear(); selectionChange = nil }
    private func companyName(_ id: String) -> String { model.state?.company(id)?.name ?? id }
    private func status(_ job: Job) -> String {
        if let entry = model.state?.queue.first(where: { $0.jobKey == job.key && $0.blocksNewApplication }) { return entry.displayStatus }
        if model.state?.company(job.companyID)?.excluded == true { return "Company excluded" }
        if job.changed { return "Posting changed · Review needed" }
        return ["pending": "Not approved", "approve": "Ready", "later": "Saved for later", "pass": "Passed"][job.decision] ?? job.decision
    }
}

/// The sheet's identity and exact reviewed records must be presented together.
private struct QueueSelection: Identifiable {
    let id = UUID()
    let keys: Set<String>
    let jobs: [Job]
}

private struct QueueConfirmation: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var attemptedSave = false
    let keys: Set<String>
    let selectedJobs: [Job]
    private var allReady: Bool {
        !keys.isEmpty && selectedJobs.count == keys.count && selectedJobs.allSatisfy { captured in
            model.ready.contains { $0.key == captured.key && $0.revision == captured.revision }
        }
    }
    private var allQueued: Bool {
        !keys.isEmpty && keys.allSatisfy { key in
            model.state?.queue.contains { $0.jobKey == key && $0.blocksNewApplication } == true
        }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("This saves work for the agent. It does not send applications.")
                    Text("Return to the chat and ask the agent to start.").foregroundStyle(Palette.muted)
                }.listRowBackground(Palette.surface)
                if let pending = model.pending {
                    Section("Save needs a check") {
                        Text(pending.summary)
                        Text("It may already be saved. Check the same request before you queue more work.")
                            .foregroundStyle(Palette.muted)
                        Button {
                            attemptedSave = true
                            Task { await model.retryPending() }
                        } label: {
                            Text("Check pending save").frame(minHeight: 44)
                        }.disabled(model.busy)
                        Text("You can close this sheet. The pending save will be kept.")
                            .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                    }.listRowBackground(Palette.surface)
                }
                if let error = model.error {
                    Section("Save not confirmed") {
                        Text(error).foregroundStyle(Palette.rust).textSelection(.enabled)
                    }.listRowBackground(Palette.surface)
                } else if attemptedSave, let notice = model.notice {
                    Section {
                        Text(notice).foregroundStyle(Palette.muted)
                    }.listRowBackground(Palette.surface)
                }
                Section("\(keys.count) selected \(keys.count == 1 ? "job" : "jobs")") {
                    ForEach(selectedJobs) { job in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(model.state?.company(job.companyID)?.name ?? "Company").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                            Text(job.title).font(AppTypography.font(.headline))
                            Text("Job ID: \(job.jobID)").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                        }.padding(.vertical, 4)
                    }
                    if selectedJobs.count != keys.count {
                        Text("Some selected jobs are no longer in the feed. Close this sheet and check your selection.")
                            .foregroundStyle(Palette.rust)
                    } else if !allReady && model.pending == nil {
                        Text(allQueued ? "Your selected jobs are in Queue." : "Some selected jobs changed or are no longer ready. Close this sheet and check your selection.")
                            .foregroundStyle(Palette.muted)
                    }
                }.listRowBackground(Palette.surface)
                Section {
                    Button {
                        attemptedSave = true
                        Task {
                            guard allReady else { return }
                            if await model.queue(keys, expectedVersions: selectedJobs.map(JobVersion.init)) { dismiss() }
                        }
                    } label: {
                        HStack {
                            Text("Queue \(keys.count) \(keys.count == 1 ? "job" : "jobs")")
                            if model.busy { ProgressView().controlSize(.small) }
                        }.frame(minHeight: 44)
                    }.disabled(!model.canSaveLegacy || !allReady || selectedJobs.count != keys.count)
                }.listRowBackground(Palette.surface)
            }.scrollContentBackground(.hidden).background(Palette.paper)
                .navigationTitle("Confirm queue").inlineTitle()
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }.frame(minHeight: 44)
                    }
                }
        }
    }
}

struct WorkQueueView: View {
    @EnvironmentObject private var model: AppModel
    @State private var includeHistory = false
    private var entries: [WorkItem] {
        let work = model.state?.queue ?? []
        return includeHistory ? work : work.filter { ["queued", "in_progress", "needs_input", "failed", "stale"].contains($0.status) }
    }
    var body: some View {
        List {
            if let error = model.legacyReadError {
                Text("Legacy queue could not refresh: \(error)")
                    .font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust).listRowBackground(Color.clear)
            }
            Section {
                if model.usesWorkflow {
                    Text("Legacy queue history includes manual work and application projections. Use Progress for current v1 status.")
                        .font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                }
                Toggle("Include completed work", isOn: $includeHistory).frame(minHeight: 44)
                Text("Only a confirmed employer receipt can mark an application as Applied.")
                    .font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
            }.listRowBackground(Palette.surface)
            if entries.isEmpty {
                ContentUnavailableView {
                    Label(includeHistory ? "No work queued yet" : "No active work", systemImage: "tray")
                } description: {
                    Text(includeHistory ? "Select approved jobs in Saved to add work here." : "Queue approved jobs from Saved, or turn on completed work to see your history.")
                }.listRowBackground(Color.clear)
            }
            ForEach(entries) { entry in
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(model.state?.company(entry.companyID)?.name ?? entry.companyID)
                            .font(AppTypography.font(.caption, weight: .semibold)).foregroundStyle(Palette.muted)
                        Text(entry.kind == "research" ? "Find more jobs" : entry.snapshot["title"]?.text ?? "Application")
                            .font(AppTypography.font(.headline)).fixedSize(horizontal: false, vertical: true)
                        Label(entry.displayStatus, systemImage: statusSymbol(entry.status))
                            .font(AppTypography.font(.subheadline, weight: .medium))
                            .foregroundStyle(entry.status == "needs_input" || entry.status == "failed" ? Palette.rust : Palette.muted)
                        if !entry.note.isEmpty {
                            Text(latestMessage(entry.note)).font(AppTypography.font(.subheadline)).textSelection(.enabled)
                        }
                        Text("Saved \(createdDate(entry.createdAt))").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
                        if let url = Job.safeURL(entry.snapshot["url"]?.text) {
                            Link("Open posting", destination: url).font(AppTypography.font(.subheadline)).frame(minHeight: 44)
                        }
                    }.padding(.vertical, 6)
                    if !entry.note.isEmpty {
                        DisclosureGroup {
                            Text(entry.note).font(AppTypography.font(.footnote)).foregroundStyle(Palette.muted)
                                .textSelection(.enabled).padding(.vertical, 8)
                        } label: {
                            Text("Full history").font(AppTypography.font(.subheadline)).frame(minHeight: 44)
                        }
                    }
                }.listRowBackground(Palette.surface)
            }
        }.scrollContentBackground(.hidden).refreshable { await model.refresh() }
    }

    private func statusSymbol(_ status: String) -> String {
        switch status {
        case "applied", "done": return "checkmark.circle"
        case "needs_input": return "person.crop.circle.badge.questionmark"
        case "failed": return "exclamationmark.circle"
        case "in_progress": return "arrow.triangle.2.circlepath"
        case "cancelled", "stale": return "minus.circle"
        default: return "clock"
        }
    }
    private func createdDate(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: value)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: value)
        }
        return date?.formatted(date: .abbreviated, time: .shortened) ?? value
    }
    private func latestMessage(_ note: String) -> String {
        // The store appends timestamped status blocks. Keep every line in the latest block.
        let pattern = #"^\d{4}-\d{2}-\d{2}T\S+ \[[^\]\r\n]+\] "#
        guard let expression = try? NSRegularExpression(pattern: pattern, options: .anchorsMatchLines),
              let match = expression.matches(in: note, range: NSRange(note.startIndex..., in: note)).last,
              let range = Range(match.range, in: note) else { return note }
        let message = String(note[range.upperBound...])
        return message.isEmpty ? note : message
    }
}
