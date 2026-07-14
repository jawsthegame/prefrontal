import SwiftUI

/// Ambiguity clarification — the task-paralysis module's task-initiation lever,
/// made native. Lists the vague todos/commitments the server has flagged with a
/// pending clarifying question (`GET /clarifications`); answering one — by
/// picking an offered reading or typing your own — hones the item into something
/// startable, and when the chosen reading maps to a built-in **playbook** the
/// guided walkthrough opens. A "check now" button runs the sweep on demand.
/// Reached from the Todos tab.
struct ClarifyView: View {
    @State private var pending: [Clarification] = []
    @State private var guided: [GuidedClarification] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var playbook: Playbook?

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                sweepButton
                if loaded && pending.isEmpty && guided.isEmpty && error == nil {
                    emptyState
                }
                ForEach(pending) { item in
                    ClarificationCard(item: item, reload: load,
                                      onError: { error = $0 },
                                      onPlaybook: { playbook = $0 })
                }
                if !guided.isEmpty { guidedSection }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Clarify")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(item: $playbook) { PlaybookSheet(playbook: $0) }
    }

    private var sweepButton: some View {
        AsyncButton {
            _ = try await withAPI { try await $0.runClarificationSweep() }
            await load()
        } label: {
            Label("Check for ambiguous items", systemImage: "sparkles")
                .frame(maxWidth: .infinity).padding(.vertical, 10)
        } onError: { error = $0 }
        .buttonStyle(.bordered).tint(Brand.accent)
    }

    private var emptyState: some View {
        Card {
            VStack(spacing: 8) {
                Image(systemName: "checkmark.bubble").font(.largeTitle).foregroundStyle(Brand.muted)
                Text("Nothing to clarify").font(.headline).foregroundStyle(Brand.nearWhite)
                Text("When a todo is too vague to start, it'll show up here with a question or two to hone it into a first step.")
                    .font(.footnote).foregroundStyle(Brand.muted)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
    }

    private var guidedSection: some View {
        Card {
            CardLabel(text: "Recently honed")
            ForEach(guided) { g in
                AsyncButton {
                    if let tt = g.taskType {
                        playbook = try await withAPI { try await $0.playbook(taskType: tt) }
                    }
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(g.title).font(.subheadline).foregroundStyle(Brand.nearWhite)
                            if let a = g.answer, !a.isEmpty {
                                Text(a).font(.caption).foregroundStyle(Brand.muted).lineLimit(1)
                            }
                        }
                        Spacer(minLength: 4)
                        if g.playbookTitle != nil {
                            Image(systemName: "list.bullet.rectangle").font(.caption)
                                .foregroundStyle(Brand.accent)
                        }
                    }
                } onError: { error = $0 }
                .buttonStyle(.plain)
                .disabled(g.taskType == nil)
            }
        }
    }

    private func load() async {
        do {
            let list = try await withAPI { try await $0.clarifications() }
            pending = list.clarifications
            guided = list.guided
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// One pending question: the vague item, the question, its candidate readings as
/// tap-to-resolve buttons, a free-text "something else" path, and a dismiss.
private struct ClarificationCard: View {
    let item: Clarification
    let reload: () async -> Void
    let onError: (String) -> Void
    let onPlaybook: (Playbook) -> Void

    @State private var showFreeText = false
    @State private var answer = ""

    var body: some View {
        Card {
            Text(item.title).font(.headline).foregroundStyle(Brand.nearWhite)
            Text(item.question).font(.subheadline).foregroundStyle(Brand.muted)

            ForEach(Array(item.options.enumerated()), id: \.offset) { idx, opt in
                AsyncButton {
                    let res = try await withAPI {
                        try await $0.resolveClarification(item.id, optionIndex: idx)
                    }
                    if let pb = res.playbook { onPlaybook(pb) }
                    await reload()
                } label: {
                    optionLabel(opt)
                } onError: { onError($0) }
                .buttonStyle(.plain)
            }

            if showFreeText {
                HStack(spacing: 8) {
                    TextField("Describe what it means…", text: $answer)
                        .textFieldStyle(.roundedBorder)
                    AsyncButton {
                        let res = try await withAPI {
                            try await $0.resolveClarification(item.id, answer: answer)
                        }
                        if let pb = res.playbook { onPlaybook(pb) }
                        await reload()
                    } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.title3)
                    } onError: { onError($0) }
                    .accessibilityLabel("Send answer")
                    .tint(Brand.accent)
                    .disabled(answer.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }

            HStack {
                Button(showFreeText ? "Cancel" : "Something else…") {
                    withAnimation { showFreeText.toggle() }
                }
                .font(.caption).tint(Brand.accent)
                Spacer()
                AsyncButton {
                    try await withAPI { try await $0.dismissClarification(item.id) }
                    await reload()
                } label: {
                    Text("Not ambiguous").font(.caption)
                } onError: { onError($0) }
                .tint(Brand.muted)
            }
        }
    }

    private func optionLabel(_ opt: Clarification.Option) -> some View {
        HStack(spacing: 8) {
            Text(opt.label ?? "—").font(.subheadline).foregroundStyle(Brand.fg)
                .frame(maxWidth: .infinity, alignment: .leading)
            if opt.hasPlaybook {
                Image(systemName: "list.bullet.rectangle").font(.caption).foregroundStyle(Brand.accent)
            }
        }
        .padding(.vertical, 8).padding(.horizontal, 10)
        .frame(maxWidth: .infinity)
        .background(Brand.chip, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Brand.line))
    }
}

/// A guided walkthrough rendered as an ordered, numbered step list.
private struct PlaybookSheet: View {
    let playbook: Playbook
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !playbook.intro.isEmpty {
                        Text(playbook.intro).font(.subheadline).foregroundStyle(Brand.fg)
                    }
                    ForEach(Array(playbook.steps.enumerated()), id: \.offset) { idx, step in
                        Card {
                            HStack(alignment: .top, spacing: 10) {
                                Text("\(idx + 1)").font(.headline).foregroundStyle(Brand.accent)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(step.title).font(.subheadline.weight(.semibold))
                                        .foregroundStyle(Brand.nearWhite)
                                    if !step.detail.isEmpty {
                                        Text(step.detail).font(.footnote).foregroundStyle(Brand.muted)
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(16)
            }
            .brandScreen()
            .navigationTitle(playbook.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}
