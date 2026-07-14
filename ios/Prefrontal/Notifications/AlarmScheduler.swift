import Foundation

/// Native system-alarm scheduling via **AlarmKit** (iOS 26+).
///
/// AlarmKit is the first public API for creating a real Clock-style alarm — one
/// that rings through silent mode and Focus like the system alarm, which a plain
/// `UNNotification` cannot. It's the native replacement for the "Set Alarm" iOS
/// Shortcut the evening `morning_prep` (Time Blindness) nudge used to deep-link
/// to — the single "hard case" in `docs/shortcuts-to-native.md`.
///
/// This is deliberately additive and gated: the app's deployment target is iOS
/// 17, so everything AlarmKit-specific lives behind `#if canImport(AlarmKit)` +
/// `@available(iOS 26.0, *)`. On anything older (or if AlarmKit isn't linked),
/// `scheduleWake` returns `false` and the caller falls back to opening the
/// `shortcuts://` deep link, exactly as before — see `AppDelegate` in
/// `PushNotifications.swift`.
enum AlarmScheduler {
    /// Parse a 24-hour `HH:MM` string into `(hour, minute)`; `nil` if malformed
    /// or out of range. Pure — the wake time the server sends in the nudge.
    static func parseHHMM(_ s: String) -> (hour: Int, minute: Int)? {
        let parts = s.split(separator: ":", maxSplits: 1)
        guard parts.count == 2,
              let hour = Int(parts[0]), let minute = Int(parts[1]),
              (0..<24).contains(hour), (0..<60).contains(minute)
        else { return nil }
        return (hour, minute)
    }

    /// Extract the wake time from the "Set alarm" action's `shortcuts://` URL,
    /// whose `text` query item carries the suggested `HH:MM` (built server-side by
    /// `notify.alarm_actions`). `nil` for any URL without a valid time — so a
    /// non-alarm client-side action never gets mistaken for one.
    static func wakeTime(from url: URL) -> (hour: Int, minute: Int)? {
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let text = comps.queryItems?.first(where: { $0.name == "text" })?.value
        else { return nil }
        return parseHHMM(text)
    }

    /// Schedule a one-off system alarm at the next occurrence of `hour:minute`
    /// (local). Ensures AlarmKit authorization first (prompting once if it's
    /// undetermined). Returns whether an alarm was actually scheduled — `false`
    /// means the caller should fall back to the Shortcut (OS too old, AlarmKit
    /// unavailable, authorization denied, or a scheduling error).
    @discardableResult
    static func scheduleWake(
        hour: Int, minute: Int, label: String = "Time to get up"
    ) async -> Bool {
        #if canImport(AlarmKit)
        if #available(iOS 26.0, *) {
            return await AlarmKitBackend.scheduleWake(hour: hour, minute: minute, label: label)
        }
        #endif
        return false
    }
}

#if canImport(AlarmKit)
import AlarmKit
import SwiftUI

/// The AlarmKit-specific half, isolated so the file still compiles on toolchains
/// (and runs on OS versions) without AlarmKit. Never referenced outside the
/// `canImport` + availability guards in `AlarmScheduler`.
@available(iOS 26.0, *)
private enum AlarmKitBackend {
    /// Empty metadata — Prefrontal attaches no custom alarm state; the type only
    /// satisfies `AlarmAttributes`' generic requirement.
    struct WakeMetadata: AlarmMetadata {}

    static func scheduleWake(hour: Int, minute: Int, label: String) async -> Bool {
        let manager = AlarmManager.shared

        // Authorization: prompt once if undetermined, honor a prior denial.
        switch manager.authorizationState {
        case .authorized:
            break
        case .notDetermined:
            guard let state = try? await manager.requestAuthorization(),
                  state == .authorized
            else { return false }
        default:
            return false
        }

        // Fire at the next wall-clock occurrence of the wake time, one-shot.
        let time = Alarm.Schedule.Relative.Time(hour: hour, minute: minute)
        let schedule = Alarm.Schedule.relative(.init(time: time, repeats: .never))

        let stopButton = AlarmButton(
            text: "Stop", textColor: .white, systemImageName: "stop.circle.fill")
        let alert = AlarmPresentation.Alert(
            title: LocalizedStringResource(stringLiteral: label), stopButton: stopButton)
        let attributes = AlarmAttributes(
            presentation: AlarmPresentation(alert: alert),
            metadata: WakeMetadata(),
            tintColor: Brand.accent)
        let configuration = AlarmManager.AlarmConfiguration(
            schedule: schedule, attributes: attributes)

        do {
            _ = try await manager.schedule(id: UUID(), configuration: configuration)
            return true
        }
        catch {
            return false
        }
    }
}
#endif
