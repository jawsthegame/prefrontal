import SwiftUI

// The add/edit sheets presented over the Household tab: a roster add (child or
// pet), a kid appointment, a co-parent invite, and a star-award sheet. Each
// mirrors the StartSheet idiom (Form in a NavigationStack, Cancel/confirm
// toolbar) and dismisses on success; the parent reloads on dismiss.

// MARK: - Add child / pet

struct AddRosterSheet: View {
    enum Kind { case child, pet }
    let kind: Kind
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var species = ""
    @State private var hasBirthday = false
    @State private var birthday = Date()
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section(kind == .child ? "Child" : "Pet") {
                    TextField("Name", text: $name)
                    if kind == .pet {
                        TextField("Species (e.g. dog)", text: $species)
                    }
                    Toggle("Birthday", isOn: $hasBirthday.animation())
                    if hasBirthday {
                        DatePicker("Date", selection: $birthday, displayedComponents: .date)
                    }
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle(kind == .child ? "Add child" : "Add pet")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { Task { await save() } }
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        let n = name.trimmingCharacters(in: .whitespaces)
        let bday = hasBirthday ? isoDay(birthday) : nil
        do {
            try await withAPI { client in
                switch kind {
                case .child: try await client.addChild(name: n, birthday: bday)
                case .pet:
                    let sp = species.trimmingCharacters(in: .whitespaces)
                    try await client.addPet(name: n, species: sp.isEmpty ? nil : sp, birthday: bday)
                }
            }
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// "yyyy-MM-dd" in the phone's local zone (the server stores a bare ISO date).
    private func isoDay(_ date: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}

// MARK: - Add appointment

struct AddAppointmentSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var start = Date()
    @State private var location = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Appointment") {
                    TextField("What (e.g. Sam dentist)", text: $title)
                    DatePicker("When", selection: $start)
                    TextField("Location (optional)", text: $location)
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Add appointment")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { Task { await save() } }
                        .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        let loc = location.trimmingCharacters(in: .whitespaces)
        do {
            try await withAPI {
                try await $0.addAppointment(title: title.trimmingCharacters(in: .whitespaces),
                                            startAtISO: iso(start),
                                            location: loc.isEmpty ? nil : loc)
            }
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// Offset-aware ISO-8601 instant; the server's `to_utc` reads the embedded
    /// offset, so the picked wall-clock time lands correctly.
    private func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }
}

// MARK: - Invite a co-parent

struct InviteSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var minted: InviteMinted?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let minted {
                    Card {
                        CardLabel(text: "Share this code")
                        Text(minted.code)
                            .font(.system(.title, design: .monospaced).weight(.bold))
                            .foregroundStyle(Brand.accent)
                            .textSelection(.enabled)
                        Text("Your co-parent enters it under Join with a code (it expires in 7 days).")
                            .font(.caption).foregroundStyle(Brand.muted)
                        ShareLink(item: shareText(minted)) {
                            Label("Share invite", systemImage: "square.and.arrow.up")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent).tint(Brand.accent)
                    }
                } else {
                    Card {
                        Text("Add your co-parent so you both see — and keep up — the same sheet.")
                            .font(.footnote).foregroundStyle(Brand.muted)
                        AsyncButton {
                            minted = try await withAPI { try await $0.createInvite() }
                        } label: {
                            Label("Create an invite code", systemImage: "person.badge.plus")
                                .frame(maxWidth: .infinity)
                        } onError: { error = $0 }
                        .buttonStyle(.borderedProminent).tint(Brand.accent)
                    }
                }
                Spacer()
            }
            .padding(16)
            .brandScreen()
            .navigationTitle("Invite a co-parent")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents([.medium])
    }

    private func shareText(_ minted: InviteMinted) -> String {
        if let url = minted.joinUrl, !url.isEmpty {
            return "Join our Prefrontal household — code \(minted.code): \(url)"
        }
        return "Join our Prefrontal household with invite code \(minted.code)"
    }
}

// MARK: - Award stars

struct AwardStarsSheet: View {
    let agreement: Agreement
    @Environment(\.dismiss) private var dismiss
    @State private var delta = 1
    @State private var note = ""
    @State private var error: String?
    @State private var saving = false
    @State private var reached: [StarGoal] = []

    var body: some View {
        NavigationStack {
            Form {
                Section(agreement.title) {
                    Stepper("Add \(delta) ⭐️", value: $delta, in: 1...10)
                    TextField("What for (optional)", text: $note)
                }
                if !reached.isEmpty {
                    Section("Reward reached!") {
                        ForEach(reached, id: \.count) { g in
                            Label("\(g.count) → \(g.reward)", systemImage: "gift")
                                .foregroundStyle(Brand.good)
                        }
                    }
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Award stars")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(reached.isEmpty ? "Award" : "Done") {
                        if reached.isEmpty { Task { await save() } } else { dismiss() }
                    }
                    .disabled(saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        let n = note.trimmingCharacters(in: .whitespaces)
        do {
            let result = try await withAPI {
                try await $0.awardStars(agreement.id, delta: delta, note: n.isEmpty ? nil : n)
            }
            reached = result.goalsReached ?? []
            // No new goal crossed → nothing more to show; just close.
            if reached.isEmpty { dismiss() }
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
