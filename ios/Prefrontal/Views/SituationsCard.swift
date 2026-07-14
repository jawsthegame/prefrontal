import SwiftUI

/// The enabled Context Packs' **situation tools** — on-demand, read-only
/// questions a pack answers from your live data (the Parent pack's School run,
/// Pack the bag, Sick-day replan). Mirrors the web dashboard's Situations card:
/// self-loads `GET /packs/situations` and **stays hidden** when no pack
/// contributes tools, so it's invisible unless a pack like `parent` is enabled.
/// Tapping **Check** runs one (`POST /packs/situations/{tool}`) and renders its
/// tool-specific result inline. Embedded on the Today tab.
struct SituationsCard: View {
    @State private var tools: [SituationTool] = []
    @State private var results: [String: SituationResult] = [:]
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        Group {
            if !tools.isEmpty {
                Card {
                    CardLabel(text: "Situations")
                    if let error { Text(error).font(.caption).foregroundStyle(Brand.danger) }
                    ForEach(Array(tools.enumerated()), id: \.element.id) { idx, t in
                        if idx > 0 { Divider().overlay(Brand.line) }
                        toolRow(t)
                    }
                }
            }
        }
        .task { if !loaded { await load() } }
    }

    private func toolRow(_ t: SituationTool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.title).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                    if let d = t.description, !d.isEmpty {
                        Text(d).font(.caption).foregroundStyle(Brand.muted)
                    }
                }
                Spacer(minLength: 8)
                AsyncButton {
                    let r = try await withAPI { try await $0.runSituation(tool: t.tool) }
                    results[t.tool] = r
                    error = nil
                } label: {
                    Text("Check").font(.caption)
                } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.accent)
            }
            if let r = results[t.tool] { result(r) }
        }
    }

    @ViewBuilder
    private func result(_ r: SituationResult) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let h = r.headline, !h.isEmpty {
                Text(h).font(.footnote).foregroundStyle(Brand.fg)
            }
            // School-run departures (each pre-phrased) and sick-day pressing items.
            ForEach(Array((r.departures ?? []).enumerated()), id: \.offset) { _, d in
                line(d.message ?? d.title, sub: leaveSub(d))
            }
            // Pack-the-bag get-ready checklists.
            ForEach(Array((r.checklists ?? []).enumerated()), id: \.offset) { _, c in
                checklist(c)
            }
            ForEach(Array((r.pressing ?? []).enumerated()), id: \.offset) { _, p in
                line(p.title, sub: p.startAt.map { PFDate.dayTime($0) })
            }
            // Sick-day's single first step. Shown alongside its `pressing` list, but
            // suppressed when a departures/checklists tool already leads with steps.
            if let fs = r.firstStep, !fs.isEmpty,
               (r.departures?.isEmpty ?? true), (r.checklists?.isEmpty ?? true) {
                Text("First step: \(fs)").font(.footnote).foregroundStyle(Brand.fg)
            }
            if isEmpty(r) {
                Text("Nothing to report right now.").font(.caption).foregroundStyle(Brand.muted)
            }
        }
        .padding(.top, 2)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func line(_ primary: String?, sub: String?) -> some View {
        if let primary, !primary.isEmpty {
            HStack(alignment: .top, spacing: 8) {
                Circle().fill(Brand.accent).frame(width: 6, height: 6).padding(.top, 5)
                VStack(alignment: .leading, spacing: 1) {
                    Text(primary).font(.footnote).foregroundStyle(Brand.nearWhite)
                    if let sub, !sub.isEmpty { Text(sub).font(.caption2).foregroundStyle(Brand.muted) }
                }
                Spacer(minLength: 0)
            }
        }
    }

    private func checklist(_ c: SituationResult.Checklist) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            if let ti = c.title, !ti.isEmpty {
                Text(ti).font(.footnote.weight(.medium)).foregroundStyle(Brand.nearWhite)
            }
            if let fs = c.firstStep, !fs.isEmpty {
                Text("Start: \(fs)").font(.caption).foregroundStyle(Brand.muted)
            }
            ForEach(Array((c.steps ?? []).enumerated()), id: \.offset) { _, s in
                Text("• \(s)").font(.caption).foregroundStyle(Brand.muted)
            }
        }
    }

    private func leaveSub(_ d: SituationResult.Item) -> String? {
        if let lb = d.leaveBy {
            let t = PFDate.time(lb)
            if !t.isEmpty { return "leave by \(t)" }
        }
        return d.location
    }

    private func isEmpty(_ r: SituationResult) -> Bool {
        (r.headline?.isEmpty ?? true)
            && (r.departures?.isEmpty ?? true)
            && (r.checklists?.isEmpty ?? true)
            && (r.pressing?.isEmpty ?? true)
            && (r.firstStep?.isEmpty ?? true)
    }

    private func load() async {
        tools = (try? await withAPI { try await $0.situations() }) ?? []
        loaded = true
    }
}
