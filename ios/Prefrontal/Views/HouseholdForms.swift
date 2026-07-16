import SwiftUI

// The add/edit sheets presented over the Household tab: a roster add (child or
// pet), a kid appointment, a co-parent invite, a star-award sheet, and the chore
// editor (add/edit). Each mirrors the StartSheet idiom (Form in a
// NavigationStack, Cancel/confirm toolbar) and dismisses on success; the parent
// reloads on dismiss.

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

// MARK: - Add / edit chore

/// Create or edit a recurring shared chore from the phone. `POST /household/chores`
/// upserts on title within the household, so in **edit** mode the title is fixed
/// (changing it would fork a new chore) and only the schedule / owner / impact /
/// enabled state are editable. A chore can inherit a routine's schedule (owner
/// still applies) or carry its own weekdays + due time; a chore with no time is an
/// untimed checklist item (no reminder). Advanced fields (reminder lead time,
/// away-behavior, day-of-month, municipal service) keep their server defaults —
/// tune those on the web dashboard.
struct ChoreEditorSheet: View {
    let mode: ChoreEditor
    let members: [HouseholdMember]
    let routines: [Routine]
    @Environment(\.dismiss) private var dismiss

    @State private var title: String
    @State private var ownerId: Int?
    @State private var routineId: Int?
    @State private var days: Set<Int>
    @State private var hasTime: Bool
    @State private var time: Date
    @State private var impact: String
    @State private var enabled: Bool
    @State private var error: String?
    @State private var saving = false

    /// Seed `@State` from `mode` at construction rather than in `onAppear`, so the
    /// fields are correct on first render and can't carry stale values across
    /// presentations — `.add` gets clean defaults, `.edit` gets the chore's values.
    init(mode: ChoreEditor, members: [HouseholdMember], routines: [Routine]) {
        self.mode = mode
        self.members = members
        self.routines = routines
        if case let .edit(chore) = mode {
            let due = chore.effectiveDueTime ?? ""
            _title = State(initialValue: chore.title)
            _ownerId = State(initialValue: chore.ownerId)
            _routineId = State(initialValue: chore.routineId)
            _days = State(initialValue: Set(chore.weekdays))
            _hasTime = State(initialValue: !due.isEmpty)
            _time = State(initialValue: AvailableHours.date(from: due.isEmpty ? "09:00" : due))
            _impact = State(initialValue: chore.impact ?? "")
            _enabled = State(initialValue: chore.isEnabled)
        } else {
            _title = State(initialValue: "")
            _ownerId = State(initialValue: nil)
            _routineId = State(initialValue: nil)
            _days = State(initialValue: [])
            _hasTime = State(initialValue: false)
            _time = State(initialValue: AvailableHours.date(from: "09:00"))
            _impact = State(initialValue: "")
            _enabled = State(initialValue: true)
        }
    }

    private static let weekdayLabels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    private var isEdit: Bool { if case .edit = mode { return true } else { return false } }
    /// A routine-linked chore inherits the routine's whole schedule, so the
    /// per-chore weekday / time controls hide.
    private var inheritsRoutine: Bool { routineId != nil }

    var body: some View {
        NavigationStack {
            Form {
                Section("Chore") {
                    if isEdit {
                        LabeledContent("What", value: title)
                    } else {
                        TextField("What has to happen (e.g. run the dishwasher)", text: $title)
                    }
                    Picker("Whose job", selection: $ownerId) {
                        Text("Either parent").tag(Int?.none)
                        ForEach(members) { m in Text(m.name).tag(Optional(m.id)) }
                    }
                }

                if !routines.isEmpty {
                    Section("Routine") {
                        Picker("Part of", selection: $routineId) {
                            Text("Stands alone").tag(Int?.none)
                            ForEach(routines) { r in Text(r.title).tag(Optional(r.id)) }
                        }
                        if inheritsRoutine {
                            Text("Inherits the routine's schedule.")
                                .font(.caption).foregroundStyle(Brand.muted)
                        }
                    }
                }

                if !inheritsRoutine { scheduleSection }

                Section {
                    TextField("Why it matters if it slips (optional)", text: $impact, axis: .vertical)
                        .lineLimit(1...3)
                    Toggle("Reminders on", isOn: $enabled)
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle(isEdit ? "Edit chore" : "Add chore")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }
                        .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
    }

    private var scheduleSection: some View {
        Section("Schedule") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Days — none selected means every day")
                    .font(.caption).foregroundStyle(Brand.muted)
                FlowRow(spacing: 6, lineSpacing: 6) {
                    ForEach(Array(Self.weekdayLabels.enumerated()), id: \.offset) { idx, label in
                        let on = days.contains(idx)
                        Button {
                            if on { days.remove(idx) } else { days.insert(idx) }
                        } label: {
                            Text(label)
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 10).padding(.vertical, 5)
                                .background(on ? Brand.accent : Brand.chip, in: Capsule())
                                .foregroundStyle(on ? Brand.accentFg : Brand.muted)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            Toggle("Has a due time", isOn: $hasTime.animation())
            if hasTime {
                DatePicker("Due by", selection: $time, displayedComponents: .hourAndMinute)
            } else {
                Text("Untimed — a checklist chore, no reminder.")
                    .font(.caption).foregroundStyle(Brand.muted)
            }
        }
    }

    private func save() async {
        saving = true; defer { saving = false }
        let sendDays = inheritsRoutine ? [] : days.sorted()
        let dueTime = (!inheritsRoutine && hasTime) ? AvailableHours.hhmm(from: time) : ""
        let trimmedImpact = impact.trimmingCharacters(in: .whitespaces)
        do {
            try await withAPI {
                try await $0.setChore(
                    title: title.trimmingCharacters(in: .whitespaces),
                    ownerId: ownerId, routineId: routineId,
                    days: sendDays, dueTime: dueTime,
                    impact: trimmedImpact.isEmpty ? nil : trimmedImpact,
                    enabled: enabled
                )
            }
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
