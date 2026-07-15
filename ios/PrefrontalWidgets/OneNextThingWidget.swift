import WidgetKit
import SwiftUI
import AppIntents

// MARK: - One next thing
//
// "One next thing," always honest. Where the main Prefrontal glance packs
// departure + a suggestion + self-care into one tile, this widget shows the
// single next action and nothing else — the mid-flight task you're in, a
// commitment to leave for, the worst clock-bound fire, or the
// avoided-but-important todo you keep skipping. Everything else collapses into a
// single "+N more can wait" line. Overwhelm is an indictment; one thing is an
// invitation. It reads `GET /next` (server-side honest prioritization; see
// `prefrontal/next_thing.py`), so the phone never re-ranks anything.

/// The one thing to show, resolved by the server. `notConfigured` when the app
/// hasn't been connected yet (the App Group has no base URL / token).
struct NextThingSnapshot {
    var notConfigured = false
    var thing: NextThing?

    static let sample = NextThingSnapshot(thing: .sample)

    @MainActor
    static func fetch() async -> NextThingSnapshot {
        let client: APIClient
        do { client = try APIClient(shared: ()) }
        catch { return NextThingSnapshot(notConfigured: true) }
        guard let thing = try? await client.nextThing() else {
            // A transient read failure isn't "unconfigured" — show a calm blank
            // rather than a scary "tap to connect" on an app that *is* connected.
            return NextThingSnapshot(thing: nil)
        }
        return NextThingSnapshot(thing: thing)
    }
}

// MARK: - Timeline

struct NextThingEntry: TimelineEntry {
    let date: Date
    let snapshot: NextThingSnapshot
}

struct NextThingProvider: TimelineProvider {
    func placeholder(in context: Context) -> NextThingEntry {
        NextThingEntry(date: Date(), snapshot: .sample)
    }
    func getSnapshot(in context: Context, completion: @escaping (NextThingEntry) -> Void) {
        if context.isPreview { completion(NextThingEntry(date: Date(), snapshot: .sample)); return }
        Task { completion(NextThingEntry(date: Date(), snapshot: await NextThingSnapshot.fetch())) }
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<NextThingEntry>) -> Void) {
        Task {
            let snap = await NextThingSnapshot.fetch()
            let next = Date().addingTimeInterval(15 * 60)
            completion(Timeline(entries: [NextThingEntry(date: Date(), snapshot: snap)],
                                policy: .after(next)))
        }
    }
}

// MARK: - Presentation

// Brand palette via the same archive-safe asset color sets the main glance uses.
private extension Color {
    static let ntPaper = Color("WidgetPaper")
    static let ntInk = Color("WidgetInk")
    static let ntMuted = Color("WidgetMuted")
    static let ntGreen = Color("WidgetGreen")
}

/// How one next thing is dressed: an SF Symbol, a short tag, and a tint — keyed
/// off `reason` first (it's the honest "why"), then `kind`. Pure display.
private struct NextThingStyle {
    let symbol: String
    let tag: String
    let tint: Color

    // A red/amber tint only for the genuinely clock-bound cases; avoided/fits/
    // mid-flight stay calm and inky, so the widget never nags a non-fire.
    static func of(_ t: NextThing) -> NextThingStyle {
        switch t.reason {
        case "leave-now":
            return .init(symbol: "figure.walk.departure", tag: "LEAVE NOW",
                         tint: Color(red: 0.75, green: 0.20, blue: 0.29))
        case "leave-soon":
            return .init(symbol: "figure.walk", tag: "HEAD OUT SOON",
                         tint: Color(red: 0.76, green: 0.34, blue: 0.12))
        case "mid-flight":
            return t.kind == "outing"
                ? .init(symbol: "cup.and.saucer", tag: "YOU'RE OUT", tint: .ntInk)
                : .init(symbol: "scope", tag: "MID-FLIGHT", tint: .ntInk)
        case "overdue":
            return .init(symbol: "exclamationmark.circle", tag: "OVERDUE",
                         tint: Color(red: 0.75, green: 0.20, blue: 0.29))
        case "due-soon":
            return .init(symbol: "clock", tag: "DUE SOON",
                         tint: Color(red: 0.76, green: 0.34, blue: 0.12))
        case "waiting":
            return .init(symbol: "person.wave.2", tag: "WAITING ON YOU", tint: .ntInk)
        case "urgent-mail":
            return .init(symbol: "envelope.badge", tag: "URGENT MAIL",
                         tint: Color(red: 0.76, green: 0.34, blue: 0.12))
        case "avoided":
            return .init(symbol: "arrow.uturn.down", tag: "KEEP SKIPPING", tint: .ntInk)
        case "fits":
            return .init(symbol: "checklist", tag: "DO NOW", tint: .ntInk)
        default: // clear
            return .init(symbol: "checkmark.circle", tag: "ALL CLEAR", tint: .ntGreen)
        }
    }
}

struct OneNextThingView: View {
    @Environment(\.widgetFamily) var family
    let entry: NextThingEntry
    private var snap: NextThingSnapshot { entry.snapshot }

    private var isSystem: Bool {
        family == .systemSmall || family == .systemMedium
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .widgetURL(URL(string: "prefrontal://today"))
            // Plain Color per branch — no AnyView, or WidgetKit misses the
            // container background and renders blank (see the main glance).
            .containerBackground(for: .widget) {
                isSystem ? Color.ntPaper : Color.clear
            }
    }

    @ViewBuilder private var content: some View {
        if snap.notConfigured {
            notConfigured
        } else if let t = snap.thing {
            switch family {
            case .systemMedium: medium(t)
            case .accessoryRectangular: accRect(t)
            case .accessoryInline: accInline(t)
            case .accessoryCircular: accCircular(t)
            default: small(t)
            }
        } else {
            // Transient read failure — stay calm, don't flash an error.
            calmBlank
        }
    }

    // MARK: Home Screen

    private var header: some View {
        HStack(spacing: 5) {
            Image(systemName: "brain.head.profile").font(.caption2).foregroundStyle(Color.ntGreen)
            Text("Next thing").font(.caption2.weight(.bold)).foregroundStyle(Color.ntMuted)
        }
    }

    private func small(_ t: NextThing) -> some View {
        let style = NextThingStyle.of(t)
        return VStack(alignment: .leading, spacing: 4) {
            header
            Spacer(minLength: 2)
            HStack(spacing: 4) {
                Image(systemName: style.symbol).font(.caption2)
                Text(style.tag).font(.caption2.weight(.bold))
            }
            .foregroundStyle(style.tint)
            Text(t.title)
                .font(.headline.weight(.semibold)).foregroundStyle(Color.ntInk)
                .lineLimit(3).minimumScaleFactor(0.8)
            if t.kind != "clear" {
                Text(t.detail).font(.caption).foregroundStyle(Color.ntMuted).lineLimit(1)
            }
            Spacer(minLength: 2)
            actionOrRest(t)
        }
    }

    private func medium(_ t: NextThing) -> some View {
        let style = NextThingStyle.of(t)
        return VStack(alignment: .leading, spacing: 6) {
            header
            Spacer(minLength: 0)
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: style.symbol)
                    .font(.title2).foregroundStyle(style.tint)
                    .frame(width: 34)
                VStack(alignment: .leading, spacing: 2) {
                    Text(style.tag).font(.caption2.weight(.bold)).foregroundStyle(style.tint)
                    Text(t.title)
                        .font(.title3.weight(.semibold)).foregroundStyle(Color.ntInk)
                        .lineLimit(2).minimumScaleFactor(0.8)
                    if t.kind != "clear" {
                        Text(t.detail).font(.caption).foregroundStyle(Color.ntMuted).lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                inlineAction(t)
            }
            Spacer(minLength: 0)
            restLine(t)
        }
    }

    // The mid-flight one-tap end action (Home Screen only — Lock Screen
    // accessories can't host interactive buttons). Otherwise nothing: a todo /
    // departure has no safe one-tap here, so we just show the "rest" line.
    @ViewBuilder private func actionOrRest(_ t: NextThing) -> some View {
        if t.action == "wrap_up" {
            actionButton("Wrap up", systemImage: "flag.checkered", intent: EndFocusIntent())
        } else if t.action == "im_back" {
            actionButton("I'm back", systemImage: "house", intent: ImBackIntent())
        } else {
            restLine(t)
        }
    }

    @ViewBuilder private func inlineAction(_ t: NextThing) -> some View {
        if t.action == "wrap_up" {
            actionButton("Wrap up", systemImage: "flag.checkered", intent: EndFocusIntent())
        } else if t.action == "im_back" {
            actionButton("I'm back", systemImage: "house", intent: ImBackIntent())
        }
    }

    // "+3 more can wait" — the whole point: name the mountain's size once, then
    // withhold it. Absent when this really is the only thing.
    @ViewBuilder private func restLine(_ t: NextThing) -> some View {
        if let n = t.alsoCount, n > 0 {
            Text("+\(n) more can wait")
                .font(.caption2).foregroundStyle(Color.ntMuted).lineLimit(1)
        }
    }

    private func actionButton<I: AppIntent>(_ title: String, systemImage: String, intent: I) -> some View {
        Button(intent: intent) {
            Label(title, systemImage: systemImage)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color.ntGreen)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Color.ntGreen.opacity(0.15), in: Capsule())
        }
        .buttonStyle(.plain)
    }

    // MARK: Lock Screen accessories (tinted rendering, no custom colors)

    private func accRect(_ t: NextThing) -> some View {
        let style = NextThingStyle.of(t)
        return VStack(alignment: .leading, spacing: 1) {
            Label(style.tag, systemImage: style.symbol)
                .font(.caption2.weight(.bold)).labelStyle(.titleAndIcon)
            Text(t.title).font(.headline).lineLimit(2)
            if t.kind != "clear" {
                Text(t.detail).font(.caption).lineLimit(1)
            } else if let n = t.alsoCount, n > 0 {
                Text("+\(n) more can wait").font(.caption)
            }
        }
        .widgetAccentable()
    }

    private func accInline(_ t: NextThing) -> some View {
        Label(t.title, systemImage: NextThingStyle.of(t).symbol)
    }

    private func accCircular(_ t: NextThing) -> some View {
        let style = NextThingStyle.of(t)
        return VStack(spacing: 1) {
            Image(systemName: style.symbol).font(.title3)
            if let n = t.alsoCount, n > 0 {
                Text("+\(n)").font(.caption2)
            }
        }
        .widgetAccentable()
    }

    // MARK: Fallbacks

    private var notConfigured: some View {
        VStack(alignment: .leading, spacing: 4) {
            header
            Spacer(minLength: 2)
            Text(isSystem ? "Open Prefrontal to connect this widget." : "Tap to connect")
                .font(isSystem ? .footnote : .caption)
                .foregroundStyle(Color.ntMuted)
        }
    }

    private var calmBlank: some View {
        VStack(alignment: .leading, spacing: 4) {
            header
            Spacer(minLength: 2)
            Text("Checking…").font(.callout).foregroundStyle(Color.ntMuted)
        }
    }
}

// MARK: - Widget

struct OneNextThingWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PrefrontalOneNextThing", provider: NextThingProvider()) { entry in
            OneNextThingView(entry: entry)
        }
        .configurationDisplayName("One next thing")
        .description("The single next action to do right now — never the whole list.")
        .supportedFamilies([.systemSmall, .systemMedium,
                            .accessoryRectangular, .accessoryInline, .accessoryCircular])
    }
}
