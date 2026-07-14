import SwiftUI

/// Quick actions: the no/low-input lifecycle taps plus dictated todo capture and
/// Panic. I'm back / Wrap up appear only while their session is active; Panic and
/// Add todo are always available.
struct WatchActionsView: View {
    @EnvironmentObject private var model: WatchModel

    @State private var draft = ""
    @State private var panic: Panic?
    @State private var showPanic = false
    @State private var busy = false

    private var g: WatchGlance { model.glance }

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                if g.outingIntention != nil {
                    actionButton("I'm back", systemImage: "house") {
                        await model.lifecycle(.imBack)
                    }
                }
                if g.focusTask != nil {
                    actionButton("Wrap up focus", systemImage: "flag.checkered") {
                        await model.lifecycle(.wrapUpFocus)
                    }
                }

                TextField("Add todo", text: $draft)
                    .onSubmit(addTodo)
                Button("Add", systemImage: "plus.circle.fill", action: addTodo)
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    .tint(WatchBrand.accent)

                actionButton("Panic", systemImage: "exclamationmark.triangle.fill",
                             tint: WatchBrand.lvlCall) {
                    await loadPanic()
                }

                if let err = model.errorText {
                    Text(err).font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 2)
        }
        .navigationTitle("Actions")
        .sheet(isPresented: $showPanic) { panicSheet }
    }

    private func actionButton(_ title: String, systemImage: String,
                              tint: Color = WatchBrand.accent,
                              action: @escaping () async -> Void) -> some View {
        Button {
            Task { busy = true; await action(); busy = false }
        } label: {
            Label(title, systemImage: systemImage).frame(maxWidth: .infinity)
        }
        .tint(tint)
        .disabled(busy)
    }

    private func addTodo() {
        model.addTodo(draft)
        draft = ""
    }

    private func loadPanic() async {
        do {
            panic = try await WatchConnectivityClient.shared.request(.panic, as: Panic.self)
            showPanic = true
        } catch {
            model.errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    @ViewBuilder private var panicSheet: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if let p = panic {
                    Text(p.headline).font(.headline)
                    if let step = p.firstStep {
                        Text("First step").font(.caption2).foregroundStyle(.secondary)
                        Text(step).font(.body)
                    }
                    if let c = p.counts {
                        Text("\(c.late ?? 0) late · \(c.soon ?? 0) soon")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                } else {
                    ProgressView()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
    }
}
