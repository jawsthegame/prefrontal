import WidgetKit
import SwiftUI
import AppIntents

// MARK: - Snapshot

/// The glance data shown on the widget. Assembled from the same endpoints the
/// Today tab uses, via the shared App Group config.
struct Glance {
    var notConfigured = false
    var depTitle: String?
    var depLeaveBy: Date?
    var depLevel: String?
    var freeMinutes = 0
    /// The single todo the server suggests starting right now (`todos/now`) —
    /// the concrete initiation nudge, in place of a bare "N fit" count.
    var suggestionTitle: String?
    var suggestionMinutes: Int?
    var nextTitle: String?
    /// Start time of the next commitment, for the "Next: … · 3:40 PM" footer.
    var nextAt: Date?
    var meal: (Int, Int)?
    var water: (Int, Int)?
    /// Every enabled self-care check, keyed by its `key` (count, target) — so the
    /// configurable Lock Screen ring can render whichever one you pick.
    var selfCareChecks: [String: (Int, Int)] = [:]
    // Active lifecycle state — when set, the widget offers a one-tap end action.
    var outingIntention: String?
    var focusTask: String?

    static let sample = Glance(
        depTitle: "Dentist", depLeaveBy: Date().addingTimeInterval(45 * 60), depLevel: "soon",
        freeMinutes: 45, suggestionTitle: "Reply to landlord", suggestionMinutes: 15,
        meal: (2, 3), water: (3, 6)
    )

    @MainActor
    static func fetch() async -> Glance {
        let client: APIClient
        do { client = try APIClient(shared: ()) }
        catch { return Glance(notConfigured: true) }

        async let depT = try? await client.departureNext()
        async let nowT = try? await client.todosNow(cap: 240)
        async let scT = try? await client.selfCare()
        async let outT = try? await client.outings()
        async let focT = try? await client.focus()
        let dep = await depT, now = await nowT, sc = await scT
        let outing = await outT, focus = await focT

        var g = Glance()
        g.outingIntention = outing?.active.first?.intention
        g.focusTask = focus?.active.first.map { $0.intendedTask ?? "Focusing" }
        if let d = dep?.departure, d.title != nil {
            g.depTitle = d.title
            g.depLeaveBy = PFDate.parse(d.leaveBy)
            g.depLevel = d.level
        }
        if let now {
            g.freeMinutes = Int(now.freeMinutes ?? 0)
            g.nextTitle = now.nextCommitment?.title
            g.nextAt = PFDate.parse(now.nextCommitment?.startAt)
            // Prefer the server's concrete "you can do this now" pick over a count;
            // it's already in this payload, so no extra request (dropped todosFit).
            if let s = now.suggestion, let t = s.title {
                g.suggestionTitle = t
                g.suggestionMinutes = s.estimateMinutes.map { Int($0) }
            }
        }
        g.applySelfCare(sc?.checks)
        return g
    }

    /// Fold the enabled self-care checks — plus the `meal`/`water` aliases the
    /// widgets read directly — into this glance. Shared by `fetch()` and
    /// `fetchSelfCare()` so the payload mapping lives in exactly one place.
    mutating func applySelfCare(_ checks: [SelfCare.Check]?) {
        guard let checks else { return }
        for c in checks where c.enabled { selfCareChecks[c.key] = (c.count, c.target) }
        meal = selfCareChecks["meal"]
        water = selfCareChecks["water"]
    }

    /// Self-care-only fetch for the configurable Lock Screen ring, which renders
    /// nothing but `selfCareChecks`. The full `fetch()` fires five parallel
    /// requests (departure, todos/now, self-care, outings, focus); the ring uses
    /// only one of them, so calling `fetch()` woke the radio for four responses it
    /// threw away — once per reload, per pinned ring (a user can add several). This
    /// hits just `/self-care`. Falls back to a `notConfigured` glance when the app
    /// isn't set up, matching `fetch()`.
    @MainActor
    static func fetchSelfCare() async -> Glance {
        let client: APIClient
        do { client = try APIClient(shared: ()) }
        catch { return Glance(notConfigured: true) }

        var g = Glance()
        g.applySelfCare((try? await client.selfCare())?.checks)
        return g
    }
}

// MARK: - Timeline

struct GlanceEntry: TimelineEntry {
    let date: Date
    let glance: Glance
}

struct Provider: TimelineProvider {
    func placeholder(in context: Context) -> GlanceEntry {
        GlanceEntry(date: Date(), glance: .sample)
    }
    func getSnapshot(in context: Context, completion: @escaping (GlanceEntry) -> Void) {
        if context.isPreview { completion(GlanceEntry(date: Date(), glance: .sample)); return }
        Task { completion(GlanceEntry(date: Date(), glance: await Glance.fetch())) }
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<GlanceEntry>) -> Void) {
        Task {
            let g = await Glance.fetch()
            let next = Date().addingTimeInterval(20 * 60)
            completion(Timeline(entries: [GlanceEntry(date: Date(), glance: g)], policy: .after(next)))
        }
    }
}

// MARK: - Views
//
// Uses system colors/materials only — custom dynamic-color providers don't
// always survive the widget's out-of-process view archiving.

// Brand palette via asset color sets (archive-safe, unlike code-defined
// dynamic colors). Used on the Home Screen widgets; Lock Screen accessories
// keep the system's tinted rendering.
private extension Color {
    static let wPaper = Color("WidgetPaper")
    static let wInk = Color("WidgetInk")
    static let wMuted = Color("WidgetMuted")
    static let wGreen = Color("WidgetGreen")
}

private func levelColor(_ level: String?) -> Color {
    switch level {
    case "go", "urgent", "call": return Color(red: 0.75, green: 0.20, blue: 0.29)
    case "soon", "firm": return Color(red: 0.76, green: 0.34, blue: 0.12)
    case "heads_up", "soft": return Color(red: 0.73, green: 0.46, blue: 0.18)
    default: return .wInk
    }
}

struct PrefrontalWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: GlanceEntry
    var g: Glance { entry.glance }

    private var isSystem: Bool {
        family == .systemSmall || family == .systemMedium || family == .systemLarge
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .widgetURL(URL(string: "prefrontal://today"))
            // Plain Color (both branches) — do NOT type-erase with AnyView, or
            // WidgetKit fails to detect the container background and renders blank.
            .containerBackground(for: .widget) {
                isSystem ? Color.wPaper : Color.clear
            }
    }

    @ViewBuilder private var content: some View {
        switch family {
        case .systemSmall: small
        case .systemMedium: medium
        case .accessoryRectangular: accRect
        case .accessoryInline: accInline
        default: small
        }
    }

    private var header: some View {
        HStack(spacing: 5) {
            Image(systemName: "brain.head.profile").font(.caption2).foregroundStyle(Color.wGreen)
            Text("Prefrontal").font(.caption2.weight(.bold)).foregroundStyle(Color.wMuted)
            // One-tap thought capture on the Home Screen families: opens the app's
            // pre-focused capture field (which feeds the sensor path) via
            // `OpenThoughtCaptureIntent`. A `Button(intent:)` — not a `Link` — so it
            // works in the small family too (small widgets have a single `widgetURL`
            // tap target and ignore `Link`, but iOS 17 interactive buttons work in
            // all families). Lock Screen accessories can't host a second tap target,
            // and there's nothing to capture into until the app is connected.
            if isSystem, !g.notConfigured {
                Spacer(minLength: 4)
                Button(intent: OpenThoughtCaptureIntent()) {
                    Image(systemName: "square.and.pencil")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.wGreen)
                        .padding(6)
                        .background(Color.wGreen.opacity(0.15), in: Circle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    // An active outing/focus is the most actionable thing → offer its one-tap
    // end action; self-care yields the space to it on the small widget.
    private var hasActive: Bool { g.outingIntention != nil || g.focusTask != nil }

    private var small: some View {
        VStack(alignment: .leading, spacing: 4) {
            header
            Spacer(minLength: 2)
            smallBody
            Spacer(minLength: 2)
            if !hasActive { selfCareLine }
        }
    }

    @ViewBuilder private var smallBody: some View {
        if g.notConfigured {
            Text("Tap to connect").font(.callout.weight(.semibold)).foregroundStyle(Color.wMuted)
        } else if let intention = g.outingIntention {
            Text("OUT").font(.caption2).foregroundStyle(Color.wMuted)
            Text(intention).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
            actionButton("I'm back", systemImage: "house", intent: ImBackIntent())
        } else if let task = g.focusTask {
            Text("FOCUS").font(.caption2).foregroundStyle(Color.wMuted)
            Text(task).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
            actionButton("Wrap up", systemImage: "flag.checkered", intent: EndFocusIntent())
        } else if let leave = g.depLeaveBy {
            Text("Leave").font(.caption).foregroundStyle(Color.wMuted)
            Text(leave, style: .time).font(.title3.weight(.bold)).foregroundStyle(levelColor(g.depLevel))
            Text(g.depTitle ?? "").font(.caption).foregroundStyle(Color.wInk).lineLimit(2)
        } else if let task = g.suggestionTitle {
            Text("DO NOW").font(.caption2).foregroundStyle(Color.wMuted)
            Text(task).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
            Text(rightNowSub).font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
        } else if g.freeMinutes > 0 {
            Text("\(g.freeMinutes) min").font(.title2.weight(.bold)).foregroundStyle(Color.wInk)
            Text("free — nothing queued").font(.caption).foregroundStyle(Color.wMuted)
        } else {
            Text("All clear").font(.title3.weight(.bold)).foregroundStyle(Color.wInk)
            if let t = g.nextTitle { Text("next: \(t)").font(.caption).foregroundStyle(Color.wMuted).lineLimit(2) }
        }
    }

    private var medium: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if g.notConfigured {
                Text("Open Prefrontal to connect this widget.").font(.footnote).foregroundStyle(Color.wMuted)
                Spacer(minLength: 0)
            } else {
                HStack(alignment: .top, spacing: 14) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("LEAVE BY").font(.caption2).foregroundStyle(Color.wMuted)
                        if let leave = g.depLeaveBy {
                            Text(leave, style: .time).font(.title3.weight(.bold)).foregroundStyle(levelColor(g.depLevel))
                            Text(g.depTitle ?? "").font(.caption).foregroundStyle(Color.wInk).lineLimit(1)
                        } else {
                            Text("—").font(.title3.weight(.bold)).foregroundStyle(Color.wMuted)
                            Text("no travel today").font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Divider()
                    rightNowColumn
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                nextRow
                Spacer(minLength: 0)
                selfCareLine
            }
        }
    }

    // The "RIGHT NOW" column: what's actually in front of you this moment. An
    // active outing or focus session is the most literal answer — you're doing it
    // right now — so it leads, carrying its one-tap end action. Absent that, the
    // server's concrete "you can do this now" suggestion (a real initiation nudge,
    // not a "N fit" count), then the open window, then a calm all-clear. The next
    // commitment lives in `nextRow` below, so this heading always means the same
    // thing.
    @ViewBuilder private var rightNowColumn: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("RIGHT NOW").font(.caption2).foregroundStyle(Color.wMuted)
            if let intention = g.outingIntention {
                Text(intention).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
                actionButton("I'm back", systemImage: "house", intent: ImBackIntent())
            } else if let task = g.focusTask {
                Text(task).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
                actionButton("Wrap up", systemImage: "flag.checkered", intent: EndFocusIntent())
            } else if let task = g.suggestionTitle {
                Text(task).font(.subheadline.weight(.semibold)).foregroundStyle(Color.wInk).lineLimit(2)
                Text(rightNowSub).font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
            } else if g.freeMinutes > 0 {
                Text("\(g.freeMinutes)m free").font(.title3.weight(.bold)).foregroundStyle(Color.wInk)
                Text("nothing queued").font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
            } else {
                Text("All clear").font(.title3.weight(.bold)).foregroundStyle(Color.wInk)
                Text(g.nextTitle == nil ? "nothing scheduled" : "you're on top of it")
                    .font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
            }
        }
    }

    // "~15 min · 45m free" — the estimate to start it, and the window it fits into.
    private var rightNowSub: String {
        var parts: [String] = []
        if let m = g.suggestionMinutes { parts.append("~\(m) min") }
        if g.freeMinutes > 0 { parts.append("\(g.freeMinutes)m free") }
        return parts.isEmpty ? "you can start now" : parts.joined(separator: " · ")
    }

    // Full-width "Next: … · 3:40 PM" footer — the upcoming commitment, kept out of
    // the RIGHT NOW column so that heading doesn't double as "what's next".
    @ViewBuilder private var nextRow: some View {
        if let t = g.nextTitle {
            HStack(spacing: 5) {
                Image(systemName: "calendar").font(.caption2).foregroundStyle(Color.wMuted)
                Text("Next: \(t)").font(.caption).foregroundStyle(Color.wMuted).lineLimit(1)
                Spacer(minLength: 4)
                if let at = g.nextAt {
                    Text(nextWhen(at)).font(.caption).foregroundStyle(Color.wMuted)
                }
            }
        }
    }

    // The next commitment's time — bare "3:40 PM" when it's today, but prefixed
    // with the weekday ("Wed 9:00 AM") when it isn't, so a commitment days out
    // doesn't read as this morning. Mirrors the Today card's PFDate.dayTime.
    private func nextWhen(_ date: Date) -> String {
        let f = DateFormatter()
        if Calendar.current.isDateInToday(date) {
            f.timeStyle = .short; f.dateStyle = .none
        } else {
            f.setLocalizedDateFormatFromTemplate("EEE h:mm a")
        }
        return f.string(from: date)
    }

    private func actionButton<I: AppIntent>(_ title: String, systemImage: String, intent: I) -> some View {
        Button(intent: intent) {
            Label(title, systemImage: systemImage)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color.wGreen)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Color.wGreen.opacity(0.15), in: Capsule())
        }
        .buttonStyle(.plain)
    }

    // Interactive self-care: tapping logs a meal / glass of water via
    // `MarkSelfCareIntent` and WidgetKit reloads the timeline (iOS 17+). Only
    // the Home Screen families render these; the Lock Screen accessories don't
    // call `selfCareLine`, and interactive buttons aren't supported there.
    @ViewBuilder private var selfCareLine: some View {
        if g.meal != nil || g.water != nil {
            HStack(spacing: 8) {
                if let m = g.meal { scButton(key: "meal", icon: "fork.knife", count: m.0, target: m.1) }
                if let w = g.water { scButton(key: "water", icon: "drop.fill", count: w.0, target: w.1) }
            }
        }
    }

    private func scButton(key: String, icon: String, count: Int, target: Int) -> some View {
        // At the target, a tap wraps the count back to zero (meal/water are both
        // quota checks) — the tap-at-max cycle the Me tab and web dashboard use,
        // rather than overshooting past the daily target.
        let done = count >= target
        return Button(intent: MarkSelfCareIntent(key: key, reset: done)) {
            HStack(spacing: 4) {
                Image(systemName: done ? "checkmark" : icon)
                Text("\(count)/\(target)").monospacedDigit()
            }
            .font(.caption2.weight(.semibold))
            .foregroundStyle(Color.wGreen)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Color.wGreen.opacity(0.15), in: Capsule())
        }
        .buttonStyle(.plain)
    }

    private var accRect: some View {
        VStack(alignment: .leading, spacing: 1) {
            if g.notConfigured {
                Text("Prefrontal").font(.headline); Text("Tap to connect").font(.caption)
            } else if let leave = g.depLeaveBy {
                Text("Leave \(leave.formatted(date: .omitted, time: .shortened))").font(.headline)
                Text(g.depTitle ?? "").font(.caption).lineLimit(1)
            } else if let task = g.suggestionTitle {
                Text(task).font(.headline).lineLimit(1)
                Text(g.suggestionMinutes.map { "~\($0) min" } ?? "\(g.freeMinutes) min free").font(.caption).lineLimit(1)
            } else if g.freeMinutes > 0 {
                Text("\(g.freeMinutes) min free").font(.headline)
                if let t = g.nextTitle { Text("next: \(t)").font(.caption).lineLimit(1) }
            } else {
                Text("All clear").font(.headline)
                if let t = g.nextTitle { Text("next: \(t)").font(.caption).lineLimit(1) }
            }
        }
        .widgetAccentable()
    }

    // The circular self-care ring moved to its own configurable widget
    // (`SelfCareCircleWidget.swift`) so you can pick which check it shows.

    private var accInline: some View {
        Group {
            if let leave = g.depLeaveBy {
                Label("Leave \(leave.formatted(date: .omitted, time: .shortened))", systemImage: "figure.walk")
            } else if let task = g.suggestionTitle {
                Label(task, systemImage: "checklist")
            } else if g.freeMinutes > 0 {
                Label("\(g.freeMinutes)m free", systemImage: "checklist")
            } else {
                Label("Prefrontal: all clear", systemImage: "checkmark")
            }
        }
    }
}

// MARK: - Widget

struct PrefrontalWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PrefrontalGlance", provider: Provider()) { entry in
            PrefrontalWidgetView(entry: entry)
        }
        .configurationDisplayName("Prefrontal")
        .description("Your next departure, the one thing to do now, and tap-to-log self-care.")
        .supportedFamilies([.systemSmall, .systemMedium,
                            .accessoryRectangular, .accessoryInline])
    }
}

@main
struct PrefrontalWidgetBundle: WidgetBundle {
    var body: some Widget {
        PrefrontalWidget()
        OneNextThingWidget()         // "one next thing" — the single honest next action
        PrefrontalSelfCareCircle()   // configurable Lock Screen self-care ring
        SessionLiveActivity()        // outing/focus Live Activity (Lock Screen + Dynamic Island)
        // Control Center controls (Capture / Panic / I'm Back / Wrap Up Focus) — iOS 18+.
        if #available(iOS 18.0, *) {
            CaptureThoughtControl()
            PanicControl()
            ImBackControl()
            WrapUpFocusControl()
        }
    }
}
