import SwiftUI

/// The Today glance: the most actionable thing right now. Priority mirrors the
/// Home Screen widget — an active outing/focus (with its one-tap end), then the
/// next departure, then the one thing to do now, then the open window.
struct WatchTodayView: View {
    @EnvironmentObject private var model: WatchModel

    private var g: WatchGlance { model.glance }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                content
                if let err = model.errorText {
                    Text(err).font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 2)
        }
        .navigationTitle("Today")
        .refreshable { await model.refresh() }
        .overlay { if model.loading && g == .disconnected { ProgressView() } }
    }

    @ViewBuilder private var content: some View {
        if let intention = g.outingIntention {
            activeCard(tag: "OUT", title: intention,
                       button: "I'm back", systemImage: "house", kind: .imBack)
        } else if let task = g.focusTask {
            activeCard(tag: "FOCUS", title: task,
                       button: "Wrap up", systemImage: "flag.checkered", kind: .wrapUpFocus)
        } else if let leaveBy = PFDate.parse(g.departureLeaveBy) {
            VStack(alignment: .leading, spacing: 2) {
                Text("LEAVE BY").font(.caption2).foregroundStyle(.secondary)
                Text(leaveBy, style: .time)
                    .font(.title2.weight(.bold))
                    .foregroundStyle(WatchBrand.level(g.departureLevel))
                if let t = g.departureTitle { Text(t).font(.footnote).lineLimit(2) }
            }
        } else if let task = g.suggestionTitle {
            VStack(alignment: .leading, spacing: 2) {
                Text("DO NOW").font(.caption2).foregroundStyle(.secondary)
                Text(task).font(.headline).lineLimit(3)
                Text(doNowSub).font(.caption2).foregroundStyle(.secondary)
            }
        } else if g.freeMinutes > 0 {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(g.freeMinutes) min").font(.title2.weight(.bold))
                Text("free — nothing queued").font(.caption2).foregroundStyle(.secondary)
            }
        } else {
            VStack(alignment: .leading, spacing: 2) {
                Text("All clear").font(.title3.weight(.bold))
                if let t = g.nextTitle {
                    Text("next: \(t)").font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }

        if !g.hasActive, let t = g.nextTitle, let at = PFDate.parse(g.nextAt) {
            HStack(spacing: 4) {
                Image(systemName: "calendar").font(.caption2)
                Text("\(t) · \(at, style: .time)").font(.caption2).lineLimit(1)
            }
            .foregroundStyle(.secondary)
        }
    }

    private var doNowSub: String {
        var parts: [String] = []
        if let m = g.suggestionMinutes { parts.append("~\(m) min") }
        if g.freeMinutes > 0 { parts.append("\(g.freeMinutes)m free") }
        return parts.isEmpty ? "you can start now" : parts.joined(separator: " · ")
    }

    private func activeCard(tag: String, title: String, button: String,
                            systemImage: String, kind: WatchRequestKind) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(tag).font(.caption2).foregroundStyle(.secondary)
            Text(title).font(.headline).lineLimit(3)
            Button {
                Task { await model.lifecycle(kind) }
            } label: {
                Label(button, systemImage: systemImage).frame(maxWidth: .infinity)
            }
            .tint(WatchBrand.accent)
        }
    }
}
