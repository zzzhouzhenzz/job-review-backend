import SwiftUI
import CoreText
#if os(iOS)
import UIKit
#endif

@main
struct JobReviewApp: App {
    @StateObject private var model = AppModel()
    init() {
        AppTypography.prepareFonts()
        #if os(iOS)
        let navigation = UINavigationBarAppearance()
        navigation.configureWithOpaqueBackground()
        navigation.backgroundColor = UIColor(Palette.surface)
        navigation.shadowColor = UIColor(Palette.line)
        navigation.titleTextAttributes = [
            .font: UIFont(name: "Inter-SemiBold", size: 17) ?? UIFont.preferredFont(forTextStyle: .headline),
            .foregroundColor: UIColor(Palette.ink)
        ]
        UINavigationBar.appearance().standardAppearance = navigation
        UINavigationBar.appearance().scrollEdgeAppearance = navigation
        let tabs = UITabBarAppearance()
        tabs.configureWithOpaqueBackground()
        tabs.backgroundColor = UIColor(Palette.surface)
        tabs.shadowColor = UIColor(Palette.line)
        for item in [tabs.stackedLayoutAppearance, tabs.inlineLayoutAppearance, tabs.compactInlineLayoutAppearance] {
            item.normal.iconColor = UIColor(Palette.muted)
            item.selected.iconColor = UIColor(Palette.primary)
            let font = UIFont(name: "Inter-Medium", size: 10) ?? UIFont.systemFont(ofSize: 10, weight: .medium)
            item.normal.titleTextAttributes = [.font: font, .foregroundColor: UIColor(Palette.muted)]
            item.selected.titleTextAttributes = [.font: font, .foregroundColor: UIColor(Palette.primary)]
        }
        UITabBar.appearance().standardAppearance = tabs
        UITabBar.appearance().scrollEdgeAppearance = tabs
        #endif
    }
    var body: some Scene {
        WindowGroup {
            ReviewRootView().environmentObject(model)
                .font(AppTypography.font(.body))
                .tint(Palette.primary)
                .task { await model.start() }
        }
    }
}

enum Palette {
    static let paper = Color(red: 0.98, green: 0.98, blue: 0.98)
    static let surface = Color.white
    static let ink = Color(red: 10 / 255, green: 10 / 255, blue: 10 / 255)
    static let muted = Color(red: 88 / 255, green: 88 / 255, blue: 88 / 255)
    static let primary = Color(red: 92 / 255, green: 75 / 255, blue: 235 / 255)
    static let brandText = Color(red: 76 / 255, green: 60 / 255, blue: 203 / 255)
    static let brandSurface = Color(red: 245 / 255, green: 242 / 255, blue: 255 / 255)
    static let brandLine = Color(red: 227 / 255, green: 219 / 255, blue: 255 / 255)
    static let information = Color(red: 29 / 255, green: 78 / 255, blue: 216 / 255)
    static let informationSurface = Color(red: 239 / 255, green: 246 / 255, blue: 255 / 255)
    static let detail = Color(red: 51 / 255, green: 65 / 255, blue: 85 / 255)
    static let fit = Color(red: 21 / 255, green: 128 / 255, blue: 61 / 255)
    static let gap = Color(red: 161 / 255, green: 98 / 255, blue: 7 / 255)
    static let rust = Color(red: 185 / 255, green: 28 / 255, blue: 28 / 255)
    static let line = Color(red: 229 / 255, green: 229 / 255, blue: 229 / 255)
    static let secondary = Color(red: 245 / 255, green: 245 / 255, blue: 245 / 255)
}

enum AppTypography {
    private static let registration: Void = {
        #if SWIFT_PACKAGE
        for face in ["Regular", "Medium", "SemiBold", "Bold"] {
            let name = "Inter-\(face)"
            guard let url = Bundle.module.url(forResource: name, withExtension: "ttf", subdirectory: "Fonts") else {
                NSLog("jobswipe: bundled font %@ was not found", name)
                continue
            }
            var error: Unmanaged<CFError>?
            if !CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error) {
                NSLog("jobswipe: could not register font %@: %@", name, String(describing: error?.takeRetainedValue()))
            }
        }
        #endif
    }()

    static func prepareFonts() { _ = registration }

    static func font(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        prepareFonts()
        let size: CGFloat
        switch style {
        case .largeTitle: size = 32
        case .title: size = 24
        case .title2: size = 20
        case .title3: size = 18
        case .headline: size = 17
        case .body: size = 16
        case .callout: size = 15
        case .subheadline: size = 14
        case .footnote: size = 13
        case .caption: size = 12
        case .caption2: size = 11
        @unknown default: size = 16
        }
        let resolvedWeight = weight ?? (style == .headline ? .semibold : .regular)
        let face: String
        switch resolvedWeight {
        case .bold, .heavy, .black: face = "Bold"
        case .semibold: face = "SemiBold"
        case .medium: face = "Medium"
        default: face = "Regular"
        }
        return .custom("Inter-\(face)", size: size, relativeTo: style)
    }
}

extension View {
    @ViewBuilder func inlineTitle() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.inline)
        #else
        self
        #endif
    }
    @ViewBuilder func URLInput() -> some View {
        #if os(iOS)
        textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
        #else
        self
        #endif
    }
}
