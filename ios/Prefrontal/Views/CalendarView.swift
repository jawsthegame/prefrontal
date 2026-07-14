import SwiftUI

struct CalendarView: View {
    @State private var commitments: [Commitment] = []
    @State private var previous: [Commitment] = []
    @State private var conflicts: ConflictList?
    @State private var rescheduleFor: Conflict?
    @State private var slots: Slots?
    @State private var slotMinutes = 30
    @State private var error: String?
    @State private var loaded = false

    private let slotOptions = [15, 30, 60, 90]

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let cl = conflicts, !cl.conflicts.isEmpty || !cl.possibleConflicts.isEmpty {
                    conflictsCard(cl)
                }
                if !previous.isEmpty { outcomeCard }
                slotFinder
                ForEach(groupedByDay, id: \.day) { group in
                    Card {
                        CardLabel(text: group.day)
                        ForEach(group.items) { c in eventRow(c) }
                    }
                }
                if commitments.isEmpty && loaded && error == nil {
                    Text("Nothing on the calendar.").foregroundStyle(Brand.muted).padding(.top, 40)
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Calendar")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(item: $rescheduleFor) { RescheduleSheet(conflict: $0, onDone: load) }
    }

    // MARK: - Schedule conflicts (double-bookings)

    private func conflictsCard(_ cl: ConflictList) -> some View {
        Card {
            CardLabel(text: "Schedule conflicts")
            ForEach(Array(cl.conflicts.enumerated()), id: \.element.id) { idx, c in
                if idx > 0 { Divider().overlay(Brand.line) }
                conflictRow(c, firm: true)
            }
            ForEach(Array(cl.possibleConflicts.enumerated()), id: \.element.id) { idx, c in
                if idx > 0 || !cl.conflicts.isEmpty { Divider().overlay(Brand.line) }
                conflictRow(c, firm: false)
            }
        }
    }

    private func conflictRow(_ c: Conflict, firm: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Chip(text: firm ? "double-booked" : "possible", color: firm ? Brand.danger : Brand.warn)
                Spacer(minLength: 0)
                if let m = c.overlapMinutes { Text("overlap \(Int(m))m").font(.caption2).foregroundStyle(Brand.muted) }
            }
            Text("\(cleanTitle(c.a.title)) ↔ \(cleanTitle(c.b.title))")
                .font(.subheadline).foregroundStyle(Brand.nearWhite)
            HStack(spacing: 8) {
                if firm {
                    Button { rescheduleFor = c } label: {
                        Label("Reschedule", systemImage: "envelope").font(.caption).lineLimit(1)
                    }
                    .buttonStyle(.bordered).tint(Brand.blue)
                }
                AsyncButton {
                    try await withAPI { try await $0.dismissConflict(key: c.key) }
                    await load()
                } label: { Text("Dismiss").font(.caption) } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.muted)
            }
        }
    }

    private func cleanTitle(_ s: String) -> String { s.replacingOccurrences(of: "\\", with: "") }

    // MARK: - Recently elapsed → made it / missed it

    private var outcomeCard: some View {
        Card {
            CardLabel(text: "Did you make it?")
            Text("A quick, honest check on what just passed — it teaches Prefrontal your real follow-through. Swipe left to hide an FYI you never had to go to.")
                .font(.caption).foregroundStyle(Brand.muted)
            ForEach(previous) { c in
                SwipeToReveal(label: "Hide") { await hide(c) } content: {
                    outcomeRow(c)
                }
                if c.id != previous.last?.id { Divider().overlay(Brand.line) }
            }
        }
    }

    /// Drop a recently-elapsed item from the "Did you make it?" list without
    /// scoring it — for FYIs or events the user didn't need to attend. Hiding it
    /// on the server also keeps it out of upcoming reads and survives a re-sync.
    private func hide(_ c: Commitment) async {
        do {
            try await withAPI { try await $0.setCommitmentHidden(c.id) }
            await load()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    @ViewBuilder
    private func outcomeRow(_ c: Commitment) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(c.title.replacingOccurrences(of: "\\", with: ""))
                .font(.subheadline).foregroundStyle(Brand.nearWhite)
            Text(PFDate.dayTime(c.startAt)).font(.caption2).foregroundStyle(Brand.muted)

            if let outcome = c.outcome {
                HStack(spacing: 10) {
                    Chip(text: outcome == "made" ? "✓ Made it" : "✗ Missed",
                         color: outcome == "made" ? Brand.good : Brand.danger)
                    AsyncButton {
                        try await withAPI { try await $0.setCommitmentOutcome(c.id, outcome: nil) }
                        await load()
                    } label: { Text("Change").font(.caption) } onError: { error = $0 }
                    .buttonStyle(.borderless).tint(Brand.muted)
                    Spacer(minLength: 0)
                }
            } else {
                HStack(spacing: 10) {
                    AsyncButton {
                        try await withAPI { try await $0.setCommitmentOutcome(c.id, outcome: "made") }
                        await load()
                    } label: {
                        Label("Made it", systemImage: "checkmark").frame(maxWidth: .infinity)
                    } onError: { error = $0 }
                    .buttonStyle(.bordered).tint(Brand.good)

                    AsyncButton {
                        try await withAPI { try await $0.setCommitmentOutcome(c.id, outcome: "missed") }
                        await load()
                    } label: {
                        Label("Missed it", systemImage: "xmark").frame(maxWidth: .infinity)
                    } onError: { error = $0 }
                    .buttonStyle(.bordered).tint(Brand.danger)
                }
            }
        }
    }

    private var slotFinder: some View {
        Card {
            CardLabel(text: "Find a free slot")
            Picker("Minutes", selection: $slotMinutes) {
                ForEach(slotOptions, id: \.self) { Text("\($0) min").tag($0) }
            }
            .pickerStyle(.segmented)
            .onChange(of: slotMinutes) { _, _ in Task { await loadSlots() } }

            if let slots, !slots.slots.isEmpty {
                ForEach(slots.slots.prefix(4)) { s in
                    HStack {
                        Text(s.day).font(.footnote.weight(.medium)).foregroundStyle(Brand.nearWhite)
                        Spacer()
                        Text("\(s.start) – \(s.end)").font(.caption).foregroundStyle(Brand.teal)
                    }
                }
            } else {
                Text("No open windows found.").font(.footnote).foregroundStyle(Brand.muted)
            }
        }
    }

    private func eventRow(_ c: Commitment) -> some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 2)
                .fill(c.hardness == "hard" ? Brand.blue : Brand.muted.opacity(0.5))
                .frame(width: 3, height: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(c.title.replacingOccurrences(of: "\\", with: ""))
                    .font(.subheadline).foregroundStyle(Brand.nearWhite)
                HStack(spacing: 6) {
                    Text("\(PFDate.time(c.startAt))–\(PFDate.time(c.endAt))")
                        .font(.caption).foregroundStyle(Brand.muted)
                    if let cal = c.calendar { Chip(text: cal) }
                }
                if let loc = c.location {
                    Text(loc.replacingOccurrences(of: "\\", with: "")).font(.caption2).foregroundStyle(Brand.muted).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private var groupedByDay: [(day: String, items: [Commitment])] {
        let f = DateFormatter(); f.setLocalizedDateFormatFromTemplate("EEEE MMM d")
        var order: [String] = []
        var map: [String: [Commitment]] = [:]
        for c in commitments.sorted(by: { ($0.startAt ?? "") < ($1.startAt ?? "") }) {
            guard let d = PFDate.parse(c.startAt) else { continue }
            let key = f.string(from: d)
            if map[key] == nil { order.append(key); map[key] = [] }
            map[key]?.append(c)
        }
        return order.map { ($0, map[$0] ?? []) }
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let payloadReq = client.commitmentList(limit: 60)
            async let conflictsReq = client.commitmentConflicts()
            let payload = try await payloadReq
            commitments = payload.commitments
            previous = payload.previous ?? []
            // Best-effort: conflicts are an add-on, not worth failing the calendar.
            conflicts = try? await conflictsReq
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        await loadSlots()
        loaded = true
    }

    private func loadSlots() async {
        slots = try? await withAPI { try await $0.slots(minutes: slotMinutes) }
    }
}

/// Resolve a double-booking by asking the other party to move one appointment.
/// Confirm-first: **Preview** drafts the note (`send: false`); **Send** emails it
/// over your SMTP (needs their email) and, on success, dismisses the conflict.
struct RescheduleSheet: View {
    let conflict: Conflict
    let onDone: () async -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var email = ""
    @State private var note = ""
    @State private var draft: RescheduleResult?
    @State private var error: String?
    @State private var busy = false

    private func clean(_ s: String) -> String { s.replacingOccurrences(of: "\\", with: "") }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("\(clean(conflict.a.title)) ↔ \(clean(conflict.b.title))")
                        .font(.subheadline).foregroundStyle(Brand.nearWhite)
                } header: { Text("Double-booked") }

                Section {
                    TextField("Their name (optional)", text: $name)
                    TextField("their@email.com", text: $email)
                        .keyboardType(.emailAddress).textInputAutocapitalization(.never)
                    TextField("Optional note…", text: $note, axis: .vertical).lineLimit(1...4)
                } header: {
                    Text("Ask the other party to move one")
                } footer: {
                    Text("Preview shows the drafted note. Sending needs their email and goes out over your own mail account.")
                }

                if let d = draft {
                    Section {
                        Text("Move \(clean(d.moved.title)) · keep \(clean(d.kept.title))")
                            .font(.caption).foregroundStyle(Brand.muted)
                        if let s = d.subject, !s.isEmpty {
                            Text(s).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                        }
                        if let b = d.body, !b.isEmpty {
                            Text(b).font(.footnote).foregroundStyle(Brand.fg)
                        }
                        if !d.slots.isEmpty {
                            Text("Offering: " + d.slots.joined(separator: " · "))
                                .font(.caption2).foregroundStyle(Brand.muted)
                        }
                    } header: { Text("Draft preview") }
                }

                if let error {
                    Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) }
                }

                Section {
                    Button { Task { await run(send: false) } } label: {
                        Label("Preview draft", systemImage: "eye")
                    }
                    .disabled(busy)
                    Button { Task { await run(send: true) } } label: {
                        Label("Send request", systemImage: "paperplane")
                    }
                    // Confirm-first: Send needs a current preview (and a recipient).
                    .disabled(busy || draft == nil || email.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            // Editing any field invalidates the shown draft, so Send re-locks until
            // you preview again — you never send a note you haven't just previewed.
            .onChange(of: name) { _, _ in draft = nil }
            .onChange(of: email) { _, _ in draft = nil }
            .onChange(of: note) { _, _ in draft = nil }
            .brandScreen()
            .navigationTitle("Reschedule")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func run(send: Bool) async {
        busy = true; defer { busy = false }
        let to = email.trimmingCharacters(in: .whitespaces)
        let nm = name.trimmingCharacters(in: .whitespaces)
        let nt = note.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let r = try await withAPI {
                try await $0.rescheduleConflict(key: conflict.key, to: to.isEmpty ? nil : to,
                                                recipientName: nm.isEmpty ? nil : nm,
                                                note: nt.isEmpty ? nil : nt, send: send)
            }
            draft = r
            if send && r.status == "forwarded" {
                await onDone()
                dismiss()
            } else if send {
                error = r.detail ?? "Couldn't send — the draft is kept above so you can send it yourself."
            } else {
                error = r.offline == true ? "Drafted offline (basic wording)." : nil
            }
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
