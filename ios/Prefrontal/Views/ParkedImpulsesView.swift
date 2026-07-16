import SwiftUI

/// The captured-impulse retro (`GET /impulses/parked`) — the Impulsivity module's
/// capture-and-defer, revisited. When an impulse pulls at your attention mid-task
/// you park it as a todo (one tap) instead of chasing it; later you triage the
/// batch: keep the real ones, drop the noise. Parked items are open
/// `source='impulse'` todos, so **Drop** reuses the normal todo-drop endpoint;
/// **Keep** leaves it in your todos (dismissed from this review pass only, since
/// it's already an open todo — there's no separate "reviewed" state server-side).
/// Server: `prefrontal/webhooks/routers/impulsivity.py`.
struct ParkedImpulsesView: View {
    @State private var parked: [ParkedImpulse] = []
    @State private var retro: String?
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let retro, !retro.isEmpty, !parked.isEmpty {
                    Card {
                        CardLabel(text: "Since you last looked")
                        Text(retro).font(.subheadline).foregroundStyle(Brand.nearWhite)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if parked.isEmpty && loaded && error == nil {
                    emptyState
                } else {
                    ForEach(parked) { impulseCard($0) }
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Parked impulses")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { if !loaded { await load() } }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "bolt.slash").font(.largeTitle).foregroundStyle(Brand.muted)
            Text("Nothing parked").font(.headline).foregroundStyle(Brand.nearWhite)
            Text("When an impulse pulls at you mid-task, capture it — it'll wait here for you to triage later.")
                .font(.footnote).foregroundStyle(Brand.muted).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.top, 60)
    }

    private func impulseCard(_ imp: ParkedImpulse) -> some View {
        Card {
            Text(imp.title).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
            // The raw captured text, shown when the inferred title trimmed it down.
            if let notes = imp.notes, !notes.isEmpty, notes != imp.title {
                Text(notes).font(.footnote).foregroundStyle(Brand.muted).lineLimit(3)
            }
            HStack(spacing: 10) {
                Button { keep(imp) } label: {
                    Label("Keep", systemImage: "checkmark").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered).tint(Brand.teal)
                AsyncButton {
                    try await withAPI { try await $0.closeTodo(imp.todoId, done: false) }
                    await load()
                } label: {
                    Label("Drop", systemImage: "trash").frame(maxWidth: .infinity)
                } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.muted)
            }
            .padding(.top, 4)
        }
    }

    // "Keep" = a real one; leave it in your todos. No server change (it's already
    // an open todo) — just remove it from this review pass so the batch works down.
    private func keep(_ imp: ParkedImpulse) {
        parked.removeAll { $0.todoId == imp.todoId }
    }

    private func load() async {
        do {
            let payload = try await withAPI { try await $0.parkedImpulses() }
            parked = payload.parked
            retro = payload.retro
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}
