import SwiftUI

/// Honest prioritization — the todos the plain list quietly buries. Surfaces the
/// important items you keep **skipping** (`GET /todos/avoided`, worst-avoided
/// first, each startable in place) and the tasks you keep **bailing on**
/// (`GET /todos/stuck`) with the Task-Paralysis body-double ("start together")
/// nudge and a tiny first step. Pure reads with pull-to-refresh. Reached from the
/// Todos tab.
struct StuckAvoidedView: View {
    @State private var avoided: [AvoidedTodo] = []
    @State private var stuck: [StuckTodo] = []
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if loaded && avoided.isEmpty && stuck.isEmpty && error == nil {
                    emptyState
                }
                if !avoided.isEmpty { avoidedCard }
                if !stuck.isEmpty { stuckCard }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Stuck & avoided")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
    }

    private var emptyState: some View {
        Card {
            VStack(spacing: 8) {
                Image(systemName: "checkmark.circle").font(.largeTitle).foregroundStyle(Brand.good)
                Text("Nothing's slipping").font(.headline).foregroundStyle(Brand.nearWhite)
                Text("No important todos are sitting too long, and nothing's been repeatedly bailed on. Nice.")
                    .font(.footnote).foregroundStyle(Brand.muted)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
    }

    private var avoidedCard: some View {
        Card {
            CardLabel(text: "Keep skipping")
            Text("Important todos that have been sitting — worst first.")
                .font(.caption).foregroundStyle(Brand.muted)
            ForEach(Array(avoided.enumerated()), id: \.element.id) { idx, a in
                if idx > 0 { Divider().overlay(Brand.line) }
                VStack(alignment: .leading, spacing: 5) {
                    Text(a.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                    FlowRow(spacing: 6) {
                        Chip(text: daysOpenText(a.daysOpen), color: Brand.warn)
                        if let p = a.priority, p >= 2 { Chip(text: priorityLabel(p), color: Brand.warn) }
                        if let m = a.estimateMinutes { Chip(text: "~\(Int(m))m") }
                        if let d = a.deadline, let short = deadlineShort(d) { Chip(text: short, color: Brand.danger) }
                    }
                    AsyncButton {
                        try await withAPI { try await $0.startTodo(a.todoId) }
                        await load()
                    } label: {
                        Label("Start now", systemImage: "play.circle").font(.caption)
                    } onError: { error = $0 }
                    .buttonStyle(.bordered).tint(Brand.teal)
                }
            }
        }
    }

    private var stuckCard: some View {
        Card {
            CardLabel(text: "Stuck — try a body-double")
            Text("Tasks you keep bailing on. A solo start isn't working — starting alongside someone helps.")
                .font(.caption).foregroundStyle(Brand.muted)
            ForEach(Array(stuck.enumerated()), id: \.element.id) { idx, s in
                if idx > 0 { Divider().overlay(Brand.line) }
                VStack(alignment: .leading, spacing: 5) {
                    Text(s.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                    Chip(text: "\(s.misses) missed of \(s.attempts) tries", color: Brand.danger)
                    if let fs = s.firstStep, !fs.isEmpty {
                        Text("First step: \(fs)").font(.footnote).foregroundStyle(Brand.fg)
                        AsyncButton {
                            try await withAPI { try await $0.addTodo(title: fs) }
                        } label: {
                            Label("Add first step as a todo", systemImage: "plus.circle").font(.caption)
                        } onError: { error = $0 }
                        .buttonStyle(.bordered).tint(Brand.teal)
                    }
                    if let sug = s.suggestion, !sug.isEmpty {
                        Text(sug).font(.caption).foregroundStyle(Brand.muted)
                    }
                }
            }
        }
    }

    private func daysOpenText(_ days: Double) -> String {
        let d = Int(days.rounded())
        return d <= 1 ? "open ~1 day" : "open \(d) days"
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let a = client.avoidedTodos()
            async let s = client.stuckTodos()
            avoided = try await a
            stuck = try await s
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}
