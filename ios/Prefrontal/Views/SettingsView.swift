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
        }
        .brandScreen()
        .navigationTitle(isOnboarding ? "Welcome" : "Settings")
        .onAppear {
            url = config.baseURLString
            token = config.token
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
