import SwiftUI

/// A tiny palette for the watch. The phone's `Theme/Brand.swift` is built on
/// `UIColor`, which watchOS doesn't have, so the watch keeps its own minimal set
/// of plain `Color`s — forest-green accent + escalation-level colors matching the
/// phone's semantics. watchOS renders on black, so surfaces stay system-default.
enum WatchBrand {
    static let accent = Color(red: 0.44, green: 0.75, blue: 0.53)   // forest green
    static let muted  = Color(white: 0.62)

    static let lvlSoft = Color(red: 0.85, green: 0.66, blue: 0.23)
    static let lvlFirm = Color(red: 0.88, green: 0.48, blue: 0.22)
    static let lvlCall = Color(red: 0.88, green: 0.33, blue: 0.42)

    /// Map a departure/nudge level string to its color (mirrors `Brand.level`).
    static func level(_ level: String?) -> Color {
        switch level {
        case "go", "urgent", "call": return lvlCall
        case "soon", "firm": return lvlFirm
        case "heads_up", "soft": return lvlSoft
        default: return .primary
        }
    }
}
