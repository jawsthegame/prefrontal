import SwiftUI

struct MeView: View {
    @State private var selfCare: SelfCare?
    @State private var review: SelfCareReview?
    @State private var error: String?
    @State private var loaded = false
    @State private var showFocus = false
    @State private var showOuting = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                selfCareCard
                if let r = review, r.enabled, r.hasContent { reviewCard(r) }
                actionsCard
                householdLink
                insightsLink
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
                Text("Tap to log").font(.caption2).foregroundStyle(Brand.muted)
                ForEach(sc.checks.filter { $0.enabled }) { check in
                    AsyncButton {
                        // Mirror the web dashboard: a quota check that's reached its
                        // target wraps back to zero on the next tap (touch has no
                        // shift-click to rewind a mis-tap), otherwise log one. An
                        // open-ended check (bio breaks) never wraps — a tap always
                        // just logs one.
                        let atMax = !check.openEnded && check.target > 0 && check.count >= check.target
                        try await withAPI { try await $0.markSelfCare(key: check.key, reset: atMax) }
                        await load()
                    } label: {
                        ProgressChip(icon: icon(check.key), label: selfCareLabel(check.key),
                                     count: check.count, target: check.target,
                                     satisfied: check.satisfied, overdue: check.overdue)
                    } onError: { error = $0 }
                    .buttonStyle(.plain)
                }
            } else if selfCare != nil {
                Text("Self-care checks are off. Enable them in the web settings.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                Text("…").foregroundStyle(Brand.muted)
            }
        }
    }

    /// End-of-day self-care recap: the gaps a raw tally hides, plus what went
    /// well. Reads today's confirm timeline back from `/self-care/review`.
    private func reviewCard(_ r: SelfCareReview) -> some View {
        Card {
            HStack {
                CardLabel(text: "Day review")
                Spacer()
                if let d = r.date { Text(d).font(.caption2).foregroundStyle(Brand.muted) }
            }
            if r.gaps.isEmpty {
                Label("No gaps today — nicely spaced.", systemImage: "checkmark.seal")
                    .font(.subheadline).foregroundStyle(Brand.good)
            } else {
                Text("Gaps to notice").font(.footnote).foregroundStyle(Brand.muted)
                ForEach(Array(r.gaps.enumerated()), id: \.offset) { _, gap in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(Brand.warn).frame(width: 7, height: 7).padding(.top, 6)
                        Text(gap).font(.subheadline).foregroundStyle(Brand.nearWhite)
                    }
                }
            }
            if !r.wins.isEmpty {
                Divider().overlay(Brand.line)
                Text("On track").font(.caption).foregroundStyle(Brand.muted)
                FlowRow(spacing: 6, lineSpacing: 6) {
                    ForEach(Array(r.wins.enumerated()), id: \.offset) { _, win in
                        Chip(text: win, color: Brand.good)
                    }
                }
            }
        }
    }

    /// Always-present entry to the shared Household screen — the durable way in
    /// (the Today glance only shows for members, so a user in no household would
    /// otherwise have no path to the create/join screen).
    private var householdLink: some View {
        NavigationLink {
            HouseholdView()
        } label: {
            Card {
                HStack(spacing: 12) {
                    Image(systemName: "house").foregroundStyle(Brand.accent)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Household").font(.subheadline.weight(.semibold))
                            .foregroundStyle(Brand.nearWhite)
                        Text("Shared chores, shopping, kids' details, and star charts")
                            .font(.caption).foregroundStyle(Brand.muted)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(Brand.muted)
                }
            }
        }
        .buttonStyle(.plain)
    }

    /// Navigates to the behavioral Insights screen (stats + focus balance).
    private var insightsLink: some View {
        NavigationLink {
            InsightsView()
        } label: {
            Card {
                HStack(spacing: 12) {
                    Image(systemName: "chart.bar.xaxis").foregroundStyle(Brand.accent)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Insights").font(.subheadline.weight(.semibold))
                            .foregroundStyle(Brand.nearWhite)
                        Text("Estimates, follow-through, and balance over time")
                            .font(.caption).foregroundStyle(Brand.muted)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(Brand.muted)
                }
            }
        }
        .buttonStyle(.plain)
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

    private func icon(_ key: String) -> String {
        ["meal": "🍽️", "water": "💧", "meds": "💊", "biobreak": "🚻",
         "winddown": "🌙", "movement": "🚶"][key] ?? "•"
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let care = client.selfCare()
            async let rev = client.selfCareReview()
            selfCare = try await care
            // Best-effort: the review is a nicety; a failure there shouldn't blank
            // the self-care card the tab is built around.
            review = try? await rev
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
