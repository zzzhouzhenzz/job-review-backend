import SwiftUI
import ReviewCore

struct JobBrowserView: View {
    @ObservedObject var model: DashboardModel
    private var cards: [WorkflowCard] {
        (model.reviewFeed?.cards ?? []).filter { card in
            model.browseSearch.isEmpty || (card.company.name + " " + card.jobs.map { $0.title + " " + $0.location }.joined(separator: " "))
                .localizedCaseInsensitiveContains(model.browseSearch)
        }
    }
    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Companies").font(.system(size: 17, weight: .semibold)).foregroundStyle(AdminTheme.text)
                    Text("\(model.reviewFeed?.counts.readyCards ?? 0) ready to review").font(.caption).foregroundStyle(AdminTheme.muted)
                }.padding(.top, 20)
                Rectangle().fill(AdminTheme.border).frame(height: 1)
                List(selection: $model.selectedCardID) {
                    ForEach(cards) { card in
                        companyRow(card).tag(card.id)
                    }
                }.listStyle(.plain).scrollContentBackground(.hidden).tint(AdminTheme.primary)
                if cards.isEmpty && !model.browseSearch.isEmpty {
                    Text("No matching companies.").font(.caption).foregroundStyle(AdminTheme.muted)
                }
                Text("Your Mac and iPhone use the same workspace.").font(.caption2).foregroundStyle(AdminTheme.muted).padding(.bottom, 18)
            }.padding(.horizontal, 16).frame(minWidth: 210, idealWidth: 245, maxWidth: 300)
                .background(AdminTheme.surface)
            Group {
                if let card = cards.first(where: { $0.id == model.selectedCardID }) {
                    CompanyReviewView(model: model, card: card).id(card.id + ":" + card.revision)
                } else {
                    VStack(spacing: 14) {
                        Image(systemName: "rectangle.stack").font(.system(size: 32, weight: .light)).foregroundStyle(AdminTheme.link)
                        Text(emptyTitle).font(.title2.weight(.semibold)).foregroundStyle(AdminTheme.text)
                        Text(emptyMessage)
                            .foregroundStyle(AdminTheme.muted).multilineTextAlignment(.center).frame(maxWidth: 360)
                        if model.reviewFeed == nil {
                            Button("Refresh jobs") { Task { await model.refresh() } }.disabled(model.isRefreshing || model.isSaving || model.demo)
                        } else if !model.browseSearch.isEmpty && cards.isEmpty {
                            Button("Clear search") { model.browseSearch = "" }
                        } else { Button("View Dashboard") { model.page = .tasks } }
                    }.padding(30).frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }.frame(minWidth: 440, maxWidth: .infinity, maxHeight: .infinity)
        }.background(AdminTheme.canvas).tint(AdminTheme.primary).accessibilityIdentifier("job-browser")
            .onAppear { reconcileSelection() }
            .onChange(of: model.browseSearch) { _, _ in reconcileSelection() }
            .onChange(of: cards.map(\.id)) { _, _ in reconcileSelection() }
            .onChange(of: model.selectedCardID) { _, _ in reconcileSelection() }
    }

    private var emptyTitle: String {
        if model.reviewFeed == nil { return "Jobs are unavailable" }
        if !model.browseSearch.isEmpty && cards.isEmpty { return "No matching companies" }
        return cards.isEmpty ? "You're up to date" : "Select a company"
    }

    private var emptyMessage: String {
        if model.reviewFeed == nil { return "Refresh to load current jobs. A confirmed decision stays saved even if the next update fails." }
        if !model.browseSearch.isEmpty && cards.isEmpty { return "Try a different company, role, or location." }
        return cards.isEmpty ? "New jobs will appear here when they are ready. Track discovery on the Dashboard." : "Review its jobs and choose your next step."
    }

    private func reconcileSelection() {
        // Keep the review view identity, and its draft, while the selected card is visible.
        guard !cards.contains(where: { $0.id == model.selectedCardID }) else { return }
        let nextID = cards.first?.id
        if model.selectedCardID != nextID { model.selectedCardID = nextID }
    }

    private func companyRow(_ card: WorkflowCard) -> some View {
        let selected = model.selectedCardID == card.id
        return VStack(alignment: .leading, spacing: 6) {
            Text(card.company.name).font(.system(size: 13, weight: .semibold)).foregroundStyle(selected ? Color.white : AdminTheme.text)
            Text(card.jobs.map(\.title).joined(separator: " · "))
                .font(.system(size: 12)).foregroundStyle(selected ? Color.white.opacity(0.85) : AdminTheme.muted).lineLimit(2)
            Text("\(card.jobs.count) job\(card.jobs.count == 1 ? "" : "s")")
                .font(.system(size: 10, weight: .medium)).foregroundStyle(selected ? Color.white.opacity(0.85) : AdminTheme.muted)
        }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? AdminTheme.primary : Color.clear, in: RoundedRectangle(cornerRadius: AdminTheme.radius))
    }
}

private struct CompanyReviewView: View {
    @ObservedObject var model: DashboardModel
    let card: WorkflowCard
    @State private var selection = JobSelection()
    @State private var note = ""
    private var canDecide: Bool { model.canDecide(card) }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(card.company.name).font(.system(size: 23, weight: .semibold)).foregroundStyle(AdminTheme.text)
                            Spacer(minLength: 8)
                            Text("\(card.jobs.count) job\(card.jobs.count == 1 ? "" : "s")")
                                .font(.system(size: 11, weight: .medium)).foregroundStyle(AdminTheme.link)
                                .padding(.horizontal, 8).padding(.vertical, 4)
                                .background(AdminTheme.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 4))
                        }
                        if let business = card.company.businessDescription {
                            Text(business).font(.callout).foregroundStyle(AdminTheme.muted).textSelection(.enabled)
                        }
                        if let summary = card.content?.summary {
                            Text(summary).font(.callout).foregroundStyle(AdminTheme.text).textSelection(.enabled)
                        }
                    }.padding(20).frame(maxWidth: .infinity, alignment: .leading).adminPanel()
                    ForEach(card.jobs) { job in jobView(job) }
                }.padding(20).frame(maxWidth: 900, alignment: .leading).frame(maxWidth: .infinity)
            }
            Rectangle().fill(AdminTheme.border).frame(height: 1)
            VStack(alignment: .leading, spacing: 12) {
                TextField("Optional note for this decision", text: $note, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(1...3)
                    .disabled(!canDecide).accessibilityIdentifier("decision-note")
                HStack(spacing: 12) {
                    Button("Apply to \(selection.keys.count) job\(selection.keys.count == 1 ? "" : "s")") {
                        decide("apply", selected: selection.keys)
                    }.buttonStyle(.borderedProminent).tint(AdminTheme.primary)
                        .disabled(!canDecide || selection.keys.isEmpty || model.reviewFeed?.capabilities.explicitApply != true)
                        .keyboardShortcut(.return, modifiers: .command).accessibilityIdentifier("apply-selected")
                    Button("Find more") { decide("find_more") }.buttonStyle(.bordered).disabled(!canDecide).accessibilityIdentifier("find-more")
                    Spacer(minLength: 0)
                    Button("Exclude company", role: .destructive) { decide("exclude") }
                        .buttonStyle(.borderless).foregroundStyle(AdminTheme.dangerText).disabled(!canDecide).accessibilityIdentifier("exclude-company")
                }
                Text("Apply authorizes applications for the selected jobs. Each decision marks all jobs on this card as reviewed.")
                    .font(.caption).foregroundStyle(AdminTheme.muted).fixedSize(horizontal: false, vertical: true)
                if !canDecide && !model.isSaving {
                    Text(model.pendingSave == nil ? "Refresh current jobs to enable decisions." : "Confirm the pending save before making another decision.")
                        .font(.caption).foregroundStyle(AdminTheme.warningText)
                }
            }.padding(18).background(AdminTheme.surface)
        }.background(AdminTheme.canvas)
    }

    private func decide(_ action: String, selected: Set<String> = []) {
        Task { await model.decide(card: card, action: action, selected: selected, note: note) }
    }
    private func jobView(_ job: Job) -> some View {
        let ranked = card.content?.jobs.first { $0.key == job.key }
        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Toggle(isOn: Binding(get: { selection.keys.contains(job.key) }, set: { _ in selection.toggle(job) })) {
                    Text(job.title).font(.system(size: 16, weight: .semibold)).foregroundStyle(AdminTheme.text)
                }.toggleStyle(.checkbox).tint(AdminTheme.primary).disabled(!canDecide).accessibilityIdentifier("select-job-\(job.key)")
                Spacer(minLength: 0)
                if let url = job.officialURL {
                    Link(destination: url) { Image(systemName: "arrow.up.right").foregroundStyle(AdminTheme.link) }
                        .help("Open official posting").accessibilityLabel("Open \(job.title) posting")
                }
            }
            HStack(alignment: .top, spacing: 24) {
                fact("Location", job.location)
                fact("Work setting", job.workSettingDescription)
                fact("Pay", job.payDescription)
            }
            Rectangle().fill(AdminTheme.border).frame(height: 1)
            fact("The work", ranked?.brief.work ?? job.short("work"))
            if let ranked { fact("Why this job", ranked.reason) }
            HStack(alignment: .top, spacing: 22) {
                fact("Fit", ranked?.brief.fit ?? job.short("fit"))
                fact("Gap", ranked?.brief.gap ?? job.short("gap"))
            }
            if !job.requirements.isEmpty {
                DisclosureGroup("Posting requirements") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(job.requirements.enumerated()), id: \.offset) { _, requirement in Text(requirement).font(.callout) }
                    }.padding(.top, 8).textSelection(.enabled)
                }.font(.callout.weight(.medium))
            }
            if let description = job.roleDescription {
                DisclosureGroup("Full role description") {
                    Text(description).font(.callout).textSelection(.enabled).padding(.top, 8)
                }.font(.callout.weight(.medium))
            }
            if let gaps = job.sourceGaps { fact("Source information still needed", gaps) }
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .adminPanel()
            .overlay(RoundedRectangle(cornerRadius: AdminTheme.radius).stroke(selection.keys.contains(job.key) ? AdminTheme.primary : Color.clear, lineWidth: 2))
    }
    private func fact(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(AdminTheme.muted)
            Text(value).font(.callout).foregroundStyle(AdminTheme.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
