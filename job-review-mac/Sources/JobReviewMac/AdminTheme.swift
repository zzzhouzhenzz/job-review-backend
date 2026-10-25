import AppKit
import SwiftUI

/// Native colors and panel geometry based on the requested CoreUI dashboard reference.
enum AdminTheme {
    static let primary = Color(hex: 0x5856D6)
    static let info = Color(hex: 0x3399FF)
    static let warning = Color(hex: 0xF9B115)
    static let danger = Color(hex: 0xE55353)
    static let success = Color(hex: 0x1B9E3E)
    static let sidebar = Color(hex: 0x212631)
    static let sidebarSelected = Color(hex: 0x343B4B)
    static let sidebarMuted = Color(hex: 0xA8ADBA)
    static let canvas = adaptive(light: 0xF3F4F7, dark: 0x181C25)
    static let surface = adaptive(light: 0xFFFFFF, dark: 0x252B36)
    static let border = adaptive(light: 0xDBDFE6, dark: 0x3A4251)
    static let text = adaptive(light: 0x252B36, dark: 0xF1F3F7)
    static let muted = adaptive(light: 0x687182, dark: 0xADB5C4)
    static let link = adaptive(light: 0x4846B6, dark: 0xB8B6FF)
    static let infoText = adaptive(light: 0x1265B8, dark: 0x79BCFF)
    static let warningText = adaptive(light: 0x865900, dark: 0xF9C55C)
    static let dangerText = adaptive(light: 0xB52A2A, dark: 0xFF9797)
    static let successText = adaptive(light: 0x14732E, dark: 0x79D68F)
    static let radius: CGFloat = 6

    private static func adaptive(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let value = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((value >> 16) & 255) / 255,
                           green: CGFloat((value >> 8) & 255) / 255,
                           blue: CGFloat(value & 255) / 255, alpha: 1)
        })
    }
}

private extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 255) / 255,
                  green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255, opacity: 1)
    }
}

extension View {
    func adminPanel() -> some View {
        background(AdminTheme.surface, in: RoundedRectangle(cornerRadius: AdminTheme.radius))
            .overlay(RoundedRectangle(cornerRadius: AdminTheme.radius).stroke(AdminTheme.border, lineWidth: 1))
    }
}

struct AdminMetricCard: View {
    let title: String
    let value: String
    let detail: String
    let icon: String
    let color: Color
    let action: () -> Void
    @State private var hovered = false

    private var ink: Color {
        color == AdminTheme.primary ? .white : Color(red: 0.08, green: 0.10, blue: 0.14)
    }
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(value).font(.system(size: 26, weight: .semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
                    Spacer(minLength: 0)
                    Image(systemName: icon).font(.system(size: 15)).opacity(0.75).accessibilityHidden(true)
                }
                Text(title).font(.system(size: 12, weight: .medium)).lineLimit(1).minimumScaleFactor(0.75)
                if !detail.isEmpty { Text(detail).font(.system(size: 10)).lineLimit(1).minimumScaleFactor(0.8) }
            }.foregroundStyle(ink).padding(14).frame(maxWidth: .infinity, alignment: .leading)
                .background(color.opacity(hovered ? 0.92 : 1), in: RoundedRectangle(cornerRadius: AdminTheme.radius))
                .contentShape(RoundedRectangle(cornerRadius: AdminTheme.radius))
        }.buttonStyle(.plain).onHover { hovered = $0 }
            .accessibilityElement(children: .ignore).accessibilityLabel("\(title): \(value). \(detail)")
            .accessibilityHint("Open related jobs or tasks")
    }
}
