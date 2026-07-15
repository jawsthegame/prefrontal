import AppIntents

// MARK: - Siri phrases / App Shortcuts

/// Every phrase must contain the `\(.applicationName)` token. These surface in
/// Spotlight and Siri and are assignable to the Action Button.
struct PrefrontalShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: PanicIntent(),
            phrases: ["Panic in \(.applicationName)", "I'm overwhelmed, \(.applicationName)"],
            shortTitle: "Panic", systemImageName: "exclamationmark.triangle.fill"
        )
        AppShortcut(
            intent: AddTodoIntent(),
            phrases: ["Add a todo in \(.applicationName)", "Capture a todo in \(.applicationName)"],
            shortTitle: "Add Todo", systemImageName: "plus.circle"
        )
        AppShortcut(
            intent: BrainDumpIntent(),
            phrases: ["Brain-dump in \(.applicationName)", "Dump my thoughts into \(.applicationName)"],
            shortTitle: "Brain-dump", systemImageName: "brain.head.profile"
        )
        AppShortcut(
            intent: GoingOutIntent(),
            phrases: ["I'm going out with \(.applicationName)", "Start an outing in \(.applicationName)"],
            shortTitle: "Going Out", systemImageName: "figure.walk.departure"
        )
        AppShortcut(
            intent: ImBackIntent(),
            phrases: ["I'm back, \(.applicationName)", "I've returned in \(.applicationName)"],
            shortTitle: "I'm Back", systemImageName: "house"
        )
        AppShortcut(
            intent: StartFocusIntent(),
            phrases: ["Start a focus session in \(.applicationName)", "Focus with \(.applicationName)"],
            shortTitle: "Start Focus", systemImageName: "scope"
        )
        AppShortcut(
            intent: EndFocusIntent(),
            phrases: ["Wrap up focus in \(.applicationName)", "End my \(.applicationName) focus"],
            shortTitle: "Wrap Up Focus", systemImageName: "flag.checkered"
        )
        AppShortcut(
            intent: MadeItIntent(),
            phrases: ["I made it, \(.applicationName)", "Log made it in \(.applicationName)"],
            shortTitle: "Made It", systemImageName: "checkmark"
        )
        // NOTE: AppShortcutsProvider caps at 10 entries. Missed It, Log Trip, and
        // Reflective Pause stay full App Intents (usable in the Shortcuts app /
        // Action Button) but aren't given an auto Siri phrase here, so the two
        // flagship captures — Brain-dump (above) and Capture a Thought — fit the cap.
        // "Capture this thought" — the headline zero-friction sensor capture.
        AppShortcut(
            intent: CaptureThoughtIntent(),
            phrases: ["Capture this thought in \(.applicationName)",
                      "Note a thought in \(.applicationName)"],
            shortTitle: "Capture a Thought", systemImageName: "square.and.pencil"
        )
        AppShortcut(
            intent: CaptureImpulseIntent(),
            phrases: ["Capture an impulse in \(.applicationName)", "Park an impulse in \(.applicationName)"],
            shortTitle: "Capture Impulse", systemImageName: "tray.and.arrow.down"
        )
    }
}
