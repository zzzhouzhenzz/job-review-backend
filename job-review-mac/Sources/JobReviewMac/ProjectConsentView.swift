import SwiftUI

struct ProjectConsentPresentation: ViewModifier {
    @ObservedObject var model: ProjectConsentModel
    func body(content: Content) -> some View {
        content.sheet(isPresented: Binding(get: { model.isPresented }, set: { if !$0 { model.dismiss() } })) {
            ProjectConsentView(model: model)
                .interactiveDismissDisabled(model.isSaving)
        }
    }
}

struct ProjectConsentView: View {
    @ObservedObject var model: ProjectConsentModel

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Image(systemName: "hand.raised.circle")
                .font(.system(size: 34, weight: .light)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 9) {
                Text("Allow Job Review to work for you").font(.title2.weight(.semibold))
                Text("These permissions are required to find jobs and prepare your approved applications.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 18) {
                permission("Browse job sources", icon: "globe", detail: "Read LinkedIn, company websites, and their application pages using your signed-in Chrome session.")
                permission("Upload your resume", icon: "doc.badge.arrow.up", detail: "Upload your approved resume when preparing an application for a job you approved.")
            }
            Text("Your job decisions still control which applications Job Review works on. This agreement is saved by the service and applies each time you use this app.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("Chrome or macOS may still need you to sign in or allow a system permission.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("project-consent-save-error")
                if model.hasPendingAgreement {
                    Text("Your Agree request is saved on this Mac. Job Review will check it again when the connection returns.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Divider()
            HStack {
                Button(model.hasPendingAgreement ? "Close" : "Not now") { model.dismiss() }.keyboardShortcut(.cancelAction).disabled(model.isSaving)
                Spacer()
                if model.isSaving { ProgressView().controlSize(.small) }
                Button(model.isSaving ? "Saving…" : "Agree") { Task { await model.agree() } }
                    .keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).disabled(model.isSaving || model.isLoading)
                    .accessibilityIdentifier("project-consent-agree")
            }
        }
        .padding(30).frame(width: 480).fixedSize(horizontal: false, vertical: true)
        .background(Color(nsColor: .windowBackgroundColor))
        .accessibilityIdentifier("project-consent-dialog")
    }

    private func permission(_ title: String, icon: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 13) {
            Image(systemName: icon).font(.system(size: 19)).frame(width: 25).padding(.top, 2).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct ProjectConsentStatusView: View {
    @ObservedObject var model: ProjectConsentModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Project permissions").font(.title3.weight(.semibold))
            Label(model.status, systemImage: model.consent?.granted == true && model.errorMessage == nil ? "checkmark.circle.fill" : "hand.raised")
                .font(.headline)
            if let date = model.consent?.grantedAt, let parsed = timestamp(date) {
                Text("Agreed on \(parsed.formatted(date: .abbreviated, time: .shortened))").font(.caption).foregroundStyle(.secondary)
            }
            Text(model.consent?.granted == true
                 ? "Job Review can browse job sources and upload your approved resume for jobs you approve."
                 : "Job search and application preparation stay paused until you agree.")
                .font(.callout).foregroundStyle(.secondary)
            if let error = model.errorMessage { Text(error).font(.callout).foregroundStyle(.orange) }
            if model.consent?.granted == false {
                Button("Review permissions") { model.review() }.disabled(model.isSaving)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
