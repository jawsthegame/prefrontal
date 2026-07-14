import SwiftUI

/// Read-only mail inbox mirroring the web dashboard's mail surface — the native
/// window onto the "Mail monitoring" pillar. Shows the triaged messages still
/// **awaiting action** (their linked todo is open) up top, then a **recent**
/// feed. Backed by `GET /mail`, which is side-effect-free, so a pull-to-refresh
/// is the whole interaction; resolving a message's todo (in the Todos tab)
/// clears it from "Needs action" on the next load.
struct MailView: View {
    @State private var inbox: MailInbox?
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let inbox {
                    if inbox.needsAction.isEmpty && inbox.recent.isEmpty {
                        emptyState
                    } else {
                        if !inbox.needsAction.isEmpty {
                            section(title: "Needs action",
                                    subtitle: "\(inbox.needsAction.count) open",
                                    messages: inbox.needsAction)
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

    private func section(title: String, subtitle: String?, messages: [MailMessage]) -> some View {
        Card {
            HStack {
                CardLabel(text: title)
                Spacer()
                if let subtitle { Text(subtitle).font(.caption2).foregroundStyle(Brand.muted) }
            }
            ForEach(Array(messages.enumerated()), id: \.element.id) { idx, m in
                if idx > 0 { Divider().overlay(Brand.line) }
                MailRow(message: m)
            }
        }
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
/// (urgency when notable, category, and who's waiting on you).
private struct MailRow: View {
    let message: MailMessage

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
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
