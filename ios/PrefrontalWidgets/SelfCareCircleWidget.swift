import WidgetKit
import SwiftUI
import AppIntents

/// A **configurable** Lock Screen circular self-care ring: long-press the widget
/// → Edit → pick which check it tracks (Water, Meals, …), instead of the old
/// hardcoded-water gauge. Configurable widgets use `AppIntentConfiguration` +
/// an `AppIntentTimelineProvider`, so the chosen check arrives as the intent's
/// parameter. See issue #465.

/// The self-care checks a ring can show. Kept to the known set so the picker is
/// a fixed list; the ring simply shows 0/0 for a check the user hasn't enabled.
enum SelfCareCheck: String, AppEnum {
    case meal, water, meds, biobreak, winddown, movement

    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Self-care check" }
    static var caseDisplayRepresentations: [SelfCareCheck: DisplayRepresentation] {
        [.meal: "Meals", .water: "Water", .meds: "Meds",
         .biobreak: "Breaks", .winddown: "Wind-down", .movement: "Movement"]
    }

    var symbol: String {
        switch self {
        case .meal: return "fork.knife"
        case .water: return "drop.fill"
        case .meds: return "pills.fill"
        case .biobreak: return "figure.walk"
        case .winddown: return "moon.fill"
        case .movement: return "figure.run"
        }
    }
}

/// The widget's edit-time configuration: which check the ring tracks.
struct SelfCareCircleConfig: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Self-care ring"
    static let description = IntentDescription("Choose which self-care check the ring shows.")

    @Parameter(title: "Check", default: .water)
    var check: SelfCareCheck
}

struct SelfCareEntry: TimelineEntry {
    let date: Date
    let check: SelfCareCheck
    let count: Int
    let target: Int
    let notConfigured: Bool
}

struct SelfCareCircleProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> SelfCareEntry {
        SelfCareEntry(date: Date(), check: .water, count: 3, target: 6, notConfigured: false)
    }

    func snapshot(for configuration: SelfCareCircleConfig, in context: Context) async -> SelfCareEntry {
        if context.isPreview {
            return SelfCareEntry(date: Date(), check: configuration.check, count: 3, target: 6, notConfigured: false)
        }
        return entry(from: await Glance.fetch(), configuration.check)
    }

    func timeline(for configuration: SelfCareCircleConfig, in context: Context) async -> Timeline<SelfCareEntry> {
        let e = entry(from: await Glance.fetch(), configuration.check)
        return Timeline(entries: [e], policy: .after(Date().addingTimeInterval(20 * 60)))
    }

    private func entry(from g: Glance, _ check: SelfCareCheck) -> SelfCareEntry {
        let ct = g.selfCareChecks[check.rawValue]
        return SelfCareEntry(date: Date(), check: check,
                             count: ct?.0 ?? 0, target: ct?.1 ?? 0,
                             notConfigured: g.notConfigured)
    }
}

struct SelfCareCircleView: View {
    let entry: SelfCareEntry
    var body: some View {
        Gauge(value: Double(entry.count), in: 0...Double(max(1, entry.target))) {
            Image(systemName: entry.check.symbol)
        } currentValueLabel: {
            Text("\(entry.count)")
        }
        .gaugeStyle(.accessoryCircular)
        .containerBackground(for: .widget) { Color.clear }
    }
}

struct PrefrontalSelfCareCircle: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(
            kind: "PrefrontalSelfCareCircle",
            intent: SelfCareCircleConfig.self,
            provider: SelfCareCircleProvider()
        ) { entry in
            SelfCareCircleView(entry: entry)
        }
        .configurationDisplayName("Self-care ring")
        .description("A Lock Screen ring for one self-care check — tap Edit to choose which.")
        .supportedFamilies([.accessoryCircular])
    }
}
