import SwiftUI

/// One tappable row per enabled self-care check. A tap logs it; at the daily
/// target a tap wraps the count back to zero (the tap-at-max cycle the phone and
/// Home Screen widget use, since the watch has no shift-click to rewind).
struct WatchSelfCareView: View {
    @EnvironmentObject private var model: WatchModel

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                if model.glance.selfCare.isEmpty {
                    Text("No self-care checks enabled.")
                        .font(.footnote).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, 12)
                } else {
                    ForEach(model.glance.selfCare) { check in
                        row(check)
                    }
                }
            }
            .padding(.horizontal, 2)
        }
        .navigationTitle("Self-care")
        .refreshable { await model.refresh() }
    }

    private func row(_ check: WatchGlance.WatchCheck) -> some View {
        Button {
            model.mark(check)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: check.done ? "checkmark.circle.fill" : WatchSelfCare.symbol(check.key))
                    .foregroundStyle(check.done ? WatchBrand.accent : .primary)
                Text(WatchSelfCare.label(check.key)).font(.body)
                Spacer()
                Text("\(check.count)/\(check.target)")
                    .font(.footnote.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(check.done ? WatchBrand.accent : .secondary)
            }
        }
        .buttonStyle(.bordered)
        .tint(check.done ? WatchBrand.accent.opacity(0.5) : .gray)
    }
}
