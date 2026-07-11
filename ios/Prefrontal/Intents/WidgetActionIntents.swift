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

    init() {}
    init(key: String) { self.key = key }

    func perform() async throws -> some IntentResult {
        let client = try APIClient(shared: ())
        try await client.markSelfCare(key: key)
        return .result()
    }

    static var parameterSummary: some ParameterSummary {
        Summary("Log \(\.$key) in Prefrontal")
    }
}
