import SwiftUI

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
                AvailableHoursSection()
                locationSection
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

    /// Read-only App Group health, to diagnose the "widget won't connect" case:
    /// the app writes its base URL + token into the shared App Group container,
    /// and the widget reads them back. This shows what the *app* sees; if a token
    /// is present here but the widget still says "Tap to connect," the App Group
    /// capability isn't provisioned into the *widget* target.
    /// Opt-in geofencing: on, it prompts for Always-location and monitors your
    /// curated places so leaving home auto-logs a departure (no Shortcut needed).
    private var locationSection: some View {
        Section("Location automations") {
            Toggle("Auto-log leaving home & arrivals", isOn: Binding(
                get: { config.locationEnabled },
                set: { on in
                    config.locationEnabled = on
                    if on { LocationMonitor.shared.enable() } else { LocationMonitor.shared.disable() }
                }
            ))
            Text("Uses background location to notice when you leave a curated place (Always access). Add places with `prefrontal place add`.")
                .font(.caption).foregroundStyle(Brand.muted)
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
