import SwiftUI

/// Read-only mail inbox mirroring the web dashboard's mail surface — the native
/// window onto the "Mail monitoring" pillar. Shows the triaged messages still
/// **awaiting action** (their linked todo is open) up top — each confirming the
/// tracked todo — then the ones that **need a todo** (triage flagged them but
/// their todo was suppressed at ingest, so they carry none), each with a one-tap
/// "Create todo" offer, then a **recent** feed. Backed by `GET /mail` (a
/// side-effect-free read) plus `POST /mail/{id}/todo` for the create offer;
/// resolving a message's todo (in the Todos tab) clears it from "Needs action",
/// and creating one moves it out of "Needs a todo" on the next load.
struct MailView: View {
    @State private var inbox: MailInbox?
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let inbox {
                    if inbox.needsAction.isEmpty && inbox.needsActionNoTodo.isEmpty
                        && inbox.recent.isEmpty {
                        emptyState
                    } else {
                        if !inbox.needsAction.isEmpty {
                            section(title: "Needs action",
                                    subtitle: "\(inbox.needsAction.count) open",
                                    messages: inbox.needsAction,
                                    confirmsTodo: true)
                        }
                        if !inbox.needsActionNoTodo.isEmpty {
                            section(title: "Needs a todo",
                                    subtitle: "\(inbox.needsActionNoTodo.count) untracked",
                                    messages: inbox.needsActionNoTodo,
                                    offerCreate: true)
                        }
                        if !inbox.recent.isEmpty {
                            section(title: "Recent", subtitle: nil, messages: inbox.recent)
                        }
                    }
                } else if error == nil {
                    Card { Text("…").foregroundStyle(Brand.muted) }
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Mail")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
    }

    /// A card of messages. `confirmsTodo` shows the "Todo" chip on rows with an
    /// open linked loop (the "Needs action" section); `offerCreate` shows the
    /// "Create todo" button on rows without one (the "Needs a todo" section). The
    /// "Recent" feed passes neither — a persisted `todoId` there may already be
    /// resolved, so it stays a plain read.
    private func section(title: String, subtitle: String?, messages: [MailMessage],
                         confirmsTodo: Bool = false, offerCreate: Bool = false) -> some View {
        Card {
            HStack {
                CardLabel(text: title)
                Spacer()
                if let subtitle { Text(subtitle).font(.caption2).foregroundStyle(Brand.muted) }
            }
            ForEach(Array(messages.enumerated()), id: \.element.id) { idx, m in
                if idx > 0 { Divider().overlay(Brand.line) }
                MailRow(
                    message: m,
                    confirmsTodo: confirmsTodo,
                    onCreateTodo: offerCreate && m.todoId == nil
                        ? { try await createTodo(for: m) } : nil,
                    onError: { error = $0 }
                )
            }
        }
    }

    /// Turn a suppressed needs-action message into a tracked todo, then refresh so
    /// it moves from "Needs a todo" into "Needs action". Throws so the row's
    /// `AsyncButton` shows its spinner and surfaces any failure via `onError`.
    private func createTodo(for message: MailMessage) async throws {
        _ = try await withAPI { try await $0.createMailTodo(message.id) }
        await load()
    }

    private var emptyState: some View {
        Card {
            VStack(spacing: 8) {
                Image(systemName: "tray").font(.largeTitle).foregroundStyle(Brand.muted)
                Text("No mail to show").font(.headline).foregroundStyle(Brand.nearWhite)
                Text("Triaged mail lands here once mail monitoring is set up on your server.")
                    .font(.footnote).foregroundStyle(Brand.muted)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
    }

    private func load() async {
        do {
            inbox = try await withAPI { try await $0.mail() }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// A single triaged message: sender + time, subject, gist, and triage chips
/// (urgency when notable, category, and who's waiting on you). When it has a
/// linked todo, a "Todo" chip confirms the loop is tracked; when it doesn't and
/// `onCreateTodo` is supplied, a "Create todo" button offers to make one.
private struct MailRow: View {
    let message: MailMessage
    /// Show the "Todo" confirmation chip when this row has an open linked loop.
    var confirmsTodo: Bool = false
    /// Supplied only for untracked needs-action rows — turning it into a todo.
    var onCreateTodo: (() async throws -> Void)? = nil
    /// Surfaces a create failure to the parent's error banner.
    var onError: (String) -> Void = { _ in }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if message.isUnread {
                    Circle().fill(Brand.accent).frame(width: 7, height: 7)
                }
                Text(message.senderDisplay)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Brand.nearWhite)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if !whenText.isEmpty {
                    Text(whenText).font(.caption2).foregroundStyle(Brand.muted)
                }
            }
            if let subject = message.subject, !subject.isEmpty {
                Text(subject)
                    .font(.subheadline)
                    .foregroundStyle(Brand.fg)
                    .lineLimit(2)
            }
            if let gist = message.gist {
                Text(gist)
                    .font(.footnote)
                    .foregroundStyle(Brand.muted)
                    .lineLimit(2)
            }
            if !chips.isEmpty {
                FlowRow(spacing: 6, lineSpacing: 6) {
                    ForEach(Array(chips.enumerated()), id: \.offset) { _, chip in
                        Chip(text: chip.text, color: chip.color)
                    }
                }
                .padding(.top, 2)
            }
            todoAffordance
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The row's todo state: a "Todo" chip confirming a tracked loop, or — for an
    /// untracked needs-action row — a "Create todo" button.
    @ViewBuilder private var todoAffordance: some View {
        if confirmsTodo, message.todoId != nil {
            HStack(spacing: 4) {
                Image(systemName: "checkmark.circle.fill").font(.caption2)
                Text("Todo").font(.caption2.weight(.semibold))
            }
            .foregroundStyle(Brand.good)
            .padding(.top, 2)
        } else if let onCreateTodo {
            AsyncButton {
                try await onCreateTodo()
            } label: {
                Label("Create todo", systemImage: "plus.circle")
                    .font(.caption.weight(.semibold))
            } onError: { onError($0) }
            .buttonStyle(.bordered)
            .tint(Brand.accent)
            .controlSize(.small)
            .padding(.top, 4)
        }
    }

    /// Localized received time ("Tue 3:40 PM"), or "" when unparseable/absent.
    private var whenText: String { PFDate.dayTime(message.receivedAt) }

    /// The triage chips to render, in reading order.
    private var chips: [(text: String, color: Color?)] {
        var out: [(text: String, color: Color?)] = []
        if let u = message.urgency?.lowercased(), let color = urgencyColor(u) {
            out.append((text: u, color: color))
        }
        if let c = message.category?.lowercased(), !c.isEmpty, c != "other" {
            out.append((text: c, color: nil))
        }
        if let w = message.waitingOn, !w.isEmpty {
            out.append((text: "waiting: \(w)", color: Brand.fyi))
        }
        return out
    }

    /// Only the notable urgencies get a colored chip; low/normal stay quiet.
    private func urgencyColor(_ urgency: String) -> Color? {
        switch urgency {
        case "urgent": return Brand.danger
        case "high": return Brand.warn
        default: return nil
        }
    }
}
