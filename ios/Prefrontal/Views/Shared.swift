import SwiftUI

/// A button that runs an async action, showing a spinner and surfacing errors.
struct AsyncButton<Label: View>: View {
    var role: ButtonRole? = nil
    let action: () async throws -> Void
    @ViewBuilder var label: Label
    var onError: (String) -> Void = { _ in }

    @State private var running = false

    var body: some View {
        Button(role: role) {
            guard !running else { return }
            running = true
            Task {
                defer { running = false }
                do { try await action() }
                catch { onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription) }
            }
        } label: {
            ZStack {
                label.opacity(running ? 0 : 1)
                if running { ProgressView().controlSize(.small) }
            }
        }
        .disabled(running)
    }
}

/// Inline error strip.
struct ErrorBanner: View {
    let message: String
    var body: some View {
        Text(message)
            .font(.footnote)
            .foregroundStyle(Brand.nearWhite)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Brand.danger.opacity(0.22), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.danger.opacity(0.5)))
    }
}

/// Section heading used across cards.
struct CardLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption.weight(.semibold))
            .tracking(0.8)
            .foregroundStyle(Brand.muted)
    }
}

/// A pill chip.
struct Chip: View {
    let text: String
    var color: Color = Brand.line
    var fg: Color = Brand.muted
    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color, in: Capsule())
            .foregroundStyle(fg)
    }
}

/// Full-screen navy background used behind every tab.
struct BrandBackground<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ZStack {
            Brand.navy.ignoresSafeArea()
            content
        }
    }
}

extension View {
    /// Standard scroll list on the brand background with pull-to-refresh.
    func brandScreen() -> some View {
        self.scrollContentBackground(.hidden)
            .background(Brand.navy.ignoresSafeArea())
    }
}
