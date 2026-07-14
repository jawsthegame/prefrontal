import SwiftUI

/// The per-user **Features** control on the Settings screen — a toggle per
/// deployment-enabled module and per enabled Context pack. Turning one off is a
/// per-user overlay (`module_enabled:<key>` / `pack_enabled:<key>` via
/// `/settings/features`), leaving the deployment default and everyone else
/// untouched. The intent: not everyone has the same symptoms, and fewer, focused
/// nudges beat an overwhelming pile. Turning a pack off hides its situation tools
/// and care surface for this user (a surfaces overlay).
struct FeaturesSection: View {
    @State private var list = FeatureList(modules: [], packs: [])
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        Section {
            if let error { Text(error).font(.footnote).foregroundStyle(Brand.danger) }
            if list.modules.isEmpty && list.packs.isEmpty && loaded && error == nil {
                Text("No modules or packs are enabled on this deployment.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            }
            ForEach(list.packs) { row($0, kind: "packs") }
            ForEach(list.modules) { row($0, kind: "modules") }
        } header: {
            Text("Features")
        } footer: {
            Text("Turn off support behaviors you don't want — this is just for you and doesn't change the deployment defaults. Turning a pack off hides its situation tools and care surface for you.")
        }
        .task { if !loaded { await load() } }
    }

    private func row(_ m: FeatureModule, kind: String) -> some View {
        Toggle(isOn: binding(m, kind: kind)) {
            VStack(alignment: .leading, spacing: 2) {
                Text(m.title)
                if let blurb = m.blurb {
                    Text(blurb).font(.caption).foregroundStyle(Brand.muted)
                }
            }
        }
        .tint(Brand.accent)
    }

    private func current(_ kind: String) -> [FeatureModule] {
        kind == "packs" ? list.packs : list.modules
    }

    private func binding(_ m: FeatureModule, kind: String) -> Binding<Bool> {
        Binding(
            get: { current(kind).first(where: { $0.key == m.key })?.enabled ?? m.enabled },
            set: { on in
                // Optimistic flip so the switch doesn't bounce while the write is in
                // flight; the response then reconciles authoritatively.
                apply(kind: kind, key: m.key, enabled: on)
                Task { await save(kind: kind, key: m.key, enabled: on) }
            }
        )
    }

    /// Mutate the local copy of one row's `enabled` (optimistic).
    private func apply(kind: String, key: String, enabled: Bool) {
        func flip(_ rows: [FeatureModule]) -> [FeatureModule] {
            rows.map { $0.key == key ? $0.setting(enabled: enabled) : $0 }
        }
        if kind == "packs" {
            list = FeatureList(modules: list.modules, packs: flip(list.packs))
        } else {
            list = FeatureList(modules: flip(list.modules), packs: list.packs)
        }
    }

    private func load() async {
        do {
            list = try await withAPI { try await $0.features() }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }

    private func save(kind: String, key: String, enabled: Bool) async {
        do {
            list = try await withAPI { try await $0.setFeature(kind: kind, key: key, enabled: enabled) }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            await load()  // reconcile the optimistic flip with server truth
        }
    }
}
