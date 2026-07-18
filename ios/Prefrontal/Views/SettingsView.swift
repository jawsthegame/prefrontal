import SwiftUI
import CoreLocation
import UIKit

struct SettingsView: View {
    @EnvironmentObject var config: AppConfig
    @Environment(\.dismiss) private var dismiss
    var isOnboarding = false

    @State private var url = ""
    @State private var token = ""
    @State private var status: String?
    @State private var testing = false
    @State private var ok = false

    var body: some View {
        Form {
            if isOnboarding {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Connect to Prefrontal")
                            .font(.title2.weight(.bold))
                            .foregroundStyle(Brand.nearWhite)
                        Text("Enter your server URL and personal token. Both come from your setup sheet (or `prefrontal user` output).")
                            .font(.footnote).foregroundStyle(Brand.muted)
                    }
                    .listRowBackground(Color.clear)
                }
            }

            Section("Server URL") {
                TextField("https://…ts.net", text: $url)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            Section("Token") {
                SecureField("X-Prefrontal-Token", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }

            if let status {
                Section {
                    Text(status)
                        .font(.footnote)
                        .foregroundStyle(ok ? Brand.ok : Brand.danger)
                }
            }

            Section {
                Button {
                    Task { await testAndSave() }
                } label: {
                    HStack {
                        if testing { ProgressView().controlSize(.small) }
                        Text(isOnboarding ? "Connect" : "Test & Save")
                    }
                }
                .disabled(testing || url.isEmpty || token.isEmpty)
            }

            if !isOnboarding {
                FeaturesSection()
                VacationSection()
                AvailableHoursSection()
                AppLockSection()
                LocationSection()
                diagnostics
            }
        }
        .brandScreen()
        .navigationTitle(isOnboarding ? "Welcome" : "Settings")
        .onAppear {
            url = config.baseURLString
            token = config.token
        }
    }

    private var diagnostics: some View {
        Section("Diagnostics") {
            LabeledContent("App Group", value: SharedStore.appGroup)
            LabeledContent("Shared store",
                           value: UserDefaults(suiteName: SharedStore.appGroup) != nil
                               ? "initialized" : "unavailable (app-local fallback)")
            LabeledContent("Token", value: SharedStore.token.isEmpty
                           ? "— not set" : "set · \(SharedStore.token.count) chars (Keychain)")
            LabeledContent("Server URL", value: SharedStore.baseURL)
            Text("The server URL is shared with the widget via the App Group; the token lives in a shared **Keychain** group. If a token shows here but the widget still says “Tap to connect,” the App Group and Keychain-sharing capabilities aren't provisioned into the widget target — enable both on **both** targets (same Team) in Signing & Capabilities, then delete the app and reinstall.")
                .font(.caption).foregroundStyle(Brand.muted)
        }
    }

    private func testAndSave() async {
        testing = true; status = nil; ok = false
        defer { testing = false }
        // Persist first so APIClient() picks up the new values.
        config.baseURLString = url.trimmingCharacters(in: .whitespaces)
        config.token = token.trimmingCharacters(in: .whitespaces)
        do {
            _ = try await withAPI { try await $0.selfCare() }
            ok = true; status = "Connected ✓"
            if isOnboarding {
                try? await Task.sleep(nanoseconds: 400_000_000)
                dismiss()
            }
        } catch {
            ok = false
            status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}

/// Opt-in biometric app lock. Only rendered when the device actually has
/// enrolled biometrics (`BiometricLock.isAvailable`) — otherwise the toggle would
/// be a dead control. Flipping it on locks immediately (and the next launch /
/// foreground prompts); off unlocks. Wording adapts to Face ID vs Touch ID.
struct AppLockSection: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var lock = BiometricLock.shared

    var body: some View {
        if lock.isAvailable {
            Section("App lock") {
                Toggle("Require \(lock.biometryName)", isOn: Binding(
                    get: { config.appLockEnabled },
                    set: { on in
                        config.appLockEnabled = on
                        lock.settingChanged(enabled: on)
                    }
                ))
                Text("Locks Prefrontal behind \(lock.biometryName) on launch and when it returns from the background. Your device passcode still works as a fallback.")
                    .font(.caption).foregroundStyle(Brand.muted)
            }
        }
    }
}

/// Manual vacation-mode control (`GET`/`POST /vacation`). The escape hatch the
/// design keeps alongside the location-cued auto-suggestion: a toggle to ease off
/// the non-urgent nudges for a staycation location can't detect, or to correct a
/// false positive. While on, the coaching engine holds discretionary cues — a
/// flight or hard commitment still gets through, and returning home lifts it
/// automatically. Loads current state, writes through on toggle, resyncs on error.
struct VacationSection: View {
    @State private var vacation: Vacation?
    @State private var loaded = false
    @State private var busy = false
    @State private var status: String?

    var body: some View {
        Section("Vacation mode") {
            if !loaded {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading…").font(.footnote).foregroundStyle(Brand.muted)
                }
            } else {
                Toggle("Ease off the nudges", isOn: Binding(
                    get: { vacation?.active ?? false },
                    set: { on in Task { await set(on) } }
                ))
                .disabled(busy)

                if vacation?.active == true {
                    Text(activeSubtitle)
                        .font(.caption).foregroundStyle(Brand.muted)
                } else {
                    Text("Holds the non-urgent nudges while you're away — a flight or hard commitment still gets through. It turns itself off when you get home; this switch is for staycations or to fix a wrong guess.")
                        .font(.caption).foregroundStyle(Brand.muted)
                }
                if let status {
                    Text(status).font(.caption).foregroundStyle(Brand.danger)
                }
            }
        }
        .task { if !loaded { await load() } }
    }

    private var activeSubtitle: String {
        let auto = vacation?.source == "auto"
        let how = auto ? "auto-detected from your trip" : "on"
        // Reuse PFDate (en_US_POSIX, UTC → local "Tue 3:40 PM"); "" on parse fail.
        let when = PFDate.dayTime(vacation?.since)
        if !when.isEmpty {
            return "🏝️ Eased off since \(when) (\(how)). Turn off to resume now."
        }
        return "🏝️ Nudges eased off (\(how)). Turn off to resume now."
    }

    private func load() async {
        do { vacation = try await withAPI { try await $0.vacation() }; status = nil }
        catch { status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription }
        loaded = true
    }

    private func set(_ on: Bool) async {
        busy = true; status = nil
        defer { busy = false }
        do { vacation = try await withAPI { try await $0.setVacation(on) } }
        catch {
            status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            await load()  // resync the toggle to what's actually stored
        }
    }
}

/// Per-weekday "available hours" — the app's first server-backed editable
/// preference. Loads the seven-day schedule from `/schedule/available-hours`,
/// renders a toggle + two time pickers per day, and writes each edited day back
/// (a partial POST the server merges). The hours gate slot-finding and todo
/// suggestions server-side; an off day is skipped entirely.
struct AvailableHoursSection: View {
    @State private var days: [String: AvailableHours.Day] = [:]
    @State private var loaded = false
    @State private var status: String?

    var body: some View {
        Section("Available hours") {
            if !loaded {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading…").font(.footnote).foregroundStyle(Brand.muted)
                }
            } else {
                ForEach(AvailableHours.order, id: \.self) { key in
                    dayRow(key)
                }
                Text("The hours you're generally free each day. Prefrontal only offers open time and suggests todos inside these — an off day is skipped entirely. Until set, each day inherits your default waking hours.")
                    .font(.caption).foregroundStyle(Brand.muted)
                if let status {
                    Text(status).font(.caption).foregroundStyle(Brand.danger)
                }
            }
        }
        .task { if !loaded { await load() } }
    }

    @ViewBuilder private func dayRow(_ key: String) -> some View {
        if days[key] != nil {
            HStack(spacing: 10) {
                Text(AvailableHours.label(key))
                    .font(.subheadline).frame(width: 42, alignment: .leading)
                Spacer(minLength: 0)
                if days[key]?.available == true {
                    DatePicker("", selection: timeBinding(key, \.start),
                               displayedComponents: .hourAndMinute).labelsHidden()
                    Text("–").foregroundStyle(Brand.muted)
                    DatePicker("", selection: timeBinding(key, \.end),
                               displayedComponents: .hourAndMinute).labelsHidden()
                } else {
                    Text("Off").font(.footnote).foregroundStyle(Brand.muted)
                }
                Toggle("", isOn: availableBinding(key)).labelsHidden()
            }
        }
    }

    private func availableBinding(_ key: String) -> Binding<Bool> {
        Binding(
            get: { days[key]?.available ?? true },
            set: { on in days[key]?.available = on; Task { await save(key) } }
        )
    }

    /// A `Date` binding over a day's `"HH:MM"` string field, for the picker.
    private func timeBinding(_ key: String, _ field: WritableKeyPath<AvailableHours.Day, String>) -> Binding<Date> {
        Binding(
            get: { AvailableHours.date(from: days[key]?[keyPath: field] ?? "09:00") },
            set: { d in days[key]?[keyPath: field] = AvailableHours.hhmm(from: d); Task { await save(key) } }
        )
    }

    private func load() async {
        do { apply(try await withAPI { try await $0.availableHours() }) }
        catch { status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription }
        loaded = true
    }

    private func save(_ key: String) async {
        guard let day = days[key] else { return }
        // Both bounds are zero-padded HH:MM, so a string compare is chronological.
        // The server enforces this too (422); catching it here avoids a round-trip.
        if day.available && day.start >= day.end {
            status = "\(AvailableHours.label(key)): end must be after start"
            return
        }
        status = nil
        do { apply(try await withAPI { try await $0.setAvailableHours([key: day]) }) }
        catch {
            status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            await load()  // resync the controls to what's actually stored
        }
    }

    private func apply(_ h: AvailableHours) { days = h.days }
}

/// Location-permission UX for the geofence/visit auto-logging (#566). Owns the
/// opt-in toggle plus everything that keeps it honest: always-on priming text
/// (so the one-shot system prompt isn't wasted), a status row reflecting the
/// *true* CoreLocation authorization, an "upgrade to Always" nudge when only
/// While-Using was granted, and an "open iOS Settings" path when it's denied.
/// Observes `LocationMonitor` so it re-renders the moment authorization changes.
struct LocationSection: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var monitor = LocationMonitor.shared
    @Environment(\.openURL) private var openURL

    private var denied: Bool {
        monitor.authorization == .denied || monitor.authorization == .restricted
    }

    var body: some View {
        Section("Location automations") {
            Toggle("Auto-log leaving home & arrivals", isOn: Binding(
                get: { config.locationEnabled },
                set: { on in
                    guard on else { config.locationEnabled = false; monitor.disable(); return }
                    // Already denied → the system won't prompt; leave the toggle
                    // off and let the "Open iOS Settings" row below guide them.
                    guard !denied else { return }
                    config.locationEnabled = true
                    monitor.enable()  // prompts for Always (or upgrades from While-Using)
                }
            ))

            // Priming, always visible so the user knows what the system prompt is
            // for before it appears — a one-shot dialog we can't re-trigger.
            Text("Uses background location to auto-log when you leave home and arrive places — no Shortcut needed. Needs **Always** access to work in the background; add places with `prefrontal place add`.")
                .font(.caption).foregroundStyle(Brand.muted)

            LabeledContent("Permission", value: authText)

            if config.locationEnabled, monitor.authorization == .authorizedWhenInUse {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Prefrontal only has “While Using” access, so departures and arrivals won't log when the app is closed. Grant **Always** for background auto-logging.")
                        .font(.caption).foregroundStyle(Brand.warn)
                    Button("Upgrade to Always") { monitor.requestAlways() }
                        .font(.caption.weight(.semibold))
                }
            }

            if denied {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Location is off for Prefrontal in iOS Settings, so auto-logging can't run.")
                        .font(.caption).foregroundStyle(Brand.danger)
                    Button("Open iOS Settings") {
                        if let u = URL(string: UIApplication.openSettingsURLString) { openURL(u) }
                    }
                    .font(.caption.weight(.semibold))
                }
            }
        }
        .onAppear(perform: reconcile)
        .onChange(of: monitor.authorization) { _, _ in reconcile() }
    }

    private var authText: String {
        switch monitor.authorization {
        case .authorizedAlways: return "Always ✓"
        case .authorizedWhenInUse: return "While Using"
        case .denied: return "Denied"
        case .restricted: return "Restricted"
        case .notDetermined: return "Not requested"
        @unknown default: return "Unknown"
        }
    }

    /// Keep the stored opt-in honest: if the system says denied/restricted, the
    /// toggle can't truly be "on", so flip it off (monitoring is already stopped
    /// in `LocationMonitor`). Runs on appear and on any authorization change, so a
    /// revoke made in the Settings app is reflected when the user returns.
    private func reconcile() {
        if denied, config.locationEnabled {
            config.locationEnabled = false
            monitor.disable()
        }
    }
}
