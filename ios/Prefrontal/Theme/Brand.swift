import SwiftUI
import UIKit

/// Prefrontal product palette — mirrors the web dashboard's theme variables
/// (prefrontal/webhooks/dashboard.html): a warm "paper" light theme and a
/// muted dark theme, forest-green accent, and a set of semantic level/domain
/// colors. Adapts automatically to the system appearance.
enum Brand {
    // Surfaces
    static let bg      = Color(l: 0xF7F5F1, d: 0x14161A)
    static let card    = Color(l: 0xFFFFFF, d: 0x1C2028)
    static let raise   = Color(l: 0xF2F0EA, d: 0x1D212B)
    static let chip    = Color(l: 0xECE9E2, d: 0x2A2F3A)
    static let line    = Color(l: 0xE7E2D8, d: 0x2B313C)

    // Text
    static let fg      = Color(l: 0x2C2A26, d: 0xE7E9EE)
    static let muted   = Color(l: 0x7C7568, d: 0x98A0AF)

    // Accent / status
    static let accent  = Color(l: 0x4A7C59, d: 0x6FBF86)
    static let accentFg = Color(l: 0xFFFFFF, d: 0x0F1512)
    static let good    = Color(l: 0x4A7C59, d: 0x6FBF86)
    static let warn    = Color(l: 0xB9762F, d: 0xD59A4F)
    static let danger  = Color(l: 0xB03A3A, d: 0xE0707A)
    static let fyi     = Color(l: 0x3F6F9C, d: 0x8AB4D8)

    // Escalation levels (departure / nudge) — fg + soft bg tint pairs.
    static let lvlNone = Color(l: 0x6B7280, d: 0x8B93A7)
    static let lvlSoft = Color(l: 0xB9762F, d: 0xD9A93B)
    static let lvlFirm = Color(l: 0xC2571F, d: 0xE07B39)
    static let lvlCall = Color(l: 0xC0344B, d: 0xE0556B)

    static let goodBg  = Color(l: 0xE2EFE4, d: 0x16341F)
    static let fyiBg   = Color(l: 0xE2ECF5, d: 0x1E2A3A)

    /// Map a nudge/departure level string to its color.
    static func level(_ level: String?) -> Color {
        switch level {
        case "go", "urgent", "call": return lvlCall
        case "soon", "firm": return lvlFirm
        case "heads_up", "soft": return lvlSoft
        default: return lvlNone
        }
    }

    /// Domain/tag color, hashed from the label so each domain reads distinctly
    /// (the web derives the same "color from a hash of the label").
    static func domain(_ s: String) -> Color {
        var h: UInt32 = 2166136261
        for b in s.lowercased().utf8 { h = (h ^ UInt32(b)) &* 16777619 }
        let hue = Double(h % 360) / 360.0
        return Color(uiColor: UIColor { tc in
            tc.userInterfaceStyle == .dark
                ? UIColor(hue: hue, saturation: 0.42, brightness: 0.80, alpha: 1)
                : UIColor(hue: hue, saturation: 0.55, brightness: 0.48, alpha: 1)
        })
    }

    static func priorityColor(_ p: Int) -> Color { p >= 3 ? lvlCall : (p == 2 ? lvlFirm : lvlNone) }

    // Back-compat aliases (older views referenced brand-mark names).
    static var navy: Color { bg }
    static var navyRaised: Color { raise }
    static var nearWhite: Color { fg }
    static var teal: Color { accent }
    static var blue: Color { fyi }
    static var ok: Color { good }
}

// MARK: - Color helpers

extension Color {
    /// Appearance-adaptive color from light/dark hex.
    init(l: UInt32, d: UInt32) {
        self.init(uiColor: UIColor { tc in
            tc.userInterfaceStyle == .dark ? UIColor(hex: d) : UIColor(hex: l)
        })
    }
    init(hex: UInt32) { self = Color(uiColor: UIColor(hex: hex)) }
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}

// MARK: - Card

/// A surface matching the web dashboard's card: paper/card fill, hairline
/// border, gentle radius and shadow.
struct Card<Content: View>: View {
    var padding: CGFloat = 16
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(padding)
            .background(Brand.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Brand.line, lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.05), radius: 2, x: 0, y: 1)
    }
}
