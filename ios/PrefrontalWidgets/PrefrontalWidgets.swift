import WidgetKit
import SwiftUI

// MARK: - Snapshot

/// The glance data shown on the widget. Assembled from the same endpoints the
/// Today tab uses, via the shared App Group config.
struct Glance {
    var notConfigured = false
    var depTitle: String?
    var depLeaveBy: Date?
    var depLevel: String?
    var freeMinutes = 0
    var fits = 0
    var nextTitle: String?
    var nextStart: Date?
    var meal: (Int, Int)?
    var water: (Int, Int)?

    static let sample = Glance(
        depTitle: "Dentist", depLeaveBy: Date().addingTimeInterval(45 * 60), depLevel: "soon",
        freeMinutes: 45, fits: 3,
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
        let dep = await depT, now = await nowT, sc = await scT

        var g = Glance()
        if let d = dep?.departure, d.title != nil {
            g.depTitle = d.title
            g.depLeaveBy = PFDate.parse(d.leaveBy)
            g.depLevel = d.level
        }
        if let now {
            g.freeMinutes = Int(now.freeMinutes ?? 0)
            g.nextTitle = now.nextCommitment?.title
            g.nextStart = PFDate.parse(now.nextCommitment?.startAt)
        }
        if g.freeMinutes > 0 {
            g.fits = (try? await client.todosFit(minutes: g.freeMinutes))?.fits.count ?? 0
        }
        if let checks = sc?.checks {
            if let m = checks.first(where: { $0.key == "meal" }) { g.meal = (m.count, m.target) }
            if let w = checks.first(where: { $0.key == "water" }) { g.water = (w.count, w.target) }
        }
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

struct PrefrontalWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: GlanceEntry
    var g: Glance { entry.glance }

    private var isSystem: Bool {
        family == .systemSmall || family == .systemMedium || family == .systemLarge
    }

    var body: some View {
        Group {
            switch family {
            case .systemSmall: small
            case .systemMedium: medium
            case .accessoryRectangular: accRect
            case .accessoryCircular: accCircle
            case .accessoryInline: accInline
            default: small
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .widgetURL(URL(string: "prefrontal://today"))
        // iOS 17 requires a container background; system widgets get the paper
        // surface, Lock Screen accessories stay transparent.
        .containerBackground(for: .widget) {
            isSystem ? Brand.bg : Color.clear
        }
    }

    // System small: the single most important thing + a self-care line.
    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            Spacer(minLength: 0)
            if g.notConfigured {
                Text("Tap to connect").font(.footnote).foregroundStyle(Brand.muted)
            } else if let leave = g.depLeaveBy {
                Text("Leave \(leave.formatted(date: .omitted, time: .shortened))")
                    .font(.title3.weight(.bold)).foregroundStyle(Brand.level(g.depLevel))
                Text(g.depTitle ?? "").font(.caption).foregroundStyle(Brand.fg).lineLimit(2)
            } else if g.freeMinutes > 0 {
                Text("\(g.fits) todo\(g.fits == 1 ? "" : "s") fit").font(.title3.weight(.bold)).foregroundStyle(Brand.fg)
                Text("\(g.freeMinutes) min free").font(.caption).foregroundStyle(Brand.muted)
            } else {
                Text("All clear").font(.title3.weight(.bold)).foregroundStyle(Brand.fg)
                if let t = g.nextTitle { Text("next: \(t)").font(.caption).foregroundStyle(Brand.muted).lineLimit(2) }
            }
            Spacer(minLength: 0)
            selfCareLine
        }
    }

    private var medium: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if g.notConfigured {
                Text("Open Prefrontal to connect this widget.").font(.footnote).foregroundStyle(Brand.muted)
            } else {
                HStack(alignment: .top, spacing: 14) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Leave by").font(.caption2).foregroundStyle(Brand.muted)
                        if let leave = g.depLeaveBy {
                            Text(leave.formatted(date: .omitted, time: .shortened))
                                .font(.title3.weight(.bold)).foregroundStyle(Brand.level(g.depLevel))
                            Text(g.depTitle ?? "").font(.caption).foregroundStyle(Brand.fg).lineLimit(1)
                        } else {
                            Text("—").font(.title3.weight(.bold)).foregroundStyle(Brand.muted)
                        }
                    }
                    Divider()
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Right now").font(.caption2).foregroundStyle(Brand.muted)
                        if g.freeMinutes > 0 {
                            Text("\(g.fits) fit \(g.freeMinutes)m").font(.title3.weight(.bold)).foregroundStyle(Brand.fg)
                        } else if let t = g.nextTitle {
                            Text(t).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.fg).lineLimit(2)
                        } else {
                            Text("clear").font(.title3.weight(.bold)).foregroundStyle(Brand.fg)
                        }
                    }
                    Spacer(minLength: 0)
                }
                Spacer(minLength: 0)
                selfCareLine
            }
        }
    }

    private var header: some View {
        HStack(spacing: 5) {
            Image(systemName: "brain.head.profile").font(.caption2).foregroundStyle(Brand.accent)
            Text("Prefrontal").font(.caption2.weight(.bold)).foregroundStyle(Brand.muted)
        }
    }

    @ViewBuilder private var selfCareLine: some View {
        if g.meal != nil || g.water != nil {
            HStack(spacing: 10) {
                if let m = g.meal { Label("\(m.0)/\(m.1)", systemImage: "fork.knife").labelStyle(.titleAndIcon) }
                if let w = g.water { Label("\(w.0)/\(w.1)", systemImage: "drop.fill") }
            }
            .font(.caption2).foregroundStyle(Brand.muted)
        }
    }

    // Lock Screen rectangular: two glanceable lines.
    private var accRect: some View {
        VStack(alignment: .leading, spacing: 1) {
            if g.notConfigured {
                Text("Prefrontal").font(.headline); Text("Tap to connect").font(.caption)
            } else if let leave = g.depLeaveBy {
                Text("Leave \(leave.formatted(date: .omitted, time: .shortened))").font(.headline)
                Text(g.depTitle ?? "").font(.caption).lineLimit(1)
            } else if g.freeMinutes > 0 {
                Text("\(g.fits) todos fit").font(.headline)
                Text("\(g.freeMinutes) min free").font(.caption)
            } else {
                Text("All clear").font(.headline)
                if let t = g.nextTitle { Text("next: \(t)").font(.caption).lineLimit(1) }
            }
        }
        .widgetAccentable()
    }

    // Lock Screen circular: water progress ring.
    private var accCircle: some View {
        let w = g.water ?? (0, 6)
        return Gauge(value: Double(w.0), in: 0...Double(max(1, w.1))) {
            Image(systemName: "drop.fill")
        } currentValueLabel: {
            Text("\(w.0)")
        }
        .gaugeStyle(.accessoryCircular)
    }

    private var accInline: some View {
        Group {
            if let leave = g.depLeaveBy {
                Label("Leave \(leave.formatted(date: .omitted, time: .shortened)) · \(g.depTitle ?? "")", systemImage: "figure.walk")
            } else if g.freeMinutes > 0 {
                Label("\(g.fits) todos fit \(g.freeMinutes)m", systemImage: "checklist")
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
        .description("Your next departure, what fits now, and self-care.")
        .supportedFamilies([.systemSmall, .systemMedium,
                            .accessoryRectangular, .accessoryCircular, .accessoryInline])
    }
}

@main
struct PrefrontalWidgetBundle: WidgetBundle {
    var body: some Widget { PrefrontalWidget() }
}
