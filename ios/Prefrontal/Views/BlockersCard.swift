import SwiftUI

/// **Waiting on you** — people blocked on *you* (the ball's in your court),
/// the mirror of a todo. Self-loads `GET /blockers` (open, most pressing first)
/// and feeds prioritization the same way the web dashboard's card does: it names
/// who's waiting and how long, so an unblock is hard to miss when you're deciding
/// what to do next (the counterweight to shiny-object syndrome). Tapping
/// **Delivered** resolves one (`POST /blockers/{id}/resolve`); the ＋ opens a
/// one-line capture sheet. Embedded on the Today tab.
struct BlockersCard: View {
    @State private var blockers: [Blocker] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var showAdd = false

    var body: some View {
        Card {
            HStack(spacing: 8) {
                CardLabel(text: "Waiting on you")
                Spacer()
                Button { showAdd = true } label: {
                    Image(systemName: "plus.circle").font(.subheadline)
                }
                .buttonStyle(.borderless)
                .tint(Brand.teal)
                .accessibilityLabel("Add someone waiting on you")
            }
            if let error { Text(error).font(.caption).foregroundStyle(Brand.danger) }
            if blockers.isEmpty {
                Text(loaded ? "No one's blocked on you. 🎉" : "…")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(Array(blockers.enumerated()), id: \.element.id) { idx, b in
                    if idx > 0 { Divider().overlay(Brand.line) }
                    row(b)
                }
            }
        }
        .task { if !loaded { await load() } }
        .sheet(isPresented: $showAdd) { AddBlockerSheet { await load() } }
    }

    private func row(_ b: Blocker) -> some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                (Text(b.person).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                    + Text(" — \(b.what)").font(.subheadline).foregroundStyle(Brand.fg))
                HStack(spacing: 6) {
                    Text(b.waitingDays == 0 ? "waiting today" : "waiting \(b.waitingDays)d")
                        .font(.caption2).foregroundStyle(Brand.muted)
                    if let d = b.deadline, !PFDate.dayTime(d).isEmpty {
                        Text("· needs by \(PFDate.dayTime(d))").font(.caption2).foregroundStyle(Brand.muted)
                    }
                    if let chip = priorityChip(b.priority) {
                        Text(chip.text).font(.caption2.weight(.semibold)).foregroundStyle(chip.color)
                    }
                }
            }
            Spacer(minLength: 8)
            AsyncButton {
                try await withAPI { try await $0.resolveBlocker(b.id) }
                await load()
            } label: {
                Text("Delivered").font(.caption)
            } onError: { error = $0 }
            .buttonStyle(.bordered).tint(Brand.good)
        }
    }

    private func priorityChip(_ priority: Int?) -> (text: String, color: Color)? {
        switch priority {
        case 3: return ("urgent", Brand.danger)
        case 2: return ("high", Brand.warn)
        default: return nil
        }
    }

    private func load() async {
        do {
            blockers = try await withAPI { try await $0.blockers() }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// One-line capture: who's waiting on you, and for what. Posts to `POST /blockers`
/// (queueable, so a capture off-tailnet is stored and syncs later).
struct AddBlockerSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var person = ""
    @State private var what = ""
    @State private var priority = 1
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Who?", text: $person)
                    TextField("Waiting on…", text: $what, axis: .vertical).lineLimit(1...4)
                    Picker("Priority", selection: $priority) {
                        Text("low").tag(0)
                        Text("normal").tag(1)
                        Text("high").tag(2)
                        Text("urgent").tag(3)
                    }
                } footer: {
                    Text("Log when someone else is blocked until you do a thing — it weighs into panic mode and the morning briefing so an unblock can outrank a shiny new task.")
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Waiting on you")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { Task { await save() } }
                        .disabled(!canSave || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private var canSave: Bool {
        !person.trimmingCharacters(in: .whitespaces).isEmpty
            && !what.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private func save() async {
        saving = true; defer { saving = false }
        do {
            try await withAPI {
                try await $0.addBlocker(
                    person: person.trimmingCharacters(in: .whitespaces),
                    what: what.trimmingCharacters(in: .whitespaces),
                    priority: priority
                )
            }
            await onSaved()
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
