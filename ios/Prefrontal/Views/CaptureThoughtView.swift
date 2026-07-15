import SwiftUI

/// The quick thought-capture sheet — the app-side landing for the zero-friction
/// capture surfaces that can't collect free text inline (the interactive-widget
/// button and the Control Center control open straight here; see
/// `SharedStore.requestCapture()` and `RootView`). The Action Button / Siri path
/// skips this entirely and dictates into `CaptureThoughtIntent`.
///
/// Whatever you type is fed to the LLM-as-sensor (`POST /observe`), which only
/// *proposes* candidate updates for later review — nothing is written to your data
/// on capture — so this stays a single, low-stakes text field with no options.
struct CaptureThoughtView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var error: String?
    @State private var captured = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                if captured {
                    confirmation
                } else {
                    editor
                }
                Spacer(minLength: 0)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .brandScreen()
            .navigationTitle("Capture a thought")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    AsyncButton {
                        try await capture()
                    } label: {
                        Text("Capture").fontWeight(.semibold)
                    } onError: { error = $0 }
                    .disabled(captured || trimmed.isEmpty)
                }
            }
            .onAppear { focused = true }
        }
    }

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    @ViewBuilder private var editor: some View {
        TextEditor(text: $text)
            .focused($focused)
            .font(.body)
            .foregroundStyle(Brand.fg)
            .scrollContentBackground(.hidden)
            .frame(minHeight: 140, alignment: .topLeading)
            .padding(8)
            .background(Brand.card, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Brand.line))
            .overlay(alignment: .topLeading) {
                if text.isEmpty {
                    Text("What's on your mind?")
                        .foregroundStyle(Brand.muted)
                        .padding(.horizontal, 13).padding(.vertical, 16)
                        .allowsHitTesting(false)
                }
            }
        Text("Prefrontal notes anything worth remembering and holds it for your "
             + "review — nothing is saved to your data until you accept it.")
            .font(.footnote).foregroundStyle(Brand.muted)
        if let error { ErrorBanner(message: error) }
    }

    private var confirmation: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Brand.good)
            Text("Captured.").font(.headline).foregroundStyle(Brand.fg)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 8)
    }

    private func capture() async throws {
        let raw = trimmed
        guard !raw.isEmpty else { return }
        error = nil
        // `observe` queues on a transport failure and returns 0, so an off-tailnet
        // capture still "succeeds" here — the thought is safely queued, not lost.
        _ = try await withAPI { try await $0.observe(text: raw) }
        captured = true
        focused = false
        // Let the checkmark register, then close.
        try? await Task.sleep(nanoseconds: 900_000_000)
        dismiss()
    }
}
