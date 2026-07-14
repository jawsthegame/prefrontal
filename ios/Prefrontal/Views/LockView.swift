import SwiftUI

/// Full-screen cover shown over the app while the biometric lock is engaged
/// (`BiometricLock`). Opaque, so it doubles as the app-switcher privacy shield
/// when the scene isn't active. Auto-prompting is driven by `RootView`'s
/// scene-phase handling; the button is the manual retry after a cancelled or
/// failed attempt.
struct LockView: View {
    @ObservedObject private var lock = BiometricLock.shared

    var body: some View {
        ZStack {
            Brand.bg.ignoresSafeArea()
            VStack(spacing: 18) {
                Image(systemName: lock.symbolName)
                    .font(.system(size: 52, weight: .light))
                    .foregroundStyle(Brand.accent)
                Text("Prefrontal is locked")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(Brand.fg)
                if let err = lock.lastError {
                    Text(err)
                        .font(.footnote)
                        .foregroundStyle(Brand.danger)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 32)
                }
                Button {
                    lock.authenticate()
                } label: {
                    HStack(spacing: 8) {
                        if lock.authenticating { ProgressView().controlSize(.small) }
                        Text("Unlock with \(lock.biometryName)")
                            .font(.body.weight(.semibold))
                    }
                    .padding(.horizontal, 20).padding(.vertical, 12)
                    .background(Brand.accent, in: Capsule())
                    .foregroundStyle(Brand.accentFg)
                }
                .disabled(lock.authenticating)
                .padding(.top, 4)
            }
        }
    }
}
