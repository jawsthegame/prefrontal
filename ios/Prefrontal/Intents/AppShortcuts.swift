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
        // NOTE: AppShortcutsProvider caps at 10 entries. MissedItIntent is still a
        // full App Intent (usable in the Shortcuts app / Action Button) but isn't
        // given an auto Siri phrase here — its common path is the notification
        // action buttons — so the three added below fit within the cap.
        AppShortcut(
            intent: CaptureImpulseIntent(),
            phrases: ["Capture an impulse in \(.applicationName)", "Park a thought in \(.applicationName)"],
            shortTitle: "Capture Impulse", systemImageName: "tray.and.arrow.down"
        )
        AppShortcut(
            intent: ReflectivePauseIntent(),
            phrases: ["Reflective pause in \(.applicationName)", "I want to switch, \(.applicationName)"],
            shortTitle: "Reflective Pause", systemImageName: "pause.circle"
        )
        AppShortcut(
            intent: LogTripIntent(),
            phrases: ["Log a trip in \(.applicationName)", "Close out my trip in \(.applicationName)"],
            shortTitle: "Log Trip", systemImageName: "car"
        )
    }
}
