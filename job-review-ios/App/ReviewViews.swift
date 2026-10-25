import SwiftUI
import ReviewCore

struct ReviewRootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var settings = false
    var body: some View {
        TabView {
            screen("Review") {
                if model.usesWorkflow { WorkflowFeedView() } else { ReviewFeedView() }
            }
                .tabItem { Label("Review", systemImage: "rectangle.stack") }
            if model.usesWorkflow {
                screen("Application progress") { WorkflowProgressView() }
                    .tabItem { Label("Progress", systemImage: "list.bullet.rectangle") }
                screen("Application questions") { WorkflowQuestionsView() }
                    .tabItem { Label("Questions", systemImage: "questionmark.bubble") }
                    .badge(model.workflow?.questions.filter { $0.answer == nil }.count ?? 0)
            }
            screen(model.usesWorkflow ? "Saved jobs · Manual" : "Saved jobs") { SavedJobsView() }
                .tabItem { Label("Saved", systemImage: "bookmark") }
                .badge(model.ready.count)
            screen(model.usesWorkflow ? "Queue · Legacy history" : "Work queue") { WorkQueueView() }
                .tabItem { Label("Queue", systemImage: "tray") }
                .badge(model.state?.activeWork.count ?? 0)
        }
        // Draft picks, navigation, and confirmations belong to one normalized data source.
        .id("\(model.connectedOrigin ?? ""):\(model.usesWorkflow)")
        .preferredColorScheme(.light)
        .sheet(isPresented: $settings) { SettingsView() }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await model.pollWhileActive()
        }
    }
    private func screen<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        NavigationStack {
            VStack(spacing: 0) {
                ConnectionBanner()
                content()
            }
            .background(Palette.paper)
            .foregroundStyle(Palette.ink)
            .navigationTitle(title).inlineTitle()
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { settings = true } label: {
                        Image(systemName: "slider.horizontal.3").frame(minWidth: 44, minHeight: 44)
                    }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Connection settings")
                }
            }
        }
    }
}

private struct ConnectionBanner: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.demo {
                Label("Demo · Sample jobs only", systemImage: "sparkle")
                    .font(AppTypography.font(.caption, weight: .medium)).foregroundStyle(Palette.muted)
            } else if !model.online {
                HStack {
                    Label("Saved copy · Read only", systemImage: "wifi.slash")
                    Spacer()
                    Button { Task { await model.refresh() } } label: {
                        Text("Refresh").frame(minHeight: 44)
                    }.buttonStyle(.plain).disabled(model.busy)
                }.font(AppTypography.font(.caption, weight: .medium))
                if let date = model.savedAt { Text("Last synced \(date.formatted(date: .abbreviated, time: .shortened))").font(AppTypography.font(.caption2)) }
            }
            if model.usesWorkflow { WorkflowHealthBanner() }
            if let pending = model.pending {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Save needs a check", systemImage: "arrow.triangle.2.circlepath").font(AppTypography.font(.subheadline, weight: .bold))
                    Text(pending.summary).font(AppTypography.font(.caption))
                    Text("It may already be saved. This checks the same request.").font(AppTypography.font(.caption))
                    Button { Task { await model.retryPending() } } label: {
                        Text("Check pending save").frame(minHeight: 44)
                    }.buttonStyle(.plain).disabled(model.busy)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            if let error = model.error {
                HStack(alignment: .top) {
                    Text(error).font(AppTypography.font(.footnote)).foregroundStyle(Palette.rust)
                    Spacer(minLength: 8)
                    Button { model.error = nil } label: {
                        Image(systemName: "xmark").frame(width: 44, height: 44)
                    }.buttonStyle(.plain).accessibilityLabel("Dismiss error")
                }.accessibilityElement(children: .combine)
            } else if let notice = model.notice {
                HStack {
                    Label(notice, systemImage: "checkmark.circle").font(AppTypography.font(.footnote))
                    Spacer(minLength: 8)
                    Button { model.notice = nil } label: {
                        Image(systemName: "xmark").frame(width: 44, height: 44)
                    }.buttonStyle(.plain).accessibilityLabel("Dismiss saved message")
                }
            }
            if model.busy { ProgressView().frame(maxWidth: .infinity).accessibilityLabel("Working") }
        }
        .padding(.horizontal, 20).padding(.vertical, model.demo || model.usesWorkflow || !model.online || model.pending != nil || model.error != nil || model.notice != nil || model.busy ? 10 : 0)
        .background(Palette.paper)
    }
}

struct ReviewFeedView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        Group {
            if let card = model.state?.flashcards.first, let company = model.state?.company(card.companyID) {
                CompanyCardView(card: card, company: company, companiesLeft: model.state?.flashcards.count ?? 0)
                    .id(card.id)
                    .transition(reduceMotion ? .identity : .asymmetric(
                        insertion: .opacity.combined(with: .offset(y: 12)), removal: .opacity))
            } else if model.state != nil {
                ContentUnavailableView {
                    Label("You're caught up", systemImage: "checkmark.circle")
                } description: {
                    Text("Your choices are saved. New companies and jobs will appear here after research.")
                } actions: {
                    Button("Refresh feed") { Task { await model.refresh() } }.disabled(model.busy)
                }
            } else {
                ContentUnavailableView {
                    Label("Your next role starts here", systemImage: "rectangle.stack")
                } description: {
                    Text("Connect to your review server, or use the sample feed in Settings.")
                } actions: {
                    Button("Try again") { Task { await model.refresh() } }.disabled(model.busy)
                }
            }
        }
        // The model changes this identity only after it has a confirmed server state.
        .animation(reduceMotion ? nil : .easeOut(duration: 0.22), value: model.state?.flashcards.first?.id)
    }
}

struct CompanyCardView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let card: FlashCard?
    let workflowCard: WorkflowCard?
    let company: Company
    let companiesLeft: Int
    @State private var selected = Set<String>()
    @State private var detail: Job?
    @State private var deciding: CardAction?
    @State private var visibleJobKey: String?
    @State private var dragStartJobKey: String?
    @GestureState private var isPagingDrag = false
    private var canDecide: Bool {
        let available: Bool
        if let workflowCard {
            available = model.canSave && model.workflow?.readErrors["feed"] == nil &&
                model.workflow?.feed?.cards.contains(workflowCard) == true
        } else {
            available = model.canSaveLegacy
        }
        return available && deciding == nil
    }
    private var canApprove: Bool { workflowCard == nil || model.workflow?.capabilities?.explicitApply == true }
    private var jobs: [Job] { workflowCard?.jobs ?? card?.jobs ?? [] }
    private var remainingJobs: Int { card?.remainingJobs ?? jobs.count }
    private var additionalJobs: Int { max(0, remainingJobs - jobs.count) }
    private var approvalLabel: String { workflowCard == nil ? "Approve" : "Apply" }

    init(card: FlashCard, company: Company, companiesLeft: Int) {
        self.card = card; workflowCard = nil; self.company = company; self.companiesLeft = companiesLeft
    }
    init(workflowCard: WorkflowCard, companiesLeft: Int) {
        card = nil; self.workflowCard = workflowCard; company = workflowCard.company; self.companiesLeft = companiesLeft
    }

    var body: some View {
        GeometryReader { geometry in
            ScrollView {
                cardContent
                    .background(Palette.surface, in: RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Palette.line, lineWidth: 1).allowsHitTesting(false))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .shadow(color: .black.opacity(0.035), radius: 2, y: 1)
                    .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 18)
                    .frame(maxWidth: 620)
                    .frame(maxWidth: .infinity)
            }
            .refreshable { await model.refresh() }
            .safeAreaInset(edge: .bottom, spacing: 0) { actions(availableWidth: geometry.size.width) }
        }
        .sheet(item: $detail) { job in NavigationStack { JobDetailView(job: job, companyName: company.name) } }
        .accessibilityActions {
            if canDecide {
                Button("Exclude company") { act(.exclude) }
                Button("Find more jobs") { act(.research) }
                if canApprove && !selected.isEmpty {
                    Button("\(approvalLabel) for \(selected.count) selected jobs") { act(.approve) }
                }
            }
        }
    }
    private var cardContent: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.horizontal, 20)
            jobPages
            VStack(alignment: .leading, spacing: 8) {
                if additionalJobs > 0 {
                    Label("\(additionalJobs) more job\(additionalJobs == 1 ? "" : "s") at \(company.name) \(additionalJobs == 1 ? "is" : "are") waiting.", systemImage: "rectangle.stack")
                        .font(AppTypography.font(.caption, weight: .medium))
                }
                Text(workflowCard == nil
                     ? "Swipe to browse jobs. Select the jobs you want, then tap the checkmark to approve."
                     : "Swipe to browse jobs. The checkmark authorizes applications for all selected jobs. Each decision saves and opens the next company.")
                    .font(AppTypography.font(.caption))
            }
            .foregroundStyle(Palette.muted).padding(20).padding(.top, 4)
        }
    }
    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    remainingLabel.fixedSize()
                    Spacer(minLength: 0)
                    jobCountLabel.fixedSize()
                }
                VStack(alignment: .leading, spacing: 6) {
                    remainingLabel
                    jobCountLabel
                }
            }.foregroundStyle(Palette.brandText)
            HStack(alignment: .center, spacing: 12) {
                CompanyLogo(company: company)
                Text(company.name).font(AppTypography.font(.title, weight: .semibold)).tracking(-0.5)
                    .foregroundStyle(Palette.brandText)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
            }
            Text(company.businessDescription ?? "A company description is not available yet.")
                .font(AppTypography.font(.subheadline)).foregroundStyle(Palette.detail)
                .fixedSize(horizontal: false, vertical: true)
            if let summary = workflowCard?.content?.summary {
                DisclosureGroup {
                    Text(summary).font(AppTypography.font(.subheadline)).foregroundStyle(Palette.detail)
                        .fixedSize(horizontal: false, vertical: true)
                } label: {
                    SectionTitle("Why these jobs", icon: "lightbulb", color: Palette.brandText)
                        .frame(minHeight: 44)
                }
                .tint(Palette.brandText)
            }
            if !company.note.isEmpty {
                Label(company.note, systemImage: "note.text").font(AppTypography.font(.caption)).foregroundStyle(Palette.muted)
            }
        }.padding(20).background(Palette.brandSurface)
    }
    private var currentJobKey: String? { visibleJobKey ?? jobs.first?.key }
    private var jobPages: some View {
        VStack(spacing: 0) {
            // Horizontal gestures only page through postings; decisions are explicit buttons.
            ScrollView(.horizontal) {
                HStack(alignment: .top, spacing: 0) {
                    ForEach(jobs) { job in
                        jobRow(job)
                            .containerRelativeFrame(.horizontal)
                            .id(job.key)
                    }
                }.scrollTargetLayout()
            }
            .scrollTargetBehavior(.paging)
            .scrollPosition(id: $visibleJobKey)
            .scrollIndicators(.hidden)
            .simultaneousGesture(DragGesture(minimumDistance: 24)
                .updating($isPagingDrag) { _, active, _ in active = true }
                .onChanged { _ in
                    if dragStartJobKey == nil { dragStartJobKey = currentJobKey }
                }
                .onEnded { value in
                    defer { dragStartJobKey = nil }
                    let horizontal = value.translation.width
                    guard abs(horizontal) >= 50,
                          abs(horizontal) > abs(value.translation.height) * 1.5,
                          let index = jobs.firstIndex(where: { $0.key == dragStartJobKey }) else { return }
                    let next = min(max(index + (horizontal < 0 ? 1 : -1), 0), jobs.count - 1)
                    withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) {
                        visibleJobKey = jobs[next].key
                    }
                })
            .onChange(of: isPagingDrag) { _, active in
                if !active { dragStartJobKey = nil }
            }
            HStack(spacing: 7) {
                ForEach(jobs) { job in
                    Circle().fill(currentJobKey == job.key ? Palette.primary : Palette.line)
                        .frame(width: 6, height: 6)
                }
            }
            .frame(maxWidth: .infinity).padding(.top, 8)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Jobs")
            .accessibilityValue("Job \((jobs.firstIndex(where: { $0.key == currentJobKey }) ?? 0) + 1) of \(jobs.count)")
            .accessibilityHint("Changes the visible posting without selecting it.")
            .accessibilityAdjustableAction { direction in
                guard let index = jobs.firstIndex(where: { $0.key == currentJobKey }) else { return }
                let step: Int
                switch direction {
                case .increment: step = 1
                case .decrement: step = -1
                @unknown default: return
                }
                let next = min(max(index + step, 0), jobs.count - 1)
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) {
                    visibleJobKey = jobs[next].key
                }
            }
        }
    }
    private var remainingLabel: some View {
        Label("\(companiesLeft) compan\(companiesLeft == 1 ? "y" : "ies") to review", systemImage: "rectangle.stack")
            .font(AppTypography.font(.caption, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
    }
    private var jobCountLabel: some View {
        Text(additionalJobs > 0 ? "\(jobs.count) of \(remainingJobs) jobs" : "\(jobs.count) job\(jobs.count == 1 ? "" : "s")")
            .font(AppTypography.font(.caption, weight: .medium))
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(Palette.surface, in: Capsule())
    }
    private func jobRow(_ job: Job) -> some View {
        let isSelected = selected.contains(job.key)
        let ranked = workflowCard?.content?.jobs.first { $0.key == job.key && $0.revision == job.revision }
        return VStack(alignment: .leading, spacing: 7) {
            Button {
                guard canDecide else { return }
                if isSelected { selected.remove(job.key) } else { selected.insert(job.key) }
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(job.title).font(AppTypography.font(.headline)).foregroundStyle(isSelected ? Palette.primary : Palette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                        Label(job.location, systemImage: "mappin.and.ellipse")
                            .font(AppTypography.font(.caption)).foregroundStyle(Palette.brandText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 26, weight: .light)).foregroundStyle(isSelected ? Palette.primary : Palette.muted)
                        .frame(width: 44, height: 44)
                }
                .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!canDecide)
            .accessibilityLabel("\(job.title), \(job.location)")
            .accessibilityValue(isSelected ? "Selected" : "Not selected")
            .accessibilityHint(isSelected ? "Remove this job from your selection." : workflowCard == nil ? "Select this job for approval." : "Select this job for Apply.")
            .accessibilityAddTraits(isSelected ? .isSelected : [])
            VStack(alignment: .leading, spacing: 10) {
                jobFact("Pay", job.payDescription, icon: "banknote")
                Rectangle().fill(Palette.brandLine).frame(height: 1).accessibilityHidden(true)
                jobFact("Work setting", job.workSettingDescription, icon: "building.2")
            }
            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.brandSurface, in: RoundedRectangle(cornerRadius: 8))
            .padding(.top, 5).padding(.bottom, 9)
            VStack(alignment: .leading, spacing: 6) {
                SectionTitle("Role", icon: "briefcase", color: Palette.brandText)
                Text(job.roleDescription ?? ranked?.brief.work ?? job.short("work", fallback: job.field("why_ranked", fallback: "Read the full posting for the work scope.")))
                    .font(AppTypography.font(.subheadline)).foregroundStyle(Palette.detail)
                    .fixedSize(horizontal: false, vertical: true)
            }.padding(.bottom, 9)
            VStack(alignment: .leading, spacing: 6) {
                SectionTitle("Requirements", icon: "list.bullet", color: Palette.information)
                if job.requirements.isEmpty {
                    Text("Requirements are not available in this feed. Check the original posting.")
                        .foregroundStyle(Palette.muted)
                } else {
                    ForEach(Array(job.requirements.enumerated()), id: \.offset) { _, requirement in
                        HStack(alignment: .firstTextBaseline, spacing: 7) {
                            Text("•").foregroundStyle(Palette.information).accessibilityHidden(true)
                            Text(requirement).foregroundStyle(Palette.detail).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }.font(AppTypography.font(.subheadline)).fixedSize(horizontal: false, vertical: true)
            Divider().padding(.vertical, 6)
            AssessmentList(title: "Fit", text: ranked?.brief.fit ?? job.short("fit", fallback: job.field("fit", fallback: "Fit has not been assessed.")), color: Palette.fit)
            AssessmentList(title: "Gap", text: ranked?.brief.gap ?? job.short("gap", fallback: job.sourceGaps ?? "Gaps have not been assessed."), color: Palette.gap)
                .padding(.top, 5)
            if let ranked {
                DisclosureGroup {
                    Text(ranked.reason).font(AppTypography.font(.subheadline)).fixedSize(horizontal: false, vertical: true)
                } label: {
                    SectionTitle("Why this job", icon: "lightbulb", color: Palette.brandText)
                        .frame(minHeight: 44)
                }
                .tint(Palette.brandText)
            }
            Button { detail = job } label: {
                HStack(spacing: 6) {
                    Image(systemName: "link").accessibilityHidden(true)
                    Text("Posting & sources")
                    Image(systemName: "arrow.up.right").font(AppTypography.font(.caption2)).accessibilityHidden(true)
                }
                .font(AppTypography.font(.caption, weight: .semibold)).foregroundStyle(Palette.information)
                .padding(.horizontal, 10).frame(minHeight: 44)
                .background(Palette.informationSurface, in: RoundedRectangle(cornerRadius: 8))
            }.buttonStyle(.plain)
        }
        .padding(.horizontal, 20).padding(.vertical, 20)
        .background(isSelected ? Palette.primary.opacity(0.055) : Color.clear)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: isSelected)
    }
    private func jobFact(_ label: String, _ text: String, icon: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: icon).font(.system(size: 14, weight: .medium))
                .foregroundStyle(Palette.brandText).frame(width: 18).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(label).font(AppTypography.font(.caption, weight: .semibold)).foregroundStyle(Palette.brandText)
                Text(text).font(AppTypography.font(.subheadline)).foregroundStyle(Palette.detail)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
    private func actions(availableWidth: CGFloat) -> some View {
        let stacked = dynamicTypeSize >= .xxxLarge || availableWidth < 340
        let layout = stacked ? AnyLayout(VStackLayout(spacing: 8)) : AnyLayout(HStackLayout(spacing: 10))
        return layout {
            actionButton("Exclude", icon: "xmark", action: .exclude, color: Palette.ink, showsLabel: false)
            actionButton("Find more", icon: "magnifyingglass", action: .research, color: Palette.ink)
            actionButton(selected.isEmpty ? "Select jobs" : "\(approvalLabel) \(selected.count)", icon: "checkmark", action: .approve, color: Palette.primary, filled: true, showsLabel: false)
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .frame(maxWidth: 620).frame(maxWidth: .infinity)
        .background(Palette.paper)
        .overlay(alignment: .top) { Divider().overlay { Palette.line } }
    }
    private func actionButton(_ label: String, icon: String, action: CardAction, color: Color, filled: Bool = false, showsLabel: Bool = true) -> some View {
        let disabled = !canDecide || (action == .approve && (!canApprove || selected.isEmpty))
        return Button { act(action) } label: {
            HStack(spacing: 6) {
                if deciding == action {
                    ProgressView().controlSize(.small).tint(filled ? Palette.surface : color)
                        .frame(height: 22)
                } else {
                    HStack(spacing: 6) {
                        Image(systemName: icon).font(.system(size: 18, weight: .medium)).frame(minHeight: 22)
                        if action == .approve && !selected.isEmpty {
                            Text("\(selected.count)")
                                .font(AppTypography.font(.subheadline, weight: .medium)).monospacedDigit()
                        }
                    }
                }
                if showsLabel {
                    Text(deciding == action ? "Saving…" : label)
                        .font(AppTypography.font(.subheadline, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                }
            }
                .padding(.horizontal, 8).padding(.vertical, 10).frame(maxWidth: .infinity).frame(minHeight: 48)
                .foregroundStyle(filled ? Palette.surface : color)
                .background(filled ? color : Palette.surface, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(filled ? color : Palette.line))
                .shadow(color: .black.opacity(0.05), radius: 1, y: 1)
                .opacity(disabled ? 0.5 : 1)
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .accessibilityLabel(deciding == action ? "Saving \(label)" : label)
        .accessibilityHint(action == .approve ? (workflowCard == nil ? "Approves only selected jobs. Does not send an application." : "Authorizes applications for selected jobs and opens the next company after saving.") : action == .exclude ? "Excludes this company from the feed." : "Requests more jobs at this company.")
    }
    private func act(_ action: CardAction) {
        guard canDecide, action != .approve || canApprove else { return }
        if action == .approve && selected.isEmpty {
            model.error = workflowCard == nil ? "Select at least one job to approve." : "Select at least one job to apply."; return
        }
        deciding = action
        let selection = selected
        Task {
            if let workflowCard {
                let decision: String
                switch action {
                case .approve: decision = "apply"
                case .research: decision = "find_more"
                case .exclude: decision = "exclude"
                }
                await model.workflowDecision(card: workflowCard, action: decision,
                                             selected: action == .approve ? selection : [])
            } else if let card {
                await model.decide(card, action: action, selected: selection)
            }
            deciding = nil
        }
    }
}

private struct SectionTitle: View {
    let title: String
    let icon: String
    let color: Color

    init(_ title: String, icon: String, color: Color) {
        self.title = title; self.icon = icon; self.color = color
    }

    var body: some View {
        HStack(spacing: 7) {
            Image(systemName: icon).font(.system(size: 14, weight: .medium)).accessibilityHidden(true)
            Text(title).font(AppTypography.font(.subheadline, weight: .semibold))
        }
        .foregroundStyle(color).accessibilityAddTraits(.isHeader)
    }
}

private struct AssessmentList: View {
    let title: String
    let text: String
    let color: Color

    var body: some View {
        let points = AssessmentText.bulletPoints(from: text)
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(AppTypography.font(.subheadline, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            if points.isEmpty {
                Text("Not assessed yet.").foregroundStyle(Palette.muted)
            } else {
                ForEach(Array(points.enumerated()), id: \.offset) { _, point in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text("•").accessibilityHidden(true)
                        Text(point).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .font(AppTypography.font(.subheadline)).foregroundStyle(color)
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct CompanyLogo: View {
    let company: Company
    var body: some View {
        AsyncImage(url: company.logoURL) { phase in
            if case .success(let image) = phase {
                image.resizable().scaledToFit().padding(7)
            } else {
                Text(company.displayInitials)
                    .font(AppTypography.font(.title3, weight: .semibold))
                    .foregroundStyle(Palette.primary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: 44, height: 44)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: 8))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Palette.brandLine, lineWidth: 1))
        .accessibilityHidden(true)
    }
}

struct JobDetailView: View {
    @Environment(\.dismiss) private var dismiss
    let job: Job
    let companyName: String
    var body: some View {
        List {
            Section {
                Text(job.title).font(AppTypography.font(.title2, weight: .semibold))
                Text(companyName).foregroundStyle(Palette.muted)
                LabeledContent("Job ID", value: job.jobID).font(AppTypography.font(.footnote))
                if let url = job.officialURL { Link("Open job posting", destination: url) }
            }
            Section("Role") {
                fact("Location", job.location)
                fact("Work setting", job.workSettingDescription)
                fact("Pay", job.payDescription)
                fact("Level", job.field("level"))
                fact("Work", job.roleDescription ?? job.short("work"))
                fact("Why this role", job.field("why_ranked", fallback: job.short("fit")))
            }
            Section("Requirements") {
                if job.requirements.isEmpty {
                    Text("Requirements are not available in this feed. Check the original posting.")
                } else {
                    ForEach(Array(job.requirements.enumerated()), id: \.offset) { _, text in
                        Text(text).font(AppTypography.font(.subheadline))
                    }
                }
            }
            Section("Evidence and limits") {
                ForEach(Array((job.data["strengths"]?.array.compactMap(\.text) ?? []).enumerated()), id: \.offset) { _, text in fact("Strength", text) }
                if let gaps = job.sourceGaps { AssessmentList(title: "Gap", text: gaps, color: Palette.gap) }
                fact("Availability", job.field("availability"))
                if job.changed { Label("The posting changed. Review its current facts.", systemImage: "exclamationmark.circle").foregroundStyle(Palette.rust) }
                if !job.note.isEmpty { fact("Your note", job.note) }
            }
            Section("Sources") {
                ForEach(Array((job.data["sources"]?.array ?? []).enumerated()), id: \.offset) { _, source in
                    if let url = Job.safeURL(source.text ?? source.object["url"]?.text) {
                        Link(source.object["label"]?.text ?? "Source", destination: url)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden).background(Palette.paper)
        .navigationTitle("Job details").inlineTitle()
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
    }
    private func fact(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) { Text(label).font(AppTypography.font(.caption, weight: .semibold)).foregroundStyle(Palette.muted); Text(text).font(AppTypography.font(.subheadline)) }
            .padding(.vertical, 4)
    }
}
