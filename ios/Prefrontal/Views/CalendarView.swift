import SwiftUI

struct CalendarView: View {
    @State private var commitments: [Commitment] = []
    @State private var previous: [Commitment] = []
    @State private var slots: Slots?
    @State private var slotMinutes = 30
    @State private var error: String?
    @State private var loaded = false

    private let slotOptions = [15, 30, 60, 90]

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
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
    }

    // MARK: - Recently elapsed → made it / missed it

    private var outcomeCard: some View {
        Card {
            CardLabel(text: "Did you make it?")
            Text("A quick, honest check on what just passed — it teaches Prefrontal your real follow-through.")
                .font(.caption).foregroundStyle(Brand.muted)
            ForEach(previous) { c in outcomeRow(c) }
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
            if c.id != previous.last?.id { Divider().overlay(Brand.line) }
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
            let payload = try await withAPI { try await $0.commitmentList(limit: 60) }
            commitments = payload.commitments
            previous = payload.previous ?? []
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
