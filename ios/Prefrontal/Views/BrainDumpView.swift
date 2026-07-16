import SwiftUI

/// Brain-dump capture (roadmap M1, "capture at the speed of thought").
///
/// One rambling voice/text note is the lowest-friction capture there is. The user
/// types — or taps the keyboard's mic to dictate — a stream of thoughts, and one
/// tap turns it into a **previewable** list of edits (todos, commitments, shopping,
/// blockers) plus any **pending** behavioral proposals. Nothing is written until
/// the user reviews and applies, so a rambling, imperfect dump can never silently
/// mutate the store.
///
/// The parse runs **on-device** (Apple Foundation Models, iOS 26+) when available
/// — private and cheap, no cloud model sees the raw thought — and falls back to
/// the server's own parse otherwise. Either way the same `POST /braindump`
/// endpoint validates the result and returns the preview. A "server pass" button
/// escalates a dump to the cloud agent for the harder reasoning — a settings
/// change the on-device pass won't propose, or a subtler aside it didn't catch.
struct BrainDumpView: View {
    @Environment(\.dismiss) private var dismiss

    @State private var text = ""
    @State private var response: BrainDumpResponse?
    @State private var provider = ""            // "on_device" | "anthropic" | "ollama"
    @State private var error: String?
    @State private var working = false
    @State private var appliedNote: String?
    @State private var acceptedProposals: Set<Int> = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if let error { ErrorBanner(message: error) }
                    if response == nil { composer } else { review }
                }
                .padding(16)
            }
            .brandScreen()
            .navigationTitle("Brain-dump")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
    }

    // MARK: Compose

    private var composer: some View {
        VStack(spacing: 12) {
            Card {
                VStack(alignment: .leading, spacing: 8) {
                    CardLabel(text: "What's on your mind")
                    ZStack(alignment: .topLeading) {
                        if text.isEmpty {
                            Text("Say or type everything at once — “call the dentist, "
                                 + "book the flights Friday, we're out of milk…”. Tap the "
                                 + "mic on the keyboard to dictate.")
                                .font(.subheadline).foregroundStyle(Brand.muted)
                                .padding(.top, 8).padding(.leading, 5)
                        }
                        TextEditor(text: $text)
                            .frame(minHeight: 160)
                            .scrollContentBackground(.hidden)
                            .font(.body)
                    }
                }
            }
            if BrainDumpParser.isAvailable {
                Label("Parsed privately on your device", systemImage: "iphone")
                    .font(.caption).foregroundStyle(Brand.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            AsyncButton {
                await capture()
            } label: {
                Label("Capture", systemImage: "sparkles")
                    .frame(maxWidth: .infinity).padding(.vertical, 10)
            } onError: { error = $0 }
            .buttonStyle(.borderedProminent).tint(Brand.accent)
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || working)
        }
    }

    // MARK: Review

    @ViewBuilder
    private var review: some View {
        if let response {
            VStack(spacing: 14) {
                if let appliedNote {
                    Label(appliedNote, systemImage: "checkmark.circle.fill")
                        .font(.subheadline).foregroundStyle(Brand.good)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !response.reply.isEmpty {
                    Text(response.reply).font(.subheadline).foregroundStyle(Brand.fg)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                providerBadge

                if !response.actions.isEmpty && appliedNote == nil {
                    actionsCard(response.actions)
                }
                if !response.proposals.isEmpty {
                    proposalsCard(response.proposals)
                }
                if !response.errors.isEmpty {
                    Card {
                        CardLabel(text: "Couldn't use")
                        ForEach(Array(response.errors.enumerated()), id: \.offset) { _, e in
                            Text("• \(e)").font(.footnote).foregroundStyle(Brand.muted)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                if response.actions.isEmpty && response.proposals.isEmpty
                    && response.errors.isEmpty && appliedNote == nil {
                    Text("Nothing to capture from that.")
                        .font(.subheadline).foregroundStyle(Brand.muted)
                }

                footerButtons
            }
        }
    }

    private var providerBadge: some View {
        HStack {
            Chip(text: provider == "on_device" ? "on-device" : "server",
                 color: provider == "on_device" ? Brand.good : Brand.accent)
            Spacer()
        }
    }

    private func actionsCard(_ actions: [BrainDumpAction]) -> some View {
        Card {
            CardLabel(text: "Will add — review & apply")
            ForEach(Array(actions.enumerated()), id: \.offset) { _, a in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "plus.circle").font(.subheadline).foregroundStyle(Brand.accent)
                    Text(a.summary).font(.subheadline).foregroundStyle(Brand.nearWhite)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            AsyncButton {
                try await apply(actions)
            } label: {
                let n = actions.count
                Label("Apply \(n) change\(n == 1 ? "" : "s")", systemImage: "checkmark")
                    .frame(maxWidth: .infinity).padding(.vertical, 8)
            } onError: { error = $0 }
            .buttonStyle(.borderedProminent).tint(Brand.accent)
            .padding(.top, 4)
        }
    }

    private func proposalsCard(_ proposals: [BrainDumpProposal]) -> some View {
        Card {
            CardLabel(text: "Noticed about you")
            ForEach(proposals) { p in
                HStack(alignment: .top, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.summary).font(.subheadline).foregroundStyle(Brand.nearWhite)
                        if !p.rationale.isEmpty {
                            Text(p.rationale).font(.caption).foregroundStyle(Brand.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if acceptedProposals.contains(p.id) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(Brand.good)
                    } else {
                        AsyncButton {
                            try await withAPI { try await $0.acceptProposal(p.id) }
                            acceptedProposals.insert(p.id)
                        } label: {
                            Text("Keep").font(.caption.weight(.semibold))
                        } onError: { error = $0 }
                        .buttonStyle(.bordered).tint(Brand.accent)
                    }
                }
            }
        }
    }

    private var footerButtons: some View {
        VStack(spacing: 8) {
            // Escalate: re-run the same ramble through the server's own parse (the
            // opt-in cloud agent), which reaches the cases the on-device pass won't
            // touch — a settings change, or a subtler aside it didn't catch.
            if provider == "on_device" {
                AsyncButton {
                    try await runServerPass()
                } label: {
                    Label("Ask the server for another pass", systemImage: "cloud")
                        .frame(maxWidth: .infinity).padding(.vertical, 8)
                } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.accent)
            }
            Button {
                // Start over on a fresh dump without leaving the sheet.
                response = nil; provider = ""; appliedNote = nil
                acceptedProposals = []; text = ""
            } label: {
                Text("New dump").frame(maxWidth: .infinity).padding(.vertical, 8)
            }
            .buttonStyle(.bordered).tint(Brand.muted)
        }
    }

    // MARK: Actions

    /// Parse on-device when possible (private/cheap), else let the server parse.
    private func capture() async {
        working = true; defer { working = false }
        error = nil
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return }
        do {
            if let parsed = await BrainDumpParser.parse(raw) {
                // On-device parse succeeded — send the structure, never the raw
                // ramble, so the thought stays on the device (the privacy win).
                // An empty parse is a valid "found nothing"; the user can tap
                // "server pass" to escalate, which is when the text leaves the phone.
                response = try await withAPI { try await $0.braindump(parse: parsed) }
            } else {
                // On-device parsing unavailable (older OS / model absent) — let the
                // server parse the raw text.
                response = try await withAPI { try await $0.braindump(text: raw) }
            }
            provider = response?.provider?["assistant"] ?? "server"
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// Force the server (cloud/local) parse of the current text, replacing the
    /// on-device preview — the escalation path for harder reasoning.
    private func runServerPass() async throws {
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return }
        response = try await withAPI { try await $0.braindump(text: raw) }
        provider = response?.provider?["assistant"] ?? "server"
        acceptedProposals = []
    }

    private func apply(_ actions: [BrainDumpAction]) async throws {
        let result = try await withAPI {
            try await $0.applyAssistantActions(actions.map { $0.wire })
        }
        let n = result.applied
        if n > 0 { appliedNote = "Applied \(n) change\(n == 1 ? "" : "s")." }
        // A 2xx can still carry partial (or total) failures — a validation drop or
        // an id that moved. Surface those rather than reporting a false success;
        // when nothing applied, appliedNote stays nil so the actions card remains
        // for a retry.
        let failures = result.results.filter { !$0.ok }.map { $0.detail.isEmpty ? $0.summary : $0.detail }
        let reasons = failures + result.errors
        if !reasons.isEmpty {
            let count = failures.count
            error = "Couldn't apply \(count) item\(count == 1 ? "" : "s"): "
                + reasons.joined(separator: "; ")
        } else {
            error = nil
        }
    }
}
