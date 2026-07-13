import SwiftUI

struct TodayView: View {
    @Binding var showPanic: Bool

    @State private var now: TodosNow?
    @State private var departure: DepartureNext.Departure?
    @State private var activeOuting: Outings.Outing?
    @State private var activeFocus: FocusState.Session?
    @State private var nudges: [Nudges.Nudge] = []
    @State private var briefing: Briefing?
    /// Held only to feed `LocalNotifications.reconcileSelfCare` (not rendered here).
    @State private var selfCareForNotifs: SelfCare?
    @State private var briefingExpanded = false
    @State private var briefingVote: Bool?
    @State private var error: String?
    @State private var loaded = false
    @State private var showAdd = false
    @State private var queuedOffline = 0

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if queuedOffline > 0 { offlineBanner }

                quickActions

                if let b = briefing, let text = b.text, !text.isEmpty { briefingCard(b, text) }

                nowCard
                if departure != nil { departureCard }
                if let o = activeOuting { outingCard(o) }
                if let f = activeFocus { focusCard(f) }
                if !nudges.isEmpty { nudgesCard }

                if loaded && error == nil { Spacer(minLength: 8) }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Today")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(isPresented: $showAdd) { AddTodoSheet { await load() } }
    }

    private var offlineBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(Brand.warn)
            Text("\(queuedOffline) \(queuedOffline == 1 ? "change" : "changes") waiting to sync — reconnect to your server.")
                .font(.footnote).foregroundStyle(Brand.fg)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Brand.warn.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.warn.opacity(0.35)))
    }

    private var quickActions: some View {
        HStack(spacing: 12) {
            Button { showAdd = true } label: {
                Label("Add todo", systemImage: "plus")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
            }
            .background(Brand.navyRaised, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Brand.line))
            .foregroundStyle(Brand.nearWhite)

            Button { showPanic = true } label: {
                Label("Panic", systemImage: "exclamationmark.triangle.fill")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
            }
            .background(Brand.danger.opacity(0.9), in: RoundedRectangle(cornerRadius: 14))
            .foregroundStyle(.white)
        }
    }

    private func briefingCard(_ b: Briefing, _ text: String) -> some View {
        // Collapsed preview shows the first few rendered lines; the server sends
        // Markdown (## headers, - bullets, **bold**) which MarkdownText renders.
        let previewLines = 6
        let long = MarkdownText.lineCount(text) > previewLines
        return Card {
            HStack {
                CardLabel(text: "Morning briefing")
                Spacer()
                if let d = b.date { Text(d).font(.caption2).foregroundStyle(Brand.muted) }
            }
            MarkdownText(text: text, lineLimit: briefingExpanded ? nil : previewLines)
            if long {
                Button(briefingExpanded ? "Show less" : "Show more") {
                    withAnimation { briefingExpanded.toggle() }
                }
                .font(.caption.weight(.medium)).tint(Brand.teal)
            }
            Divider().overlay(Brand.line)
            if let vote = briefingVote {
                Label(vote ? "Thanks — glad it helped." : "Thanks — noted.",
                      systemImage: "checkmark.circle")
                    .font(.caption).foregroundStyle(Brand.muted)
            } else {
                HStack(spacing: 12) {
                    Text("Was this helpful?").font(.caption).foregroundStyle(Brand.muted)
                    Spacer()
                    AsyncButton {
                        try await withAPI { try await $0.briefingFeedback(helpful: true) }
                        briefingVote = true
                    } label: { Image(systemName: "hand.thumbsup") } onError: { error = $0 }
                    .buttonStyle(.borderless).tint(Brand.good)
                    AsyncButton {
                        try await withAPI { try await $0.briefingFeedback(helpful: false) }
                        briefingVote = false
                    } label: { Image(systemName: "hand.thumbsdown") } onError: { error = $0 }
                    .buttonStyle(.borderless).tint(Brand.muted)
                }
            }
        }
    }

    private var nowCard: some View {
        Card {
            CardLabel(text: "Right now")
            if let now {
                if let s = now.suggestion, let title = s.title {
                    Text("You can do this now:").font(.footnote).foregroundStyle(Brand.muted)
                    Text(title).font(.headline).foregroundStyle(Brand.nearWhite)
                    if let m = s.estimateMinutes { Chip(text: "~\(Int(m)) min") }
                } else {
                    Text(now.reason ?? "Nothing pressing right now.")
                        .font(.headline).foregroundStyle(Brand.nearWhite)
                }
                if let nc = now.nextCommitment, let t = nc.title {
                    Divider().overlay(Brand.line)
                    HStack {
                        Image(systemName: "calendar").foregroundStyle(Brand.teal)
                        Text("Next: \(t)").font(.subheadline).foregroundStyle(Brand.muted)
                        Spacer()
                        Text(PFDate.dayTime(nc.startAt)).font(.caption).foregroundStyle(Brand.muted)
                    }
                }
            } else {
                Text("…").foregroundStyle(Brand.muted)
            }
        }
    }

    private var departureCard: some View {
        let d = departure!
        return Card {
            CardLabel(text: "Leave by")
            HStack(alignment: .firstTextBaseline) {
                Text(PFDate.time(d.leaveBy)).font(.title2.weight(.bold)).foregroundStyle(levelColor(d.level))
                Spacer()
                if let m = d.minutesUntilLeave { Text(minutesPhrase(m)).font(.caption).foregroundStyle(Brand.muted) }
            }
            Text(d.title ?? "").font(.subheadline).foregroundStyle(Brand.nearWhite)
            if let loc = d.location { Text(loc).font(.caption).foregroundStyle(Brand.muted) }
        }
    }

    private func outingCard(_ o: Outings.Outing) -> some View {
        Card {
            CardLabel(text: "Out now")
            Text(o.intention).font(.subheadline).foregroundStyle(Brand.nearWhite)
            if let m = o.timeWindowMinutes { Chip(text: "~\(Int(m)) min window", color: Brand.accent) }
            AsyncButton {
                try await withAPI { try await $0.returnOuting() }
                await load()
            } label: {
                Label("I'm back", systemImage: "house").frame(maxWidth: .infinity).padding(.vertical, 8)
            } onError: { error = $0 }
            .buttonStyle(.borderedProminent).tint(Brand.teal)
        }
    }

    private func focusCard(_ f: FocusState.Session) -> some View {
        Card {
            CardLabel(text: "Focus session")
            Text(f.intendedTask ?? "Focusing").font(.subheadline).foregroundStyle(Brand.nearWhite)
            if let m = f.plannedMinutes { Chip(text: "\(Int(m)) min planned") }
            AsyncButton {
                try await withAPI { try await $0.endFocus() }
                await load()
            } label: {
                Label("Wrap up", systemImage: "flag.checkered").frame(maxWidth: .infinity).padding(.vertical, 8)
            } onError: { error = $0 }
            .buttonStyle(.borderedProminent).tint(Brand.blue)
        }
    }

    private var nudgesCard: some View {
        Card {
            CardLabel(text: "Recent nudges")
            ForEach(nudges.prefix(4)) { n in
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(levelColor(n.level)).frame(width: 7, height: 7).padding(.top, 6)
                    Text(n.message).font(.footnote).foregroundStyle(Brand.nearWhite)
                }
            }
        }
    }

    private func levelColor(_ level: String?) -> Color {
        switch level {
        case "go", "urgent": return Brand.danger
        case "soon": return Brand.warn
        default: return Brand.teal
        }
    }

    private func minutesPhrase(_ m: Double) -> String {
        if m <= 0 { return "now" }
        if m < 60 { return "in \(Int(m)) min" }
        let h = Int(m) / 60, mm = Int(m) % 60
        return mm == 0 ? "in \(h)h" : "in \(h)h \(mm)m"
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let now = client.todosNow(cap: 60)
            async let dep = client.departureNext()
            async let out = client.outings()
            async let foc = client.focus()
            async let nud = client.nudges(limit: 8)
            async let brief = client.briefing()
            // Fetched only to refresh the offline self-care local notifications
            // (not shown on Today — the Me tab renders the self-care card).
            async let care = client.selfCare()

            self.now = try? await now
            let d = try? await dep
            self.departure = (d?.departure?.title != nil) ? d?.departure : nil
            self.activeOuting = (try? await out)?.active.first
            self.activeFocus = (try? await foc)?.active.first
            self.nudges = (try? await nud) ?? []
            self.briefing = try? await brief
            self.selfCareForNotifs = try? await care
            self.error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        queuedOffline = OfflineQueue.count
        // Schedule local nudges as an off-tailnet fallback (replaced each refresh
        // from current state). No-op unless notifications are authorized.
        await LocalNotifications.reconcileDeparture(departure)
        await LocalNotifications.reconcileSelfCare(selfCareForNotifs)
        // Keep the outing/focus Live Activity in sync with the active session.
        await LiveActivityManager.sync(outing: activeOuting, focus: activeFocus)
        loaded = true
    }
}
