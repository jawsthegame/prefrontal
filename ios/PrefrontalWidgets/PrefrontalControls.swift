import WidgetKit
import SwiftUI
import AppIntents

/// Control Center controls (iOS 18). Each runs a **no-input** App Intent from
/// `PrefrontalIntents.swift` (compiled into this extension too) so the action
/// fires straight from Control Center or the Lock Screen without opening the
/// app. Add via **Settings ▸ Control Center** — or assign one to the **Action
/// Button** (Settings ▸ Action Button ▸ Controls). See issue #471.
///
/// Only no-input actions belong here: a Control Center tap can't collect a todo
/// title or an outing intention, so Add Todo / Going Out / Start Focus stay in
/// Siri/Shortcuts (they prompt) rather than becoming controls.

@available(iOS 18.0, *)
struct PanicControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.morningstatic.prefrontal.control.panic") {
            ControlWidgetButton(action: PanicIntent()) {
                Label("Panic", systemImage: "exclamationmark.triangle.fill")
            }
        }
        .displayName("Panic")
        .description("What's on fire, and one first step.")
    }
}

@available(iOS 18.0, *)
struct ImBackControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.morningstatic.prefrontal.control.imback") {
            ControlWidgetButton(action: ImBackIntent()) {
                Label("I'm Back", systemImage: "house")
            }
        }
        .displayName("I'm Back")
        .description("End the current outing.")
    }
}

@available(iOS 18.0, *)
struct WrapUpFocusControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.morningstatic.prefrontal.control.focusend") {
            ControlWidgetButton(action: EndFocusIntent()) {
                Label("Wrap Up Focus", systemImage: "flag.checkered")
            }
        }
        .displayName("Wrap Up Focus")
        .description("End the current focus session.")
    }
}
