import SwiftUI

struct CalendarView: View {
    @State private var commitments: [Commitment] = []
    @State private var slots: Slots?
    @State private var slotMinutes = 30
    @State private var error: String?
    @State private var loaded = false

    private let slotOptions = [15, 30, 60, 90]

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
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
            commitments = try await withAPI { try await $0.commitments(limit: 60) }
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
