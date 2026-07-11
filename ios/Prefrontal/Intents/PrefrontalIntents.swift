import AppIntents
import WidgetKit

/// App Intents expose Prefrontal's core one-tap actions to Siri, Shortcuts,
/// Spotlight, and the Action Button — replacing the hand-built "Get Contents of
/// URL" shortcuts (`deploy/ios-shortcut.md`) and their pasted tokens. Each intent
/// authenticates the same way the widget does — `APIClient(shared:)` reads the
/// base URL + token from the App Group — so it runs without launching the UI
/// (`openAppWhenRun == false`), even when the app isn't in memory.
///
/// Registered for Siri via `PrefrontalShortcuts` in `AppShortcuts.swift` (app
/// target only). These intent types are also compiled into the widget extension
/// so the Control Center controls (`PrefrontalControls.swift`) can call them.
/// See issues #470, #471.

// MARK: - Shared helpers

/// Build a client from the App Group store, mapping "not connected yet" to a
/// user-facing message instead of a raw decode/config error.
private func prefrontalClient() throws -> APIClient {
    do { return try APIClient(shared: ()) }
    catch { throw PrefrontalIntentError.notConnected }
}

enum PrefrontalIntentError: Error, CustomLocalizedStringResourceConvertible {
    case notConnected
    var localizedStringResource: LocalizedStringResource {
        "Open Prefrontal and connect to your server first."
    }
}

/// Nudge the Home/Lock Screen widget to refresh after a state-changing action.
private func reloadWidgets() {
    WidgetCenter.shared.reloadAllTimelines()
}

// MARK: - Capture

struct AddTodoIntent: AppIntent {
    static let title: LocalizedStringResource = "Add Todo"
    static let description = IntentDescription("Capture a todo in Prefrontal.")
    static var openAppWhenRun: Bool { false }

    @Parameter(title: "Todo", requestValueDialog: "What should I add?")
    var text: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        let title = text.trimmingCharacters(in: .whitespacesAndNewlines)
        try await client.addTodo(title: title)
        reloadWidgets()
        return .result(dialog: "Added “\(title)”.")
    }

    static var parameterSummary: some ParameterSummary {
        Summary("Add \(\.$text) to Prefrontal")
    }
}

// MARK: - Panic

struct PanicIntent: AppIntent {
    static let title: LocalizedStringResource = "Panic"
    static let description = IntentDescription("Ask Prefrontal what's on fire and one first step.")
    // Read-only triage; no need to open the app — just speak/show the result.
    static var openAppWhenRun: Bool { false }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        let p = try await client.panic()
        var msg = p.headline
        if let step = p.firstStep, !step.isEmpty { msg += " First step: \(step)" }
        return .result(dialog: IntentDialog(stringLiteral: msg))
    }
}

// MARK: - Outings

struct GoingOutIntent: AppIntent {
    static let title: LocalizedStringResource = "Going Out"
    static let description = IntentDescription("Start an outing with an intention and optional time window.")
    static var openAppWhenRun: Bool { false }

    @Parameter(title: "Intention", requestValueDialog: "What's the plan?")
    var intention: String
    @Parameter(title: "Time window (min)")
    var minutes: Int?

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        let what = intention.trimmingCharacters(in: .whitespacesAndNewlines)
        try await client.startOuting(intention: what, minutes: minutes)
        reloadWidgets()
        if let m = minutes {
            return .result(dialog: "Out for “\(what)” — back in about \(m) min.")
        }
        return .result(dialog: "Out for “\(what)”.")
    }

    static var parameterSummary: some ParameterSummary {
        Summary("Going out — \(\.$intention)") { \.$minutes }
    }
}

struct ImBackIntent: AppIntent {
    static let title: LocalizedStringResource = "I'm Back"
    static let description = IntentDescription("End the current outing.")
    static var openAppWhenRun: Bool { false }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        try await client.returnOuting()
        reloadWidgets()
        return .result(dialog: "Welcome back.")
    }
}

// MARK: - Focus

struct StartFocusIntent: AppIntent {
    static let title: LocalizedStringResource = "Start Focus"
    static let description = IntentDescription("Start a focus session on a task, with an optional planned length.")
    static var openAppWhenRun: Bool { false }

    @Parameter(title: "Task", requestValueDialog: "What are you focusing on?")
    var task: String
    @Parameter(title: "Planned (min)")
    var minutes: Int?

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        let what = task.trimmingCharacters(in: .whitespacesAndNewlines)
        try await client.startFocus(task: what, minutes: minutes)
        reloadWidgets()
        return .result(dialog: "Focusing on “\(what)”.")
    }

    static var parameterSummary: some ParameterSummary {
        Summary("Start focus — \(\.$task)") { \.$minutes }
    }
}

struct EndFocusIntent: AppIntent {
    static let title: LocalizedStringResource = "Wrap Up Focus"
    static let description = IntentDescription("End the current focus session.")
    static var openAppWhenRun: Bool { false }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        try await client.endFocus()
        reloadWidgets()
        return .result(dialog: "Focus wrapped up.")
    }
}

// MARK: - Departure outcome

struct MadeItIntent: AppIntent {
    static let title: LocalizedStringResource = "Made It"
    static let description = IntentDescription("Log that you made your departure on time.")
    static var openAppWhenRun: Bool { false }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        try await client.logShortcut(action: "made_it")
        return .result(dialog: "Logged — you made it.")
    }
}

struct MissedItIntent: AppIntent {
    static let title: LocalizedStringResource = "Missed It"
    static let description = IntentDescription("Log that you missed your departure.")
    static var openAppWhenRun: Bool { false }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let client = try prefrontalClient()
        try await client.logShortcut(action: "missed_it")
        return .result(dialog: "Logged — noted the miss.")
    }
}
