import SwiftUI

/// **Trips** — the closed-loop trip log (mirrors the web `/trips/board`). A trip
/// opens when you leave home and closes when you return; the system then asks you
/// to name it. Shows the open trip (if you're out), the completed trips still
/// awaiting a label (the ask), and recent history — plus a link to the
/// focus-balance rollup those trips feed. Reads `GET /trips`; labeling posts
/// `/webhooks/trip/retro`. Reached from the Me tab.
struct TripsView: View {
    @State private var snapshot: TripsSnapshot?
    @State private var balanceSummary: String?
    @State private var error: String?
    @State private var loaded = false
    @State private var labeling: Trip?

    /// Recent, completed, already-labeled trips — the history, with the active and
    /// still-unlabeled trips (shown in their own sections) filtered out.
    private var history: [Trip] {
        (snapshot?.recent ?? []).filter { $0.status == "completed" && $0.isLabeled }
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                if let error { ErrorBanner(message: error) }
                if let s = snapshot {
                    if let active = s.active { activeCard(active) }
                    if !s.unlabeled.isEmpty { unlabeledCard(s.unlabeled) }
                    historyCard
                    balanceLink
                } else if !loaded {
                    ProgressView().padding(.top, 40)
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Trips")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(item: $labeling, onDismiss: { Task { await load() } }) { trip in
            TripLabelSheet(trip: trip,
                           categories: snapshot?.categories ?? [],
                           domains: snapshot?.domains ?? [])
        }
    }

    // MARK: active

    private func activeCard(_ trip: Trip) -> some View {
        Card {
            CardLabel(text: "Out now")
            HStack(alignment: .firstTextBaseline) {
                Text(trip.elapsedMinutes.map { "\(Int($0)) min out" } ?? "On a trip")
                    .font(.title3.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                Spacer()
                if let d = trip.distanceLabel { Chip(text: d) }
            }
            if let since = trip.departedAt, !PFDate.time(since).isEmpty {
                Text("Left home at \(PFDate.time(since))").font(.caption).foregroundStyle(Brand.muted)
            }
            Text("It'll close when you get home, then you can label it.")
                .font(.caption).foregroundStyle(Brand.muted)
        }
    }

    // MARK: unlabeled (the ask)

    private func unlabeledCard(_ trips: [Trip]) -> some View {
        Card {
            CardLabel(text: "Needs a label")
            Text("Name these so they feed your focus balance.")
                .font(.caption).foregroundStyle(Brand.muted)
            ForEach(trips) { trip in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(whenLabel(trip)).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                        Spacer(minLength: 4)
                        Button("Label") { labeling = trip }
                            .font(.caption.weight(.semibold)).buttonStyle(.bordered).tint(Brand.accent)
                    }
                    FlowRow(spacing: 6) {
                        if let m = trip.durationMinutes { Chip(text: Trip.minutesPhrase(m)) }
                        if let d = trip.distanceLabel { Chip(text: d) }
                        if let place = trip.suggestion?.place, !place.isEmpty {
                            Chip(text: "looks like \(place)", color: Brand.fyi)
                        }
                    }
                }
                .padding(.vertical, 2)
                if trip.id != trips.last?.id { Divider().overlay(Brand.line) }
            }
        }
    }

    // MARK: recent history

    private var historyCard: some View {
        Card {
            CardLabel(text: "Recent trips")
            if history.isEmpty {
                Text("No labeled trips yet.").font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(history) { trip in
                    TripRow(trip: trip, domains: snapshot?.domains ?? [], reload: load,
                            onError: { error = $0 })
                    if trip.id != history.last?.id { Divider().overlay(Brand.line) }
                }
            }
        }
    }

    // MARK: balance link

    @ViewBuilder
    private var balanceLink: some View {
        if let summary = balanceSummary, !summary.isEmpty {
            NavigationLink { InsightsView() } label: {
                Card {
                    HStack(spacing: 10) {
                        Image(systemName: "chart.pie").foregroundStyle(Brand.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            CardLabel(text: "Focus balance")
                            Text(summary).font(.caption).foregroundStyle(Brand.muted)
                        }
                        Spacer(minLength: 4)
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(Brand.muted)
                    }
                }
            }
            .buttonStyle(.plain)
        }
    }

    private func whenLabel(_ trip: Trip) -> String {
        let d = PFDate.dayTime(trip.departedAt)
        return d.isEmpty ? "A recent trip" : d
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let t = client.trips()
            async let b = client.focusBalance(days: 7)
            snapshot = try await t
            // Balance is a nicety here (full chart lives in Insights); best-effort.
            balanceSummary = (try? await b)?.summary
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// One completed, labeled trip in the history — with a long-press to re-file its
/// life-domain (the focus-balance edit path).
struct TripRow: View {
    let trip: Trip
    let domains: [String]
    let reload: () async -> Void
    let onError: (String) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if let o = trip.reflectionOutcome, !o.isEmpty {
                Text(outcomeIcon(o)).accessibilityLabel("Outcome: \(o)")
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(trip.label ?? "Trip").font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                FlowRow(spacing: 6) {
                    Text(PFDate.dayTime(trip.departedAt)).font(.caption2).foregroundStyle(Brand.muted)
                    if let m = trip.durationMinutes { Chip(text: Trip.minutesPhrase(m)) }
                    if let d = trip.distanceLabel { Chip(text: d) }
                    if let dom = trip.domain, !dom.isEmpty { DomainPill(text: dom) }
                    if let cat = trip.category, !cat.isEmpty, cat != trip.domain { Chip(text: cat) }
                }
                if let note = trip.reflection, !note.isEmpty {
                    Text(note).font(.caption).foregroundStyle(Brand.muted).lineLimit(2)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .contextMenu { domainMenu }
    }

    @ViewBuilder
    private var domainMenu: some View {
        Text("File under…")
        ForEach(domains, id: \.self) { d in
            Button {
                Task { await setDomain(d) }
            } label: {
                if trip.domain == d { Label(d.capitalized, systemImage: "checkmark") } else { Text(d.capitalized) }
            }
        }
        if !(trip.domain ?? "").isEmpty {
            Divider()
            Button(role: .destructive) { Task { await setDomain(nil) } } label: {
                Label("Clear life area", systemImage: "xmark")
            }
        }
    }

    private func outcomeIcon(_ outcome: String) -> String {
        switch outcome {
        case "success": return "✅"
        case "partial": return "🟡"
        case "miss": return "🔴"
        default: return "•"
        }
    }

    private func setDomain(_ domain: String?) async {
        do {
            try await withAPI { try await $0.setTripDomain(trip.id, domain: domain) }
            await reload()
        } catch {
            onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }
}

/// Name a completed trip: a label, an optional category + life-domain (for the
/// focus-balance rollup), and an optional "how it went" note that feeds learning.
/// Posts everything in one `/webhooks/trip/retro` call.
struct TripLabelSheet: View {
    let trip: Trip
    let categories: [String]
    let domains: [String]
    @Environment(\.dismiss) private var dismiss

    @State private var label: String
    @State private var category: String
    @State private var domain: String
    @State private var reflection = ""
    @State private var error: String?
    @State private var saving = false

    init(trip: Trip, categories: [String], domains: [String]) {
        self.trip = trip
        self.categories = categories
        self.domains = domains
        _label = State(initialValue: trip.suggestion?.label ?? trip.label ?? "")
        _category = State(initialValue: trip.category ?? "")
        _domain = State(initialValue: trip.suggestion?.domain ?? trip.domain ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Label (e.g. Target run)", text: $label)
                    Picker("Category", selection: $category) {
                        Text("None").tag("")
                        ForEach(categories, id: \.self) { Text($0.capitalized).tag($0) }
                    }
                    Picker("Life area", selection: $domain) {
                        Text("None").tag("")
                        ForEach(domains, id: \.self) { Text($0.capitalized).tag($0) }
                    }
                } header: {
                    Text("What was this trip?")
                } footer: {
                    if let place = trip.suggestion?.place, !place.isEmpty {
                        Text("A stop matched your saved place \u{201C}\(place)\u{201D}.")
                    }
                }
                Section("How did it go? (optional)") {
                    TextField("A word on how it went — feeds your learning.",
                              text: $reflection, axis: .vertical)
                        .lineLimit(1...4)
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Label trip")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }
                        .disabled(label.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        let note = reflection.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            try await withAPI {
                try await $0.tripRetro(tripId: trip.id,
                                       label: label.trimmingCharacters(in: .whitespaces),
                                       category: category.isEmpty ? nil : category,
                                       domain: domain.isEmpty ? nil : domain,
                                       reflection: note.isEmpty ? nil : note)
            }
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
