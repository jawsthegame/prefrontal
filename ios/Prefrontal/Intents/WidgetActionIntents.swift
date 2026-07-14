import AppIntents

/// Intents backing the **interactive widget** buttons (iOS 17). Kept in their
/// own file so `project.yml` can compile *just these* into the widget extension
/// target — not `PrefrontalIntents.swift`, whose `AppShortcutsProvider` must
/// live in the app target alone. Authenticates via the App Group like every
/// other widget read (`APIClient(shared:)`).
///
/// A tapped widget `Button(intent:)` runs `perform()` in a short background
/// window and WidgetKit reloads the timeline afterwards, so the count updates
/// without opening the app.
struct MarkSelfCareIntent: AppIntent {
    static let title: LocalizedStringResource = "Log self-care"
    static let description = IntentDescription("Log a meal or a glass of water in Prefrontal.")
    static var openAppWhenRun: Bool { false }

    @Parameter(title: "Check")
    var key: String

    /// Wrap a quota check that's already at its target back to zero instead of
    /// logging another (the tap-at-max cycle), matching the Me tab and web
    /// dashboard. Set by the widget button when the check is done.
    @Parameter(title: "Reset", default: false)
    var reset: Bool

    init() {}
    init(key: String, reset: Bool = false) { self.key = key; self.reset = reset }

    func perform() async throws -> some IntentResult {
        let client = try APIClient(shared: ())
        try await client.markSelfCare(key: key, reset: reset)
        return .result()
    }

    static var parameterSummary: some ParameterSummary {
        Summary("Log \(\.$key) in Prefrontal")
    }
}
