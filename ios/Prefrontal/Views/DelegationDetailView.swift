import SwiftUI

/// A delegated todo's full workspace on its own screen — the brief, the draft
/// comms, the action items, the research trail, and (for an `auto` run) the
/// questions it needs answered, with an inline answer field.
///
/// Lifted out of the todo card (Phase 1 of docs/design/delegation-workspace.md):
/// the card kept getting more content than a checklist item should hold, and —
/// more importantly — a `needs_input` run was a *dead end* on iOS, because the
/// embedded panel rendered neither the questions nor a way to answer them. Here
/// the questions are first-class and answering re-runs the research
/// (`POST /todos/{id}/delegate/answers`), same as the web dashboard.
struct DelegationDetailView: View {
    let todo: Todo
    let reload: () async -> Void
    let onError: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    /// Typed answers keyed by the question's position in the full `questions`
    /// list — the server records answers positionally.
    @State private var answers: [Int: String] = [:]

    var body: some View {
        NavigationStack {
            ScrollView {
                if let g = todo.delegation {
                    VStack(alignment: .leading, spacing: 16) {
                        header(g)
                        if g.isWorking { workingNote }
                        briefSection(g)
                        questionsSection(g)
                        actionsSection(g)
                        draftsSection(g)
                        stepsSection(g)
                        contextSection(g)
                        footer(g)
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .brandScreen()
            .navigationTitle(todo.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }

    private func header(_ g: Delegation) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(g.label).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
            Spacer(minLength: 8)
            if let d = g.detail, !d.isEmpty, g.status != "failed" {
                Text(d).font(.caption2).foregroundStyle(Brand.muted)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    private var workingNote: some View {
        Label("Reading your context and prepping — you'll get a heads-up when it's ready.",
              systemImage: "brain.head.profile")
            .font(.caption).foregroundStyle(Brand.muted)
    }

    @ViewBuilder private func briefSection(_ g: Delegation) -> some View {
        if let brief = g.brief, !brief.isEmpty {
            Card {
                CardLabel(text: "Brief")
                Text(brief).font(.footnote).foregroundStyle(Brand.nearWhite)
            }
        }
    }

    @ViewBuilder private func questionsSection(_ g: Delegation) -> some View {
        let qs = g.questions ?? []
        if !qs.isEmpty {
            Card {
                CardLabel(text: g.pendingQuestions.isEmpty ? "Questions" : "It needs a few things from you")
                ForEach(Array(qs.enumerated()), id: \.offset) { idx, q in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(q.text ?? "—").font(.subheadline).foregroundStyle(Brand.nearWhite)
                        if let why = q.why, !why.isEmpty {
                            Text(why).font(.caption2).foregroundStyle(Brand.muted)
                        }
                        if let ans = q.answer, !ans.isEmpty {
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "checkmark.circle.fill")
                                    .font(.caption2).foregroundStyle(Brand.ok)
                                Text(ans).font(.caption).foregroundStyle(Brand.fg)
                            }
                        } else {
                            TextField("Your answer…", text: Binding(
                                get: { answers[idx] ?? "" },
                                set: { answers[idx] = $0 }
                            ))
                            .textFieldStyle(.roundedBorder)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 2)
                }
                if !g.pendingQuestions.isEmpty {
                    AsyncButton {
                        let payload = qs.indices.map {
                            (answers[$0] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                        }
                        try await withAPI { try await $0.delegateAnswers(todo.id, answers: payload) }
                        await reload()
                        dismiss()
                    } label: {
                        Label("Send answers & carry on", systemImage: "paperplane.fill")
                            .frame(maxWidth: .infinity).padding(.vertical, 6)
                    } onError: { onError($0) }
                    .buttonStyle(.borderedProminent).tint(Brand.accent)
                    .disabled(!hasAnswer(g))
                }
            }
        }
    }

    @ViewBuilder private func actionsSection(_ g: Delegation) -> some View {
        let actions = (g.actions ?? []).filter { !($0.text ?? "").isEmpty }
        if !actions.isEmpty {
            Card {
                CardLabel(text: "Action items")
                ForEach(Array(actions.enumerated()), id: \.offset) { _, a in
                    let text = a.text ?? ""
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: a.mine == true ? "person.fill" : "person")
                            .font(.caption2).foregroundStyle(a.mine == true ? Brand.teal : Brand.muted)
                        Text(text).font(.caption).foregroundStyle(Brand.nearWhite)
                        Spacer(minLength: 0)
                        if a.mine == true {
                            AsyncButton {
                                try await withAPI { try await $0.addTodo(title: text) }
                                await reload()
                            } label: { Text("＋ Todo").font(.caption2) } onError: { onError($0) }
                            .buttonStyle(.borderless).tint(Brand.teal)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func draftsSection(_ g: Delegation) -> some View {
        if let drafts = g.drafts, !drafts.isEmpty {
            Card {
                CardLabel(text: drafts.count == 1 ? "Draft" : "Drafts")
                ForEach(Array(drafts.enumerated()), id: \.offset) { _, dr in
                    VStack(alignment: .leading, spacing: 2) {
                        let head = [dr.channel, dr.to, dr.subject]
                            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
                        if !head.isEmpty {
                            Text(head).font(.caption2.weight(.semibold)).foregroundStyle(Brand.muted)
                        }
                        if let b = dr.body, !b.isEmpty {
                            Text(b).font(.caption2).foregroundStyle(Brand.nearWhite)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(Brand.raise, in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    @ViewBuilder private func stepsSection(_ g: Delegation) -> some View {
        if let steps = g.steps, !steps.isEmpty {
            Card {
                CardLabel(text: "What it looked up (\(steps.count))")
                ForEach(Array(steps.enumerated()), id: \.offset) { _, s in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Image(systemName: s.ok == true ? "checkmark.circle" : "xmark.circle")
                                .font(.caption2).foregroundStyle(s.ok == true ? Brand.ok : Brand.warn)
                            Text([s.server, s.tool].compactMap { $0 }.joined(separator: "."))
                                .font(.caption2.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                        }
                        if let why = s.why, !why.isEmpty {
                            Text(why).font(.caption2).foregroundStyle(Brand.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    @ViewBuilder private func contextSection(_ g: Delegation) -> some View {
        if let c = g.context, !c.isEmpty {
            Card {
                CardLabel(text: "Context you gave the assistant")
                Text(c).font(.caption2).foregroundStyle(Brand.muted)
            }
        }
    }

    @ViewBuilder private func footer(_ g: Delegation) -> some View {
        if g.status == "failed", let d = g.detail, !d.isEmpty {
            Text(d).font(.caption2).foregroundStyle(Brand.warn)
        }
        if g.canReturn {
            AsyncButton {
                try await withAPI { try await $0.returnDelegation(todo.id) }
                await reload()
                dismiss()
            } label: {
                Label("Mark returned", systemImage: "arrow.uturn.left")
                    .frame(maxWidth: .infinity).padding(.vertical, 6)
            } onError: { onError($0) }
            .buttonStyle(.bordered).tint(Brand.teal)
        }
    }

    private func hasAnswer(_ g: Delegation) -> Bool {
        (g.questions ?? []).indices.contains { idx in
            !(answers[idx] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
}
