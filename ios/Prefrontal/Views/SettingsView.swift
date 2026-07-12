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
                           ? "— not set" : "set · \(SharedStore.token.count) chars")
            LabeledContent("Server URL", value: SharedStore.baseURL)
            Text("These are shared with the widget via the App Group. If a token shows here but the widget still says “Tap to connect,” the App Group capability isn't provisioned into the widget target — enable it on **both** targets (same Team) in Signing & Capabilities, then delete the app and reinstall.")
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
