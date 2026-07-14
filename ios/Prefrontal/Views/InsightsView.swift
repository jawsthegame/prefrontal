import SwiftUI

/// Behavioral insights — the native window onto the learning loop's "it gets
/// better the longer you use it" story. Rolls up `GET /stats/data` (estimate
/// bias, follow-through, channel responsiveness, self-care adherence, feature
/// usage) and `GET /balance` (out-of-home time per life-domain) into glanceable
/// cards. Both are pure reads, so pull-to-refresh is the whole interaction.
/// Reached from the Me tab.
struct InsightsView: View {
    @State private var stats: Stats?
    @State private var balance: FocusBalance?
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let s = stats {
                    if s.counts.episodes == 0 {
                        emptyState
                    } else {
                        if s.timeEstimation.n > 0 { estimationCard(s.timeEstimation) }
                        if s.followThrough.n > 0 { followThroughCard(s.followThrough) }
                        if let b = balance, !b.domains.isEmpty { balanceCard(b) }
                        if !s.channels.isEmpty { channelsCard(s.channels) }
                        selfCareCard(s.selfCare)
                        featureUsageCard(s.featureUsage)
                    }
                } else if error == nil {
                    Card { Text("…").foregroundStyle(Brand.muted) }
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Insights")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
    }

    private var emptyState: some View {
        Card {
            VStack(spacing: 8) {
                Image(systemName: "chart.line.uptrend.xyaxis")
                    .font(.largeTitle).foregroundStyle(Brand.muted)
                Text("Not enough history yet").font(.headline).foregroundStyle(Brand.nearWhite)
                Text("As Prefrontal watches how your days go, your estimate accuracy, follow-through, and balance will show up here.")
                    .font(.footnote).foregroundStyle(Brand.muted)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
    }

    // MARK: - Time estimation

    private func estimationCard(_ te: Stats.TimeEstimation) -> some View {
        Card {
            CardLabel(text: "Time estimation")
            Text(estimationPhrase(te)).font(.headline).foregroundStyle(Brand.nearWhite)
            Text("from \(te.n) tracked \(te.n == 1 ? "estimate" : "estimates")")
                .font(.caption).foregroundStyle(Brand.muted)
            if !te.contexts.isEmpty {
                Divider().overlay(Brand.line)
                ForEach(te.contexts.prefix(4)) { ctx in
                    HStack {
                        Text(ctx.context.capitalized).font(.subheadline).foregroundStyle(Brand.fg)
                        Spacer()
                        if let r = ctx.ratio {
                            Chip(text: "×\(r.formatted())", color: directionColor(ctx.direction))
                        }
                    }
                }
            }
        }
    }

    // MARK: - Follow-through

    private func followThroughCard(_ ft: Stats.FollowThrough) -> some View {
        Card {
            CardLabel(text: "Follow-through")
            HStack(alignment: .firstTextBaseline) {
                Text(ft.rate != nil ? "\(pct(ft.rate))%" : "—")
                    .font(.title.weight(.bold)).foregroundStyle(rateColor(ft.rate))
                Text("follow-through").font(.subheadline).foregroundStyle(Brand.muted)
                Spacer()
                if ft.streak > 0 {
                    Chip(text: "🔥 \(ft.streak) in a row", color: Brand.good)
                }
            }
            HStack(spacing: 10) {
                splitTag("\(ft.counts.success)", "done", Brand.good)
                splitTag("\(ft.counts.partial)", "partial", Brand.warn)
                splitTag("\(ft.counts.miss)", "missed", Brand.danger)
            }
            if !ft.series.isEmpty {
                HStack(spacing: 2) {
                    ForEach(Array(ft.series.enumerated()), id: \.offset) { _, outcome in
                        RoundedRectangle(cornerRadius: 2)
                            .fill(outcomeColor(outcome)).frame(height: 18)
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    private func splitTag(_ value: String, _ label: String, _ color: Color) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(value).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
            Text(label).font(.caption).foregroundStyle(Brand.muted)
        }
    }

    // MARK: - Focus balance

    private func balanceCard(_ b: FocusBalance) -> some View {
        Card {
            HStack {
                CardLabel(text: "Focus balance")
                Spacer()
                Text("last \(b.days) days").font(.caption2).foregroundStyle(Brand.muted)
            }
            if let s = b.summary, !s.isEmpty {
                Text(s).font(.subheadline).foregroundStyle(Brand.nearWhite)
            }
            ForEach(b.domains) { d in
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        DomainPill(text: d.domain)
                        Spacer()
                        Text(hm(d.minutes)).font(.caption.weight(.medium))
                            .foregroundStyle(d.underserved ? Brand.warn : Brand.muted)
                            .monospacedDigit()
                    }
                    MeterBar(fraction: domainFraction(d),
                             color: d.underserved ? Brand.warn : Brand.accent)
                }
            }
            if let hint = b.hint, !hint.isEmpty {
                Text(hint).font(.caption).foregroundStyle(Brand.muted)
            }
        }
    }

    // MARK: - Channels

    private func channelsCard(_ channels: [Stats.Channel]) -> some View {
        Card {
            CardLabel(text: "Which channel you answer")
            ForEach(channels) { c in
                HStack {
                    Text(c.channel.capitalized).font(.subheadline).foregroundStyle(Brand.fg)
                    Spacer()
                    Text("\(c.acked)/\(c.n)").font(.caption).foregroundStyle(Brand.muted).monospacedDigit()
                    Text(c.rate != nil ? "\(pct(c.rate))%" : "—")
                        .font(.subheadline.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(rateColor(c.rate))
                        .frame(width: 48, alignment: .trailing)
                }
            }
        }
    }

    // MARK: - Self-care adherence

    @ViewBuilder
    private func selfCareCard(_ rows: [Stats.SelfCareStat]) -> some View {
        let shown = rows.filter { $0.enabled || $0.n > 0 }
        if !shown.isEmpty {
            Card {
                CardLabel(text: "Self-care adherence")
                ForEach(shown) { row in
                    HStack(alignment: .firstTextBaseline) {
                        Text(selfCareLabel(row.key)).font(.subheadline).foregroundStyle(Brand.fg)
                        Spacer()
                        VStack(alignment: .trailing, spacing: 1) {
                            Text("~\(row.avgPerDay.formatted())/day of \(row.target)")
                                .font(.caption).foregroundStyle(Brand.muted).monospacedDigit()
                            if let lat = row.avgLatencySeconds {
                                Text("\(latencyText(lat)) to tap")
                                    .font(.caption2).foregroundStyle(Brand.muted)
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - Feature usage

    private func featureUsageCard(_ fu: Stats.FeatureUsage) -> some View {
        Card {
            CardLabel(text: "Feature usage")
            HStack(spacing: 10) {
                splitTag("\(fu.summary.using)", "in use", Brand.good)
                splitTag("\(fu.summary.ignored)", "ignored", Brand.warn)
                splitTag("\(fu.summary.dormant)", "dormant", Brand.muted)
            }
            if fu.summary.muted > 0 {
                Text("\(fu.summary.muted) muted").font(.caption).foregroundStyle(Brand.muted)
            }
        }
    }

    // MARK: - Formatting helpers

    private func estimationPhrase(_ te: Stats.TimeEstimation) -> String {
        guard let ratio = te.ratio else { return "Not enough estimates yet" }
        switch te.direction {
        case "over": return "You run ~\(ratio.formatted())× over your estimates"
        case "under": return "You finish ~\(ratio.formatted())× under your estimates"
        default: return "Your estimates are about right"
        }
    }

    private func pct(_ rate: Double?) -> Int { Int(((rate ?? 0) * 100).rounded()) }

    /// A domain's fill toward its (window-scaled) target, or 0 when untargeted.
    private func domainFraction(_ d: FocusBalance.Domain) -> Double {
        guard let target = d.targetMinutes, target > 0 else { return 0 }
        return d.minutes / target
    }

    private func hm(_ minutes: Double) -> String {
        let total = Int(minutes.rounded())
        if total < 60 { return "\(total)m" }
        let h = total / 60, m = total % 60
        return m == 0 ? "\(h)h" : "\(h)h \(m)m"
    }

    private func latencyText(_ seconds: Double) -> String {
        seconds < 90 ? "~\(Int(seconds.rounded()))s" : "~\(Int((seconds / 60).rounded()))m"
    }

    private func directionColor(_ direction: String?) -> Color {
        switch direction {
        case "over": return Brand.warn
        case "under": return Brand.fyi
        default: return Brand.good
        }
    }

    private func outcomeColor(_ outcome: String) -> Color {
        switch outcome {
        case "success": return Brand.good
        case "partial": return Brand.warn
        default: return Brand.danger
        }
    }

    private func rateColor(_ rate: Double?) -> Color {
        guard let rate else { return Brand.muted }
        if rate >= 0.66 { return Brand.good }
        if rate >= 0.33 { return Brand.warn }
        return Brand.danger
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let s = client.stats()
            async let b = client.focusBalance(days: 7)
            stats = try await s
            // Focus balance is best-effort: it's empty/erroring when trip tracking
            // is off, and shouldn't blank the rest of the insights.
            balance = try? await b
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// A thin horizontal meter that fills left→right toward a target (clamped 0–1).
private struct MeterBar: View {
    let fraction: Double
    var color: Color = Brand.accent

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Brand.chip)
                Capsule().fill(color)
                    .frame(width: max(0, min(1, fraction)) * geo.size.width)
            }
        }
        .frame(height: 6)
    }
}
