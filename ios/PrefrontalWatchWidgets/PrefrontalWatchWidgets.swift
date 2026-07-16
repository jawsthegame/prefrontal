import WidgetKit
import SwiftUI

/// Watch face complications for Prefrontal. They render from the last
/// `WatchGlance` the watch app cached to the shared App Group
/// (`WatchGlanceCache`) — the extension does no networking of its own; the app
/// reloads these timelines each time it refreshes.

struct ComplicationEntry: TimelineEntry {
    let date: Date
    let glance: WatchGlance?
}

struct ComplicationProvider: TimelineProvider {
    func placeholder(in context: Context) -> ComplicationEntry {
        ComplicationEntry(date: Date(), glance: .sample)
    }

    func getSnapshot(in context: Context, completion: @escaping (ComplicationEntry) -> Void) {
        let glance = context.isPreview ? .sample : WatchGlanceCache.read()
        completion(ComplicationEntry(date: Date(), glance: glance))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ComplicationEntry>) -> Void) {
        let entry = ComplicationEntry(date: Date(), glance: WatchGlanceCache.read())
        // The watch app reloads us after each refresh; poll occasionally as a floor.
        let next = Date().addingTimeInterval(30 * 60)
        completion(Timeline(entries: [entry], policy: .after(next)))
    }
}

struct ComplicationView: View {
    @Environment(\.widgetFamily) private var family
    let entry: ComplicationEntry

    private var g: WatchGlance? { entry.glance }
    /// The self-care check the circular ring tracks: water if present, else the
    /// first enabled check.
    private var ringCheck: WatchGlance.WatchCheck? {
        g?.selfCare.first { $0.key == "water" } ?? g?.selfCare.first
    }

    var body: some View {
        switch family {
        case .accessoryInline: inline
        case .accessoryCircular: circular
        case .accessoryRectangular: rectangular
        default: inline
        }
    }

    @ViewBuilder private var inline: some View {
        if let intention = g?.outingIntention {
            Label(intention, systemImage: "cup.and.saucer")
        } else if let task = g?.focusTask {
            Label(task, systemImage: "scope")
        } else if let leave = PFDate.parse(g?.departureLeaveBy) {
            Label("Leave \(leave.formatted(date: .omitted, time: .shortened))", systemImage: "figure.walk")
        } else if let task = g?.suggestionTitle {
            Label(task, systemImage: "checklist")
        } else if let free = g?.freeMinutes, free > 0 {
            Label("\(free)m free", systemImage: "checklist")
        } else {
            Label("All clear", systemImage: "checkmark")
        }
    }

    @ViewBuilder private var circular: some View {
        if g?.outingIntention != nil {
            // You're out — the most literal "right now"; no time to show, just the state.
            Image(systemName: "cup.and.saucer").font(.title3).widgetAccentable()
        } else if g?.focusTask != nil {
            Image(systemName: "scope").font(.title3).widgetAccentable()
        } else if let leave = PFDate.parse(g?.departureLeaveBy) {
            // Time-to-leave is the most urgent glance when travel is pending.
            VStack(spacing: 0) {
                Image(systemName: "figure.walk").font(.caption2)
                Text(leave, style: .time).font(.caption2).minimumScaleFactor(0.6)
            }
            .widgetAccentable()
        } else if let c = ringCheck {
            Gauge(value: Double(c.count), in: 0...Double(max(c.target, 1))) {
                Image(systemName: WatchSelfCare.symbol(c.key))
            } currentValueLabel: {
                Text("\(c.count)")
            }
            .gaugeStyle(.accessoryCircular)
            .widgetAccentable()
        } else {
            Image(systemName: "brain.head.profile").widgetAccentable()
        }
    }

    @ViewBuilder private var rectangular: some View {
        VStack(alignment: .leading, spacing: 1) {
            if let intention = g?.outingIntention {
                Text("OUT").font(.caption2).widgetAccentable()
                Text(intention).font(.headline).lineLimit(2)
            } else if let task = g?.focusTask {
                Text("FOCUS").font(.caption2).widgetAccentable()
                Text(task).font(.headline).lineLimit(2)
            } else if let leave = PFDate.parse(g?.departureLeaveBy) {
                Text("Leave \(leave.formatted(date: .omitted, time: .shortened))")
                    .font(.headline).widgetAccentable()
                if let t = g?.departureTitle { Text(t).font(.caption).lineLimit(1) }
            } else if let task = g?.suggestionTitle {
                Text("Do now").font(.caption2).widgetAccentable()
                Text(task).font(.headline).lineLimit(2)
            } else if let free = g?.freeMinutes, free > 0 {
                Text("\(free) min free").font(.headline).widgetAccentable()
                if let t = g?.nextTitle { Text("next: \(t)").font(.caption).lineLimit(1) }
            } else {
                Text("All clear").font(.headline).widgetAccentable()
                if let t = g?.nextTitle { Text("next: \(t)").font(.caption).lineLimit(1) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct PrefrontalComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PrefrontalComplication", provider: ComplicationProvider()) { entry in
            ComplicationView(entry: entry)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Prefrontal")
        .description("Your next leave-by time, the one thing to do now, and self-care progress.")
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular])
    }
}

@main
struct PrefrontalWatchWidgetBundle: WidgetBundle {
    var body: some Widget {
        PrefrontalComplication()
    }
}
