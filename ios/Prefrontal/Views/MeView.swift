import SwiftUI

struct MeView: View {
    @State private var selfCare: SelfCare?
    @State private var error: String?
    @State private var loaded = false
    @State private var showFocus = false
    @State private var showOuting = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                selfCareCard
                actionsCard
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Me")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink { SettingsView() } label: { Image(systemName: "gearshape") }
            }
        }
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(isPresented: $showFocus) { StartSheet(kind: .focus) { await load() } }
        .sheet(isPresented: $showOuting) { StartSheet(kind: .outing) { await load() } }
    }

    private var selfCareCard: some View {
        Card {
            CardLabel(text: "Self-care today")
            if let sc = selfCare, sc.enabled {
                ForEach(sc.checks.filter { $0.enabled }) { check in
                    HStack {
                        Image(systemName: check.satisfied ? "checkmark.circle.fill" : (check.overdue ? "exclamationmark.circle" : "circle"))
                            .foregroundStyle(check.satisfied ? Brand.ok : (check.overdue ? Brand.warn : Brand.muted))
                        Text(label(check.key)).foregroundStyle(Brand.nearWhite)
                        Spacer()
                        Text("\(check.count)/\(check.target)").font(.caption).foregroundStyle(Brand.muted)
                        AsyncButton {
                            try await withAPI { try await $0.markSelfCare(key: check.key) }
                            await load()
                        } label: {
                            Text(logVerb(check.key)).font(.caption.weight(.semibold))
                        } onError: { error = $0 }
                        .buttonStyle(.bordered).tint(Brand.teal)
                        .disabled(check.satisfied)
                    }
                }
            } else if selfCare != nil {
                Text("Self-care checks are off. Enable them in the web settings.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                Text("…").foregroundStyle(Brand.muted)
            }
        }
    }

    private var actionsCard: some View {
        Card {
            CardLabel(text: "Start something")
            Button { showFocus = true } label: {
                Label("Start a focus session", systemImage: "scope")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.bordered).tint(Brand.blue)
            Button { showOuting = true } label: {
                Label("Going out", systemImage: "figure.walk.departure")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.bordered).tint(Brand.teal)
        }
    }

    private func label(_ key: String) -> String {
        ["meal": "Meals", "water": "Water", "meds": "Meds", "biobreak": "Breaks",
         "winddown": "Wind-down", "movement": "Movement"][key] ?? key.capitalized
    }
    private func logVerb(_ key: String) -> String {
        ["meal": "Ate", "water": "Drank", "meds": "Took", "biobreak": "Done",
         "winddown": "Done", "movement": "Moved"][key] ?? "Log"
    }

    private func load() async {
        do {
            selfCare = try await withAPI { try await $0.selfCare() }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// Sheet to start a focus session or an outing.
struct StartSheet: View {
    enum Kind { case focus, outing }
    let kind: Kind
    let onDone: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var minutes = 30
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section(kind == .focus ? "What are you focusing on?" : "What's the plan?") {
                    TextField(kind == .focus ? "e.g. Roast batches" : "e.g. coffee, back in 15", text: $text)
                    Stepper("~\(minutes) min", value: $minutes, in: 5...240, step: 5)
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle(kind == .focus ? "Focus" : "Going out")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Start") { Task { await save() } }
                        .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        let t = text.trimmingCharacters(in: .whitespaces)
        do {
            try await withAPI { client in
                switch kind {
                case .focus: try await client.startFocus(task: t, minutes: minutes)
                case .outing: try await client.startOuting(intention: t, minutes: minutes)
                }
            }
            await onDone()
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
