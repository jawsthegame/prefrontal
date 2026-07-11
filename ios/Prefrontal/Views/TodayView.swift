import SwiftUI

struct TodayView: View {
    @Binding var showPanic: Bool

    @State private var now: TodosNow?
    @State private var departure: DepartureNext.Departure?
    @State private var activeOuting: Outings.Outing?
    @State private var activeFocus: FocusState.Session?
    @State private var nudges: [Nudges.Nudge] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var showAdd = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }

                quickActions

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
            if let m = o.timeWindowMinutes { Chip(text: "~\(Int(m)) min window", color: Brand.teal.opacity(0.2), fg: Brand.teal) }
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

            self.now = try? await now
            let d = try? await dep
            self.departure = (d?.departure?.title != nil) ? d?.departure : nil
            self.activeOuting = (try? await out)?.active.first
            self.activeFocus = (try? await foc)?.active.first
            self.nudges = (try? await nud) ?? []
            self.error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}
