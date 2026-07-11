import SwiftUI

/// Prefrontal brand palette (see docs/brand/README.md).
/// Field navy background, a lime→teal→blue stroke gradient.
enum Brand {
    static let navy = Color(hex: 0x061A3D)
    static let navyRaised = Color(hex: 0x0B2650)
    static let lime = Color(hex: 0xA7F07A)
    static let teal = Color(hex: 0x43E6C2)
    static let blue = Color(hex: 0x2AA7FF)
    static let nearWhite = Color(hex: 0xF8FAFC)
    static let muted = Color(hex: 0x9DB0D0)
    static let line = Color.white.opacity(0.10)

    /// The signature top→bottom lime→teal→blue stroke gradient.
    static let gradient = LinearGradient(
        colors: [lime, teal, blue],
        startPoint: .top,
        endPoint: .bottom
    )

    /// Primary accent used for tint / interactive elements.
    static let accent = teal

    static let danger = Color(hex: 0xFF6B6B)
    static let warn = Color(hex: 0xF0B849)
    static let ok = lime
}

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: alpha
        )
    }
}

/// A card surface consistent with the web dashboard's card look.
struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Brand.navyRaised, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(Brand.line, lineWidth: 1)
            )
    }
}
