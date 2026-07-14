import SwiftUI

/// The per-user **Features** control on the Settings screen — a toggle per
/// deployment-enabled module. Turning one off disables it for this user only
/// (a `module_enabled:<key>` overlay via `/settings/features`), leaving the
/// deployment default and everyone else untouched. The intent: not everyone has
/// the same symptoms, and fewer, focused nudges beat an overwhelming pile.
struct FeaturesSection: View {
    @State private var modules: [FeatureModule] = []
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        Section {
            if let error { Text(error).font(.footnote).foregroundStyle(Brand.danger) }
            if modules.isEmpty && loaded && error == nil {
                Text("No modules are enabled on this deployment.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            }
            ForEach(modules) { m in
                Toggle(isOn: binding(m)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(m.title)
                        if let ch = m.challenge, !ch.isEmpty {
                            Text(ch).font(.caption).foregroundStyle(Brand.muted)
                        }
                    }
                }
                .tint(Brand.accent)
            }
        } header: {
            Text("Features")
        } footer: {
            Text("Turn off support behaviors you don't want — this is just for you and doesn't change the deployment defaults.")
        }
        .task { if !loaded { await load() } }
    }

    private func binding(_ m: FeatureModule) -> Binding<Bool> {
        Binding(
            get: { modules.first(where: { $0.key == m.key })?.enabled ?? m.enabled },
            set: { on in
                // Optimistic flip so the switch doesn't bounce back while the write
                // is in flight; the response then reconciles authoritatively.
                modules = modules.map { $0.key == m.key ? $0.setting(enabled: on) : $0 }
                Task { await save(key: m.key, enabled: on) }
            }
        )
    }

    private func load() async {
        do {
            modules = try await withAPI { try await $0.features() }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }

    private func save(key: String, enabled: Bool) async {
        do {
            modules = try await withAPI { try await $0.setFeature(key: key, enabled: enabled) }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            await load()  // reconcile the optimistic flip with server truth
        }
    }
}
