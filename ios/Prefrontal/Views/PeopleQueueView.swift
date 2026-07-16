import SwiftUI

/// The name-mention review queue (`GET /people/queue`) — names pulled from
/// ingested items (mail / calendar) that aren't on the roster yet. Identify one
/// (create + categorize a new person) or dismiss it; the resulting roster feeds
/// the behavioral profile (learning) and todo prioritization. Server:
/// `prefrontal/people.py`.
///
/// v1 covers the queue workflow (identify-as-new + dismiss). Linking a mention to
/// an *existing* roster person, and browsing/editing the roster itself, are a
/// deliberate follow-up.
struct PeopleQueueView: View {
    @State private var mentions: [PersonMention] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var identifying: PersonMention?

    var body: some View {
        ScrollView {
            // Lazy so off-screen mention cards aren't built when the queue is long.
            LazyVStack(alignment: .leading, spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if mentions.isEmpty && loaded && error == nil {
                    emptyState
                } else {
                    ForEach(mentions) { mentionCard($0) }
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("People to identify")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(item: $identifying) { m in
            IdentifySheet(name: m.name) { relationship, importance in
                try await withAPI {
                    try await $0.identifyMention(m.id, relationship: relationship, importance: importance)
                }
                await load()
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "person.2").font(.largeTitle).foregroundStyle(Brand.muted)
            Text("No names to review").font(.headline).foregroundStyle(Brand.nearWhite)
            Text("When your mail or calendar names someone new, they'll show up here to identify.")
                .font(.footnote).foregroundStyle(Brand.muted).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.top, 60)
    }

    private func mentionCard(_ m: PersonMention) -> some View {
        Card {
            Text(m.name).font(.title3.weight(.semibold)).foregroundStyle(Brand.nearWhite)
            if let sub = subtitle(m) {
                Text(sub).font(.footnote).foregroundStyle(Brand.muted).lineLimit(2)
            }
            HStack(spacing: 10) {
                Button { identifying = m } label: {
                    Label("Identify", systemImage: "person.crop.circle.badge.checkmark")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(Brand.teal)
                AsyncButton {
                    try await withAPI { try await $0.dismissMention(m.id) }
                    await load()
                } label: {
                    Label("Dismiss", systemImage: "xmark").frame(maxWidth: .infinity)
                } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.muted)
            }
            .padding(.top, 4)
        }
    }

    // "from mail — “…meeting with Sarah…”" — where the name came from plus the
    // snippet, when the server captured them.
    private func subtitle(_ m: PersonMention) -> String? {
        var parts: [String] = []
        if let s = m.source, !s.isEmpty { parts.append("from \(s)") }
        if let c = m.context, !c.isEmpty { parts.append("“\(c)”") }
        return parts.isEmpty ? nil : parts.joined(separator: " — ")
    }

    private func load() async {
        do { mentions = try await withAPI { try await $0.peopleQueue() }; error = nil }
        catch { self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription }
        loaded = true
    }
}

/// Categorize a queued name into a new roster person: pick a relationship and how
/// much they matter (the shared 0–3 priority scale). Mirrors the server's
/// `RELATIONSHIPS` / importance vocab (`prefrontal/people.py`).
struct IdentifySheet: View {
    let name: String
    let onIdentify: (_ relationship: String, _ importance: Int) async throws -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var relationship = "unknown"
    @State private var importance = 1
    @State private var error: String?
    @State private var saving = false

    // Mirror of prefrontal.people.RELATIONSHIPS (kept in step by hand — a small,
    // rarely-changing vocab; the server 422s an unknown value as a backstop).
    private static let relationships = ["family", "coworker", "friend", "professional",
                                        "service", "acquaintance", "other", "unknown"]
    // Importance 0–3, the same scale as todo/blocker priority (0 low · 1 normal ·
    // 2 high · 3 top).
    private static let importanceLabels = ["Low", "Normal", "High", "Top"]

    var body: some View {
        NavigationStack {
            Form {
                Section("Who is \(name)?") {
                    Picker("Relationship", selection: $relationship) {
                        ForEach(Self.relationships, id: \.self) { Text($0.capitalized).tag($0) }
                    }
                    Picker("How much they matter", selection: $importance) {
                        ForEach(0..<Self.importanceLabels.count, id: \.self) { i in
                            Text(Self.importanceLabels[i]).tag(i)
                        }
                    }
                    .pickerStyle(.segmented)
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Add \(name)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    // Flip `saving` synchronously here (not inside the async Task) so a
                    // fast double-tap can't enqueue a second identify before it disables.
                    Button("Add") {
                        guard !saving else { return }
                        saving = true
                        Task { await save() }
                    }
                    .disabled(saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    // `saving` is set true by the Add button (synchronously) before this runs; we
    // only clear it here.
    private func save() async {
        defer { saving = false }
        do { try await onIdentify(relationship, importance); dismiss() }
        catch { self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription }
    }
}
